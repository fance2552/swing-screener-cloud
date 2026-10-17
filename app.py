"""Cloud Cockpit v4.0 — 2-Tab (사냥 데스크 / 수호 데스크).

보존 인프라: Streamlit Secrets, 6자리 PIN 게이트, cloud_sync(Gist) 어댑터, zoneinfo 세션 시계.
"""
from __future__ import annotations

import datetime as dt
import hmac
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import streamlit as st

import ai_advisor as ai
from screener import episodic_pivot as ep
from screener import fmp_client as fc
from screener import portfolio as pf

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

st.set_page_config(page_title="Cloud Cockpit v4.0", page_icon="🎯", layout="wide")


# ============================================================================
# 인프라: 시크릿 / PIN 게이트 / 시계
# ============================================================================
def get_secret(*names):
    for n in names:
        try:
            v = st.secrets.get(n)
        except Exception:
            v = None
        if v:
            return str(v).strip()
        env = os.environ.get(n)
        if env:
            return str(env).strip()
    return ""


FMP_KEY = get_secret("FMP_API_KEY", "FMP_KEY")


@st.cache_resource
def _pin_guard() -> dict:
    # 세션을 새로 열어도 실패 지연은 프로세스 안에 남는다.
    return {"lock": threading.Lock(), "fails": 0, "next_ok": 0.0}


def auth_gate():
    """PIN 게이트 (fail-closed). 미인증이면 st.stop(). 시크릿 이름은 COCKPIT_PIN 이다."""
    if st.session_state.get("authenticated") or st.session_state.get("authed"):
        return
    expected = get_secret("COCKPIT_PIN", "APP_PIN", "PIN", "PIN_CODE", "APP_PASSWORD")
    st.title("🔐 Cloud Cockpit")
    if not expected:
        st.error("PIN 시크릿(COCKPIT_PIN)이 설정되지 않아 접근을 차단했습니다. Streamlit Secrets 를 확인하세요.")
        st.stop()
    with st.form("pin_form"):
        pin = st.text_input("PIN", type="password")
        ok = st.form_submit_button("입장")
    if ok:
        guard = _pin_guard()
        with guard["lock"]:
            wait = guard["next_ok"] - time.time()
            if wait > 0:
                st.error(f"연속 실패로 잠김. {wait:.0f}초 후 다시 시도하십시오.")
                st.stop()
            if hmac.compare_digest(pin.strip().encode("utf-8"), expected.strip().encode("utf-8")):
                guard["fails"] = 0
                guard["next_ok"] = 0.0
                passed = True
            else:
                guard["fails"] += 1
                guard["next_ok"] = time.time() + min(2 ** guard["fails"], 60)
                passed = False
        if passed:
            st.session_state["authenticated"] = True
            st.session_state["authed"] = True
            st.rerun()
        st.error("PIN 이 일치하지 않습니다.")
    st.stop()


def _hunt_boards() -> tuple[list, list]:
    pool = st.session_state.get("pool") or {}
    top = pool.get("top") if isinstance(pool.get("top"), list) else []
    ep_rows = [row for row in top if isinstance(row, dict) and row.get("setup") == ep.SETUP_A]
    pull_rows = [row for row in top if isinstance(row, dict) and row.get("setup") == ep.SETUP_B]
    return ep_rows, pull_rows


@st.fragment(run_every=dt.timedelta(seconds=0.5))
def _health_badges() -> None:
    fc.pulse(FMP_KEY)
    now = time.time()
    ts = float(getattr(fc, "LAST_OK_TS", 0) or 0)
    if ts <= 0:
        try:
            from state import SHARED
            ts = float(getattr(SHARED, "last_fmp_update", 0) or 0)
        except Exception:
            ts = 0.0
    age = fc.age_label(now, ts)
    if not FMP_KEY:
        fmp_txt = "키 없음"
    elif ts <= 0:
        fmp_txt = "대기"
    elif now - ts > 45:
        fmp_txt = f"끊김 · {age}"
    else:
        fmp_txt = f"연결됨 · {age}"
    regime = get_regime(FMP_KEY) if FMP_KEY else None
    if not regime:
        phase = "대기"
    elif regime.get("above"):
        phase = "NORMAL"
    else:
        phase = "CORRECTION"
    pool = st.session_state.get("pool") or {}
    scanned = int(pool.get("scanned") or 0)
    n_a = int(pool.get("n_a") or 0)
    n_b = int(pool.get("n_b") or 0)
    import env_settings
    deep = (env_settings.get_api_keys().get("DEEPSEEK_API_KEY") or "").strip()
    ai_txt = "DeepSeek CIO" if deep else "키 없음"
    stamp = dt.datetime.now(KST).strftime("%H:%M:%S")
    st.markdown(
        '<div style="display:flex;flex-wrap:wrap;gap:6px;font-size:12px;line-height:1.3;margin:4px 0 8px 0;">'
        f'<span style="border:1px solid #555;border-radius:999px;padding:1px 8px;">🚀 FMP: {fmp_txt}</span>'
        f'<span style="border:1px solid #555;border-radius:999px;padding:1px 8px;">💡 전략: {phase} · 검사 {scanned} · EP {n_a} · 눌림 {n_b}</span>'
        f'<span style="border:1px solid #555;border-radius:999px;padding:1px 8px;">🧠 AI: {ai_txt}</span>'
        f'<span style="border:1px solid #555;border-radius:999px;padding:1px 8px;">[Last Update: {stamp}]</span>'
        "</div>",
        unsafe_allow_html=True,
    )


def clock_header():
    _health_badges()
    now_k = dt.datetime.now(KST)
    now_e = now_k.astimezone(ET)
    state, minutes = ep.market_state(now_e)
    label = {"LIVE": f"🟢 장중 (개장 {minutes}분 경과)", "PRE": "🟡 프리마켓/개장 전", "CLOSED": "⚪ 장 마감"}[state]
    c1, c2, c3 = st.columns([1, 1, 2])
    c1.metric("한국 시각 (KST)", now_k.strftime("%m-%d %H:%M"))
    c2.metric("미국 동부 (ET)", now_e.strftime("%m-%d %H:%M"))
    c3.metric("미국 시장 상태", label)


def entry_badge() -> str:
    from screener import radar
    ts = radar.now_et()
    if not radar.is_session_day(ts.date()):
        return "🔒 휴장 — 사격 잠금. 22:45 해제는 다음 거래일까지 없다."
    if not radar.session_allows_fire_alert(ts):
        t = ep.recommended_entry_kst()
        h12 = t.hour % 12 or 12
        ampm = "밤" if t.hour >= 18 or t.hour < 5 else "낮"
        return f"🔒 사격 잠금 · 해제 {ampm} {h12}:{t.minute:02d} (개장 15분 후)"
    t = ep.recommended_entry_kst()
    h12 = t.hour % 12 or 12
    ampm = "밤" if t.hour >= 18 or t.hour < 5 else "낮"
    return f"💡 권장 진입: {ampm} {h12}:{t.minute:02d} 이후 (첫 15분 노이즈 확인 후 진입 권장)"


def safe(x):
    return ai.sanitize_text(x)


# ============================================================================
# 캐시된 데이터 조회
# ============================================================================
@st.cache_data(ttl=600, show_spinner=False)
def get_regime(key: str):
    df = fc.fetch_eod(key, "QQQ", 60)
    if df is None or len(df) < 20:
        return None
    close, s20 = float(df["close"].iloc[-1]), fc.sma(df["close"], 20)
    return {"close": close, "sma20": s20, "above": close >= s20}


@st.cache_data(ttl=45, show_spinner=False)
def get_marks(tickers: tuple, key: str):
    def one(t):
        q = fc.fetch_quote(key, t)
        bars = fc.fetch_eod(key, t, 40)
        return t, {"price": q["price"] if q else None,
                   "sma10": fc.sma(bars["close"], 10) if bars is not None else None}
    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(ex.map(one, tickers))


# ============================================================================
# Tab 1: 사냥 데스크
# ============================================================================
def candidate_card(c: dict, equity: float, risk_pct: float):
    p = c["plan"]
    icon = "🔥" if c["setup"] == ep.SETUP_A else "🎯"
    with st.container(border=True):
        st.subheader(f"{icon} {c['symbol']} · {safe(c['name'])}")
        st.caption(f"{c['setup_label']} · 종합점수 {c['score']:.0f} · EP 발생일 {c['ep_date']}")
        m = st.columns(5)
        m[0].metric("현재가", ai.usd(c["price"]))
        m[1].metric("갭", f"{c['gap']:+.1f}%")
        m[2].metric("거래량 배수", f"{c['volx']:.1f}x" + (" (추정)" if c["projected"] else ""))
        m[3].metric("손절폭", f"-{p['stop_pct']:.1f}%")
        m[4].metric("손익비", f"{p['rr']:.1f} : 1")
        cat = c.get("catalyst") or {}
        st.markdown(f"**촉매** — {safe(cat.get('text', '-'))}")
        st.markdown(
            f"- 진입 지정가: **{ai.usd(p['entry_lo'])} ~ {ai.usd(p['entry_hi'])}**\n"
            f"- 강제 손절: **{ai.usd(p['stop'])}** (-{p['stop_pct']:.1f}%)\n"
            f"- 1차 목표: **{ai.usd(p['t1'])}** (+{p['t1_pct']:.1f}%) → 50% 매도\n"
            f"- 2차: 10일선 {ai.usd(c.get('sma10'))} 이탈까지 완주")
        for w in c["warnings"]:
            st.warning(safe(w))
        if p["lod_cap_exceeded"]:
            st.warning(f"당일 저가 기준 손절 거리가 {p['struct_stop_dist_pct']:.1f}% 로 한도(3.5%)보다 넓어 "
                       "-3.5% 하드 손절로 조였습니다. 노이즈 손절 가능성이 있으니 수량을 줄이세요.")

        key = f"order_{c['symbol']}_{c['setup']}"
        if st.button("📋 딥시크 주문표 발행", key=f"btn_{key}"):
            with st.spinner("결재판 작성 중..."):
                kst_entry = ep.recommended_entry_kst().strftime("%H:%M")
                ep_rows, pull_rows = _hunt_boards()
                st.session_state.setdefault("orders", {})[key] = ai.review_order(
                    c, equity, risk_pct, kst_entry, ep_rows, pull_rows
                )
        res = st.session_state.get("orders", {}).get(key)
        if res:
            v = res["verdict"]
            badge = {"APPROVE": "✅ 승인", "CONDITIONAL": "🟡 조건부 승인", "REJECT": "⛔ 반려",
                     "RULES_ONLY": "ℹ️ AI 미응답 — 규칙 기준만 통과"}[v]
            st.markdown(f"### 1페이지 주문 결재판 — {badge}")
            if res.get("net_rr") is not None:
                st.markdown(f"**[수수료 반영 실질 손익비: {float(res['net_rr']):.1f}]**")
            a = res["ai"] or {}
            if a.get("summary"):
                st.markdown(f"**총평** — {safe(a['summary'])}")
            cols = st.columns(2)
            with cols[0]:
                for r in a.get("reasons", []):
                    st.markdown(f"- {safe(r)}")
            with cols[1]:
                for r in a.get("risks", []):
                    st.markdown(f"- ⚠️ {safe(r)}")
            for r in a.get("conditions", []):
                st.info(safe(r))
            if res["provider"]:
                st.caption(f"심의 엔진: {res['provider']}")
            elif res["errors"]:
                st.caption("AI 호출 실패: " + ", ".join(res["errors"]))
            st.code(res["text"], language="text")


def tab_hunt():
    st.markdown(f"**{entry_badge()}**")
    reg = get_regime(FMP_KEY) if FMP_KEY else None
    if reg:
        if reg["above"]:
            st.success(f"시장 필터: QQQ {reg['close']:.2f} ≥ 20일선 {reg['sma20']:.2f} — 신규 진입 허용")
        else:
            st.error(f"시장 필터: QQQ {reg['close']:.2f} < 20일선 {reg['sma20']:.2f} — 신규 진입 휴식 권고 (돌파 승률 급락 구간)")

    with st.sidebar:
        st.header("⚙️ 사냥 설정")
        equity = st.number_input("계좌 규모 (USD)", min_value=100.0, value=float(st.session_state.get("equity", 1000.0)),
                                 step=100.0)
        risk = st.number_input("1회 리스크 (% of 계좌)", min_value=0.1, max_value=2.0,
                               value=float(st.session_state.get("risk", 0.75)), step=0.05)
        st.session_state["equity"], st.session_state["risk"] = equity, risk

    c1, c2 = st.columns([1, 3])
    do_scan = c1.button("🔄 사냥터 최신 재검색", type="primary", width="stretch")
    with c2.expander("셋업 B 백필 (느림 · 선택)"):
        st.caption("서버 재시작 등으로 EP 기록이 없을 때, 우량 유동주 상위 N개의 최근 5일 EP 이력을 복원합니다.")
        n_uni = st.slider("스캔 종목 수", 100, 400, 250, 50)
        do_backfill = st.button("📚 백필 포함 재검색")

    if not FMP_KEY:
        st.error("FMP_API_KEY 시크릿이 없습니다.")
        return

    if do_scan or do_backfill:
        bar = st.progress(0.0, text="사냥터 스캔 중...")
        pool = ep.scan_all(FMP_KEY, backfill=do_backfill, max_universe=n_uni,
                           progress=lambda d, t, s: bar.progress(min(d / max(t, 1), 1.0), text=f"{d}/{t} · {s}"))
        bar.empty()
        st.session_state["pool"] = pool
    elif "pool" not in st.session_state:
        pool = ep.load_pool()
        if ep.pool_is_current(pool):
            st.session_state["pool"] = pool

    pool = st.session_state.get("pool")
    if not pool:
        st.info("오늘 스캔 기록이 없습니다. [🔄 사냥터 최신 재검색]을 눌러 시작하세요.")
        return

    stale = pool.get("session") != ep.session_date().isoformat()
    st.caption(f"스캔 기준 세션 {pool['session']} · 실행 {pool['scanned_at_kst']} KST · "
               f"{pool['scanned']}건 검사 · A {pool['n_a']} / B {pool['n_b']}"
               + (" · ⚠️ 이전 세션 결과입니다. 재검색을 권장합니다." if stale else ""))
    top = pool.get("top", [])
    if not top:
        st.markdown("## 💤 오늘은 기준을 통과한 A급 셋업이 없습니다 (현금 100% 휴식)")
    else:
        for c in top:
            candidate_card(c, equity, risk)
    if pool.get("rejects"):
        with st.expander("탈락 사유 요약"):
            for k, v in pool["rejects"].items():
                st.write(f"- {safe(k)}: {v}건")


# ============================================================================
# Tab 2: 수호 데스크
# ============================================================================
def _persist(msg_prefix=""):
    ok, msg = pf.save_positions(st.session_state["positions"])
    st.toast(f"{msg_prefix}{msg}", icon="✅" if ok else "⚠️")


def _on_ticker():
    t = st.session_state.get("reg_ticker", "").strip().upper()
    st.session_state["reg_ticker"] = t
    q = fc.fetch_quote(FMP_KEY, t) if (t and FMP_KEY) else None
    if q:
        st.session_state["reg_avg"] = round(q["price"], 2)
        st.session_state["reg_msg"] = f"{t} 실시간 시세 {ai.usd(q['price'])} 자동 입력"
    else:
        st.session_state["reg_msg"] = f"{t} 시세 조회 실패 — 평단가를 직접 입력하세요"


def _on_stop_change(i):
    st.session_state["positions"][i]["stop_pct"] = float(st.session_state[f"sp_{i}"])
    _persist()


def tab_guard():
    if "positions" not in st.session_state:
        with st.spinner("Gist 포트폴리오 동기화 중..."):
            pos, msg = pf.load_positions()
        st.session_state["positions"], st.session_state["sync_msg"] = pos, msg
    st.caption(f"동기화 상태: {safe(st.session_state.get('sync_msg', ''))}")

    top = st.columns([1, 1, 4])
    if top[0].button("🔄 시세 새로고침"):
        get_marks.clear()
        st.rerun()
    if top[1].button("☁️ Gist 다시 불러오기"):
        pos, msg = pf.load_positions()
        st.session_state["positions"], st.session_state["sync_msg"] = pos, msg
        st.rerun()

    with st.expander("➕ 포지션 등록", expanded=not st.session_state["positions"]):
        st.session_state.setdefault("reg_avg", 0.0)
        r = st.columns([1.2, 1, 1, 1])
        r[0].text_input("티커 (입력 후 Enter → 시세 자동 조회)", key="reg_ticker", on_change=_on_ticker)
        r[1].number_input("평단가 (USD)", min_value=0.0, step=0.01, key="reg_avg", format="%.2f")
        qty = r[2].number_input("수량", min_value=0.0, step=1.0, value=1.0, key="reg_qty")
        sp = r[3].number_input("손절폭 %", min_value=pf.STOP_MIN, max_value=pf.STOP_MAX, value=pf.STOP_MAX,
                               step=0.1, key="reg_sp")
        if st.session_state.get("reg_msg"):
            st.caption(safe(st.session_state["reg_msg"]))
        if st.button("원클릭 등록", type="primary"):
            p = pf.new_position(st.session_state.get("reg_ticker", ""), qty, st.session_state["reg_avg"], sp)
            if p:
                st.session_state["positions"].append(p)
                _persist(f"{p['ticker']} 등록 · ")
                st.rerun()
            else:
                st.error("티커·수량·평단가를 확인하세요.")

    positions = st.session_state["positions"]
    if not positions:
        st.info("등록된 포지션이 없습니다.")
        return
    marks = get_marks(tuple(p["ticker"] for p in positions), FMP_KEY) if FMP_KEY else {}
    rows = [pf.evaluate(p, marks.get(p["ticker"], {}).get("price"), marks.get(p["ticker"], {}).get("sma10"))
            for p in positions]
    s = pf.summarize(rows)
    k = st.columns(4)
    k[0].metric("투입 원가", ai.usd(s["cost"]))
    k[1].metric("평가금액", ai.usd(s["value"]))
    k[2].metric("순손익 (수수료 0.2% 반영)", ai.usd(s["pnl"]), f"{s['pnl_pct']:+.2f}%")
    k[3].metric("보유 종목", f"{len(positions)}개")

    icon = {"HOLD": "🟢", "TRIM": "🟡", "EXIT": "🔴", "NO_DATA": "⚪", "TIME_EXIT": "🚨"}
    for i, (p, r) in enumerate(zip(positions, rows)):
        with st.container(border=True):
            h = st.columns([2, 1, 1, 1, 1])
            h[0].markdown(f"### {icon[r['signal']]} {p['ticker']}  {safe(r['alert'])}")
            h[1].metric("현재가", ai.usd(r["price"]))
            h[2].metric("평단", ai.usd(r["avg"]))
            h[3].metric("순손익", "-" if r["net_pct"] is None else f"{r['net_pct']:+.2f}%")
            h[4].metric("수량", f"{p['qty']:g}")
            st.markdown(
                f"- 손절선: **{ai.usd(r['stop'])}**" + (" (본전 · 수수료 포함)" if r["tp1_done"] else f" (-{p['stop_pct']:.1f}%)")
                + f"\n- 1차 익절선: **{ai.usd(r['tp1'])}** (+5%)" + (" — 체결 완료" if r["tp1_done"] else "")
                + f"\n- 10일선: **{ai.usd(r['sma10'])}**\n- 규칙 판정: {safe(r['reason'])}")
            if r["signal"] == "TIME_EXIT":
                st.error("🚨 사이렌 — 보유 3거래일 초과. 수익률과 관계없이 전량 청산하라.")
            b = st.columns([1.2, 1.2, 1.2, 3])
            b[0].number_input("손절폭 %", min_value=pf.STOP_MIN, max_value=pf.STOP_MAX, value=float(p["stop_pct"]),
                              step=0.1, key=f"sp_{i}", on_change=_on_stop_change, args=(i,),
                              disabled=r["tp1_done"])
            if b[1].button("1차 익절 체결 처리", key=f"tp1_{i}", disabled=r["tp1_done"]):
                st.session_state["positions"][i] = pf.apply_tp1(p)
                _persist(f"{p['ticker']} 50% 익절 · ")
                st.rerun()
            if b[2].button("포지션 삭제", key=f"del_{i}"):
                st.session_state["positions"].pop(i)
                _persist(f"{p['ticker']} 삭제 · ")
                st.rerun()

    st.divider()
    if st.button("🛡️ 딥시크 포트폴리오 정밀 감리", type="primary"):
        with st.spinner("전수 감리 중..."):
            ep_rows, pull_rows = _hunt_boards()
            st.session_state["audit"] = ai.audit_portfolio(rows, ep_rows, pull_rows)
    audit = st.session_state.get("audit")
    if audit:
        label = {
            "HOLD": "🟢 [HOLD 홀딩 유지]",
            "TRIM": "🟡 [TRIM 50% 익절]",
            "EXIT": "🔴 [EXIT 탈출/손절]",
            "TIME_EXIT": "🚨 [TIME_EXIT 3거래일 초과]",
        }
        for it in audit["items"]:
            with st.container(border=True):
                st.markdown(f"**{it['ticker']}** — {label[it['action']]}")
                st.markdown(
                    f"{safe(it['reason'])}\n\n"
                    f"- 현재가 {ai.usd(it['price'])} · 손절선 **{ai.usd(it['stop'])}** · 익절선 **{ai.usd(it['tp1'])}** · "
                    f"10일선 {ai.usd(it['sma10'])}")
        if audit.get("overall"):
            st.info(safe(audit["overall"]))
        if audit.get("provider"):
            st.caption(f"감리 엔진: {audit['provider']} (규칙의 EXIT/TRIM 은 AI 가 하향할 수 없음)")
        elif audit.get("errors"):
            st.caption("AI 호출 실패 → 규칙 판정만 표시: " + ", ".join(audit["errors"]))


# ============================================================================
# 엔트리
# ============================================================================
def _sidebar_keys() -> None:
    import env_settings

    keys = env_settings.get_api_keys()
    names = ("DEEPSEEK_API_KEY", "GROQ_API_KEY", "FMP_API_KEY")
    with st.sidebar:
        st.header("🔑 API 키")
        st.caption("저장하면 비공개 Gist에 남는다. 재배포나 다른 기기에서도 다시 읽는다.")
        for name in names:
            state_key = f"cloud_key_{name}"
            if state_key not in st.session_state:
                st.session_state[state_key] = keys.get(name) or ""
            st.text_input(name, type="password", key=state_key)
        if st.button("키를 Gist에 저장", key="save_cloud_keys_btn", width="stretch"):
            merged = dict(keys)
            for name in names:
                merged[name] = str(st.session_state.get(f"cloud_key_{name}") or "").strip()
            env_settings.save_api_keys(merged)
            global FMP_KEY
            FMP_KEY = get_secret("FMP_API_KEY", "FMP_KEY")
            st.rerun()
        deep = (env_settings.get_api_keys().get("DEEPSEEK_API_KEY") or "").strip()
        if deep:
            st.caption("AI 엔진: DEEPSEEK (deepseek-chat) 🟢")
        else:
            st.caption(f"AI 엔진: {ai.provider_label()}")


auth_gate()
import env_settings
env_settings.get_api_keys()
FMP_KEY = get_secret("FMP_API_KEY", "FMP_KEY")
_sidebar_keys()
st.title("🎯 Cloud Cockpit v4.0")
clock_header()
t1, t2 = st.tabs(["🎯 사냥 데스크", "🛡️ 수호 데스크"])
with t1:
    tab_hunt()
with t2:
    tab_guard()
