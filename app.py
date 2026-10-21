"""Cloud Cockpit (버전은 version.py) — 3-Tab (사냥 / 수호 / 에이전시).

보존 인프라: Streamlit Secrets, 6자리 PIN 게이트, cloud_sync(Gist) 어댑터, zoneinfo 세션 시계.
"""
from __future__ import annotations

import datetime as dt
import hmac
import importlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import streamlit as st

import ai_advisor as ai
from version import APP_VERSION, version_label
from screener import episodic_pivot as ep
from screener import fmp_client as fc
from screener import intraday as itd
from screener import ledger as lg
from screener import portfolio as pf
from screener import pullback as pb
from ui import verdict as vd

# v4.5.6: 클라우드 재배포 때 app.py 만 새로 읽고 바뀐 부품 파일은 예전 것을 쓰던 문제(AttributeError) 방지. 둘 다 상태 없는 표시·규칙 모듈이다.
vd, pf = importlib.reload(vd), importlib.reload(pf)

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")
# backtest/pullback_stop_2026-10-19.md: 3년 757건에서 손절 -3~-20% 모두 손절 없음보다 평균·적중률이 낮았다.
# v4.5.9 (swing-agent research/stop_test.py): 장중 -15%는 평균 -0.2%p 대신 최악 -54% → -28%. 드문 폭락 보험으로만 표시.
PB_STOP_NOTE = ("3년 백테스트에서 -3~-10% 손절은 반등 전에 털려 성적이 나빠집니다. "
                "-15% 비상 손절만 토스에 예약해 드문 폭락을 막습니다(평소엔 거의 안 걸림).")

st.set_page_config(page_title=f"Cloud Cockpit {APP_VERSION}", page_icon="🎯", layout="wide")


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


def _pin_secret() -> str:
    return get_secret("COCKPIT_PIN", "APP_PIN", "PIN", "PIN_CODE", "APP_PASSWORD")


def _pin_submit() -> None:
    """PIN 입력칸에서 엔터를 치면(on_change) 바로 검사한다. 버튼을 누르지 않아도 된다."""
    pin = str(st.session_state.get("pin_input") or "")
    st.session_state["pin_input"] = ""
    expected = _pin_secret()
    if not pin.strip() or not expected:
        return
    guard = _pin_guard()
    with guard["lock"]:
        wait = guard["next_ok"] - time.time()
        if wait > 0:
            st.session_state["pin_msg"] = f"연속 실패로 잠김. {wait:.0f}초 후 다시 시도하십시오."
            return
        if hmac.compare_digest(pin.strip().encode("utf-8"), expected.strip().encode("utf-8")):
            guard["fails"] = 0
            guard["next_ok"] = 0.0
            st.session_state["authenticated"] = True
            st.session_state["authed"] = True
            st.session_state.pop("pin_msg", None)
            return
        guard["fails"] += 1
        guard["next_ok"] = time.time() + min(2 ** guard["fails"], 60)
    st.session_state["pin_msg"] = "PIN 이 일치하지 않습니다."


def auth_gate():
    """PIN 게이트 (fail-closed). 미인증이면 st.stop(). 시크릿 이름은 COCKPIT_PIN 이다."""
    if st.session_state.get("authenticated") or st.session_state.get("authed"):
        return
    st.title("🔐 Cloud Cockpit")
    if not _pin_secret():
        st.error("PIN 시크릿(COCKPIT_PIN)이 설정되지 않아 접근을 차단했습니다. Streamlit Secrets 를 확인하세요.")
        st.stop()
    st.text_input("PIN (입력 후 엔터)", type="password", key="pin_input", on_change=_pin_submit)
    st.button("입장", on_click=_pin_submit)
    if st.session_state.get("pin_msg"):
        st.error(st.session_state["pin_msg"])
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
    phase = regime_phase(regime)
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
    t = ep.recommended_entry_kst()
    h12 = t.hour % 12 or 12
    ampm = "밤" if t.hour >= 18 or t.hour < 5 else "낮"
    if not radar.is_session_day(ts.date()):
        return "📅 오늘은 미국 휴장일 · 다음 거래일 본장 개장 후 재검색하세요."
    if not radar.session_allows_fire_alert(ts):
        state, _ = ep.market_state()
        if state == "CLOSED":
            return f"🌙 오늘 본장 마감 · 다음 거래일 {ampm} {h12}:{t.minute:02d}(개장 15분 후)부터 진입 판단"
        return (f"⏳ 본장 대기 · {ampm} {h12}:{t.minute:02d}(개장 15분 후)부터 진입 판단 "
                "(셋업 A는 개장 30분 후부터 판정)")
    return f"💡 권장 진입: {ampm} {h12}:{t.minute:02d} 이후 (첫 15분 노이즈 확인 후 진입 권장)"


def safe(x):
    return ai.sanitize_text(x)


def _metric(col, label: str, value: str, note: str = "") -> None:
    """화살표 없는 4칸용 칸: 큰 값 + 작은 설명 한 줄."""
    col.metric(label, value)
    if note:
        col.caption(safe(note))


# ============================================================================
# 캐시된 데이터 조회
# ============================================================================
@st.cache_data(ttl=600, show_spinner=False)
def get_regime(key: str):
    """SPY 200일선 + 25세션 분산일. 엔진(SHARED.regime)과 같은 정의."""
    from screener import regime as rg
    df = fc.fetch_eod(key, "SPY", 300)
    if df is None or len(df) < 200:
        return None
    spy = df.rename(columns={"close": "Close", "volume": "Volume"}).set_index("date")
    return rg.get_market_regime(spy)


def regime_phase(reg) -> str:
    """NORMAL / PRESSURE / CORRECTION / 대기(판정 불가)."""
    state = str((reg or {}).get("state") or "").upper()
    return state if state in ("NORMAL", "PRESSURE", "CORRECTION") else "대기"


@st.cache_data(ttl=45, show_spinner=False)
def get_marks(tickers: tuple, key: str):
    def one(t):
        q = fc.fetch_quote(key, t)
        bars = fc.fetch_eod(key, t, 40)
        return t, {"price": q["price"] if q else None,
                   "sma10": fc.sma(bars["close"], 10) if bars is not None else None,
                   "sma5": fc.sma(bars["close"], 5) if bars is not None else None,
                   "pb_target": pf.pb_target(bars["date"], bars["close"], dt.datetime.now(ET))
                   if bars is not None else None}
    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(ex.map(one, tickers))


# ============================================================================
# Tab 1: 사냥 데스크
# ============================================================================
@st.cache_data(ttl=25, show_spinner=False)
def _bars_5m(sym: str, day: str, key: str):
    import env_settings
    keys = env_settings.get_api_keys()
    alp = ((keys.get("ALPACA_API_KEY") or "").strip(), (keys.get("ALPACA_SECRET_KEY") or "").strip())
    return itd.fetch_5min(sym, dt.date.fromisoformat(day), key, alp)


@st.cache_data(ttl=10, show_spinner=False)
def _live_price(sym: str, key: str):
    q = fc.fetch_quote(key, sym) if key else None
    return q["price"] if q else None


@st.fragment(run_every=dt.timedelta(seconds=30))
def ep_order_block(c: dict) -> None:
    """카드 맨 위 결론 한 줄 + 4칸. 장중에는 30초마다 ORH 기준으로 진입가·손절·목표를 다시 계산한다."""
    now_e = dt.datetime.now(ET)
    state, _ = ep.market_state(now_e)
    sig, src, errs = None, "", []
    if state == "LIVE":
        df, src, errs = _bars_5m(c["symbol"], now_e.date().isoformat(), FMP_KEY)
        sig = itd.entry_signal(df, _live_price(c["symbol"], FMP_KEY), now_e.date(), now_e)
    o = vd.ep_order(c["plan"], sig)
    st.markdown(f"### {safe(vd.ep_headline(c['symbol'], state, sig))}")
    icon = "🔥" if c["setup"] == ep.SETUP_A else "🎯"
    st.caption(f"{icon} {safe(c['name'])} · {c['setup_label']} · 점수 {c['score']:.0f} · "
               f"{dt.datetime.now(KST):%H:%M:%S} KST 기준")
    m = st.columns(4)
    _metric(m[0], "진입가 (ORH 돌파 지정가)" if o["basis"] == "ORH" else "진입가 (검색 시점 지정가)",
            ai.usd(o["entry_lo"]), f"~ {ai.usd(o['entry_hi'])} 사이 지정가 · 그 위는 추격 금지")
    _metric(m[1], "보유 기간", "최대 3거래일", "3거래일 넘기면 전량 매도")
    m[2].metric("손절 가격", ai.usd(o["stop"]), f"-{o['stop_pct']:.1f}% (진입 상단 기준)")
    m[3].metric("매도 목표", ai.usd(o["t1"]), f"+{o['t1_pct']:.1f}% 에서 절반 매도")
    st.caption(f"나머지 절반: 손절을 본전으로 올리고 10일선 {ai.usd(c.get('sma10'))} 아래로 내려가면 매도.")
    if sig and sig.get("orh"):
        st.caption(f"진입 타이밍: {safe(sig['text'])} · 시가 범위 {ai.usd(sig['orl'])} ~ {ai.usd(sig['orh'])} · "
                   f"현재가 {ai.usd(_live_price(c['symbol'], FMP_KEY))} · 분봉 {src}")
    elif sig:
        st.caption(f"진입 타이밍: {safe(sig['text'])}" + (f" · 분봉 조회 실패: {safe(', '.join(errs))}" if errs else ""))


def candidate_card(c: dict, equity: float, risk_pct: float):
    p = c["plan"]
    with st.container(border=True):
        ep_order_block(c)
        note = vd.lod_note(p)
        if note:
            st.warning(safe(note))
        for w in c["warnings"]:
            if not w.startswith("장중 거래량은"):
                st.warning(safe(w))
        with st.expander("근거 보기 (갭·거래량·촉매·점수)"):
            m = st.columns(4)
            m[0].metric("검색 시점 현재가", ai.usd(c["price"]))
            m[1].metric("갭", f"{c['gap']:+.1f}%")
            m[2].metric("거래량 배수", f"{c['volx']:.1f}x" + (" (추정)" if c["projected"] else ""))
            m[3].metric("손익비", f"{p['rr']:.1f} : 1")
            cat = c.get("catalyst") or {}
            st.markdown(f"**촉매** — {safe(cat.get('text', '-'))}")
            st.caption(f"EP 발생일 {c['ep_date']} · 검색 시점 지정가 {ai.usd(p['entry_lo'])} ~ {ai.usd(p['entry_hi'])} · "
                       f"검색 시점 손절 {ai.usd(p['stop'])} · 1차 목표 {ai.usd(p['t1'])}")
            if c["setup"] == ep.SETUP_A and c.get("hold") is not None:
                run = c.get("prior_run")
                st.caption(f"갭 버팀: 당일 범위 상위 {float(c['hold']) * 100:.0f}% 위치 · "
                           + (f"갭 직전 60세션 {float(run):+.0f}%" if run is not None else "직전 60세션 데이터 없음"))
            info = []
            if c.get("float_turn") is not None:
                fl = c.get("float_shares")
                info.append(f"유통주식 회전율 {float(c['float_turn']):.1f}%"
                            + (f" (유통 {float(fl) / 1e6:,.0f}M주)" if fl else ""))
            sec = c.get("sector") or {}
            if sec:
                s_txt = f"업종 {sec.get('industry') or sec.get('sector')}"
                if sec.get("etf") and sec.get("etf_vs_open_pct") is not None:
                    s_txt += f" · {sec['etf']} 시가 대비 {float(sec['etf_vs_open_pct']):+.1f}%"
                peers = sec.get("peers") or []
                s_txt += f" · 동반 급등 {len(peers)}종목" + (f" ({', '.join(peers[:4])})" if peers else "")
                info.append(s_txt)
            if info:
                st.caption(safe(" · ".join(info)))
            for w in c["warnings"]:
                if w.startswith("장중 거래량은"):
                    st.caption(safe(w))

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


def _regime_line(reg) -> None:
    phase = regime_phase(reg)
    detail = (reg or {}).get("detail") or "SPY 일봉 조회 실패"
    if phase == "NORMAL":
        st.success(f"시장 필터: NORMAL · {detail} — 신규 진입 허용")
    elif phase == "PRESSURE":
        st.warning(f"시장 필터: PRESSURE · {detail} — 분산일 누적, 최상위 후보만")
    elif phase == "CORRECTION":
        st.warning(f"시장 필터: CORRECTION · {detail} — 조정장 주의 (돌파 승률이 낮은 구간, 엄선·소량 진입)")
    else:
        st.warning(f"시장 필터: 판정 불가 · {detail} — 국면 확인 전까지 보수적으로")


def tab_hunt():
    with st.sidebar:
        st.header("⚙️ 사냥 설정")
        equity = st.number_input("계좌 규모 (USD)", min_value=100.0, value=float(st.session_state.get("equity", 1000.0)),
                                 step=100.0)
        risk = st.number_input("1회 리스크 (% of 계좌)", min_value=0.1, max_value=2.0,
                               value=float(st.session_state.get("risk", 0.75)), step=0.05)
        st.session_state["equity"], st.session_state["risk"] = equity, risk

    st.markdown("### 🔥 당일 EP · 장중 검색 · 최대 3거래일 보유")
    head = st.columns([3, 1])
    do_scan = head[1].button("🔄 당일 EP 재검색", type="primary", width="stretch")
    body, tail = st.container(), st.container()
    with tail.expander("셋업 B 백필 (느림 · 선택)"):
        st.caption("서버 재시작 등으로 EP 기록이 없을 때, 우량 유동주 상위 N개의 최근 5일 EP 이력을 복원합니다.")
        n_uni = st.slider("스캔 종목 수", 100, 400, 250, 50)
        do_backfill = st.button("📚 백필 포함 재검색")

    if not FMP_KEY:
        head[0].error("FMP_API_KEY 시크릿이 없습니다.")
        return
    with body:
        _ep_desk(equity, risk, do_scan, do_backfill, n_uni, head[0])
    pullback_section(equity, risk)


def _ep_desk(equity: float, risk: float, do_scan: bool, do_backfill: bool, n_uni: int, head) -> None:
    if do_scan or do_backfill:
        bar = st.progress(0.0, text="사냥터 스캔 중...")
        import env_settings
        keys = env_settings.get_api_keys()
        alp = ((keys.get("ALPACA_API_KEY") or "").strip(), (keys.get("ALPACA_SECRET_KEY") or "").strip())
        pool = ep.scan_all(FMP_KEY, backfill=do_backfill, max_universe=n_uni, alpaca=alp,
                           progress=lambda d, t, s: bar.progress(min(d / max(t, 1), 1.0), text=f"{d}/{t} · {s}"))
        bar.empty()
        st.session_state["pool"] = pool
        _ledger_record(pool)
    elif "pool" not in st.session_state:
        pool = ep.load_pool()
        if ep.pool_is_current(pool):
            st.session_state["pool"] = pool

    pool = st.session_state.get("pool")
    if not pool:
        head.markdown("## ⏸️ 아직 검색 전")
        head.caption(safe(entry_badge()) + " · 오른쪽 [🔄 당일 EP 재검색]을 누르세요.")
        _regime_line(get_regime(FMP_KEY))
        return

    top = pool.get("top", [])
    now_e = dt.datetime.now(ET)
    state, minutes = ep.market_state(now_e)
    if top:
        head.markdown("## " + safe("✅ 매수 추천: " + ", ".join(c["symbol"] for c in top)))
        sess = dt.date.fromisoformat(pool["session"])
        head.caption(safe(vd.ep_when(state, minutes, session_day=sess, next_session=pb.entry_session(sess))))
    else:
        head.markdown("## " + vd.ep_none_line(pool.get("state") or state))
        if state == "LIVE":
            head.caption("장중 후보는 수시로 바뀝니다. 잠시 후 오른쪽 [🔄 당일 EP 재검색]을 다시 누르세요.")
        else:
            head.caption(safe(entry_badge()) + " · 본장 개장 30분 후 오른쪽 버튼으로 다시 검색하세요.")

    stale = pool.get("session") != ep.session_date().isoformat()
    st.caption(f"스캔 기준 세션 {pool['session']} · 실행 {pool['scanned_at_kst']} KST · "
               f"{pool['scanned']}건 검사 · A {pool['n_a']} / B {pool['n_b']}"
               + (" · ⚠️ 이전 세션 결과입니다. 재검색을 권장합니다." if stale else ""))
    mday = pool.get("market_day") or {}
    st.caption(f"시장: {regime_phase(get_regime(FMP_KEY))}"
               + (f" · 당일 방향 {safe(mday.get('detail'))}" if mday else "") + " · 자세한 내용은 아래 '시장 상태'")
    if pool.get("degraded"):
        fails = {k: v for k, v in (pool.get("rejects") or {}).items() if k in ep._DATA_FAIL}
        why = ", ".join(f"{k} {v}건" for k, v in fails.items())
        st.error(f"⚠️ 데이터 불완전: {pool['scanned']}건 중 {pool.get('data_fail', 0)}건"
                 f"({float(pool.get('fail_rate') or 0) * 100:.0f}%)을 데이터 문제로 판정하지 못했습니다"
                 + (f" ({safe(why)})" if why else "") + ". '후보 없음'을 믿지 말고 재검색하세요.")
    cf = pool.get("catalyst_fails") or {}
    if cf.get("earnings"):
        st.warning(f"실적 데이터 조회가 {cf['earnings']}건 실패했습니다. 실적 갭은 보도자료로 대신 확인하며 "
                   "EPS 비트 폭은 검증되지 않습니다. 사이드바 '📡 FMP 데이터 점검'으로 플랜 포함 여부를 확인하세요.")
    for c in top:
        candidate_card(c, equity, risk)
    with st.expander("시장 상태 · 탈락 사유"):
        _regime_line(get_regime(FMP_KEY))
        if mday:
            st.caption(f"당일 시장 방향: {safe(mday.get('detail'))}")
        for k, v in (pool.get("rejects") or {}).items():
            st.write(f"- {safe(k)}: {v}건")
        for e in pool.get("errors") or []:
            st.caption(f"처리 오류 {safe(e)}")


def _nth_session(start: dt.date, n: int) -> dt.date:
    """start 를 1일째로 셀 때 n 번째 거래일."""
    d = start
    for _ in range(n - 1):
        d = pb.entry_session(d)
    return d


def pullback_card(c: dict, pool: dict, equity: float, risk_pct: float) -> None:
    amt = pb.position_usd(equity, risk_pct)
    cfg = pb.PbCfg()
    ent = dt.date.fromisoformat(pool["entry_session"])
    last_day = _nth_session(ent, cfg.max_hold)
    with st.container(border=True):
        passed = vd.pb_entry_passed(ent)
        st.markdown(f"### ⏰ 진입 시점 지남: {c['symbol']}" if passed
                    else f"### ✅ 매수 추천: {c['symbol']} — {safe(vd.pb_when(ent))}")
        st.caption(f"{pb.LABEL} · 신호일 {pool['session']} 종가 기준 · {safe(vd.pb_when(ent))}")
        if passed:
            st.warning("이 추천의 진입 시점(본장 시가)은 이미 지났습니다. 지금 사면 백테스트와 다른 가격이니 "
                       "새로 사지 말고, 다음 신호는 장 마감 후 다시 검색하세요.")
        m = st.columns(4)
        _metric(m[0], "진입가 (본장 시가 매수)", f"{ai.usd(c['close'])} 근처",
                    "시가 그대로 매수 · 지정가 없음")
        _metric(m[1], "보유 기간", f"1~{cfg.max_hold}거래일", f"늦어도 {last_day:%m-%d} 종가 매도")
        _metric(m[2], "비상 손절", f"{ai.usd(c['close'] * (1 - pf.PB_EMERGENCY_STOP_PCT / 100))} 근처",
                f"체결가 -{pf.PB_EMERGENCY_STOP_PCT:.0f}% 토스 예약 · 금액 {ai.usd(amt, 0)} 이하")
        _metric(m[3], "매도 목표", f"종가 > 5일선", f"지금 5일선 {ai.usd(c['sma5'])} (매일 바뀜)")
        st.caption(f"짧은 손절이 없는 대신 한 번에 {ai.usd(amt, 0)} 이하만 삽니다. 백테스트 하위 5% 손실 "
                   f"-{cfg.tail_loss_pct:.0f}%가 나도 계좌 1회 리스크 {risk_pct:.2f}% 이내입니다. "
                   f"{PB_STOP_NOTE}")
        with st.expander("근거 보기 (RSI2·추세·백테스트)"):
            n = st.columns(4)
            n[0].metric("신호일 종가", ai.usd(c["close"]))
            n[1].metric("2일 하락", f"{c['drop_2d']:+.1f}%")
            n[2].metric("RSI(2)", f"{c['rsi2']:.1f}")
            n[3].metric("200일선", ai.usd(c["sma200"]))
            st.caption(f"60일 상승률 {c['r60']:+.0f}% (같은 날 후보 중 1위) · 50일선 {ai.usd(c['sma50'])} > 200일선 · "
                       "백테스트 최근 1년: 적중률 62~63%, 수수료 뺀 평균 +0.7~0.9%, 평균 보유 3.3일.")
            st.caption("수호 데스크에 등록할 때 전략을 '눌림 반등'으로 고르면 이 청산 규칙으로 감시합니다.")


def pullback_section(equity: float, risk_pct: float) -> None:
    st.divider()
    st.markdown("### 🔁 눌림 반등 · 장 마감 후 검색 · 하루 1개 · 1~5일 보유")
    head = st.columns([3, 1])
    do_pb = head[1].button("🔁 눌림 반등 검색", width="stretch", help="완결 일봉 · 약 1천 종목 · 몇 분 걸림")
    if do_pb:
        bar = st.progress(0.0, text="눌림 반등 스캔 중...")
        try:
            recent = lg.recent_pullbacks(_ledger_rows())
        except Exception:
            recent = {}
        pool = pb.scan(FMP_KEY, recent=recent,
                       progress=lambda d, t, s_: bar.progress(min(d / max(t, 1), 1.0), text=f"{d}/{t} · {s_}"))
        bar.empty()
        st.session_state["pb_pool"] = pool
        try:
            rows = _ledger_rows()
            if lg.record_pullback(rows, pool):
                _ledger_save(rows)
        except Exception as exc:  # noqa: BLE001
            st.caption(f"추천 원장 기록 실패: {safe(type(exc).__name__)}")
    elif "pb_pool" not in st.session_state:
        cached = pb.load_pool()
        if pb.pool_is_current(cached):
            st.session_state["pb_pool"] = cached
    pool = st.session_state.get("pb_pool")
    sig_day = pb.signal_session()
    hint = vd.pb_search_hint(pb.entry_session(sig_day) if pool and pool.get("session") == sig_day.isoformat()
                             else sig_day, pb.EOD_READY_HOUR)
    if not pool:
        head[0].markdown("## ⏸️ 아직 검색 전")
        head[0].caption(f"오른쪽 [🔁 눌림 반등 검색]을 누르세요. 마지막 완결일 {sig_day:%m-%d} 신호를 찾습니다.")
        return
    top = pool.get("top") or []
    if top:
        ent = dt.date.fromisoformat(pool["entry_session"])
        syms = ", ".join(c["symbol"] for c in top)
        if vd.pb_entry_passed(ent):
            head[0].markdown("## " + safe(f"⏰ 진입 시점 지남: {syms}"))
            head[0].caption(safe(vd.pb_when(ent)) + f" · 다음 신호: {hint}")
        else:
            head[0].markdown("## " + safe(f"✅ 매수 추천: {syms}"))
            head[0].caption("진입 시각: " + safe(vd.pb_when(ent)))
    else:
        head[0].markdown("## ⛔ 오늘은 추천 없음")
        head[0].caption(f"상승 추세 종목 중 2일 과매도로 눌린 종목이 없습니다. 다음 신호: {hint}.")
    stale = pool.get("session") != sig_day.isoformat()
    st.caption(f"신호일 {pool['session']} · 진입일 {pool['entry_session']} · 실행 {pool['scanned_at_kst']} KST · "
               f"{pool['scanned']}종목 검사 · 조건 통과 {pool['n_cands']}개"
               + (" · ⚠️ 이전 신호일 결과입니다. 다시 검색하세요." if stale else ""))
    if float(pool.get("fail_rate") or 0) > 0.2:
        st.error(f"⚠️ 데이터 불완전: {pool.get('data_fail', 0)}종목의 일봉을 받지 못했습니다. 잠시 후 다시 검색하세요.")
    for c in top:
        pullback_card(c, pool, equity, risk_pct)
    others = [o for o in pool.get("others") or [] if o["symbol"] not in {c["symbol"] for c in top}]
    if others:
        with st.expander("다음 순위 후보 (참고용, 검증 성적은 1위보다 낮음)"):
            st.dataframe([{"티커": o["symbol"], "60일 상승률": f"{o['r60']:+.0f}%", "RSI(2)": round(o["rsi2"], 1),
                           "종가": ai.usd(o["close"])} for o in others], hide_index=True, width="stretch")


# ============================================================================
# 추천 원장 (당일 적중률)
# ============================================================================
def _ledger_rows() -> list:
    from pathlib import Path
    import cloud_sync
    cloud_sync.pull_file_from_cloud(Path(lg.DEFAULT_PATH), lg.GIST_NAME)
    return lg.load()


def _ledger_save(rows: list) -> None:
    from pathlib import Path
    import cloud_sync
    lg.save(rows)
    cloud_sync.push_file_to_cloud(Path(lg.DEFAULT_PATH), lg.GIST_NAME)


def _ledger_record(pool: dict) -> None:
    try:
        rows = _ledger_rows()
        if lg.record(rows, pool):
            _ledger_save(rows)
    except Exception as exc:  # noqa: BLE001
        st.caption(f"추천 원장 기록 실패: {safe(type(exc).__name__)}")


def ledger_panel() -> None:
    st.subheader("📈 추천 적중률")
    st.caption("EP: 장중 첫 추천가 대비 그 세션 종가 (종가 > 추천가 = 적중). "
               "눌림 반등: 다음날 시가 진입 → 5일선 위 종가 또는 5거래일째 종가 청산, 수수료 0.2% 뺀 수익 > 0 = 적중.")
    rows = _ledger_rows()
    last = float(st.session_state.get("ledger_graded_at", 0) or 0)
    if FMP_KEY and time.time() - last > 600:
        st.session_state["ledger_graded_at"] = time.time()
        if lg.grade(rows, FMP_KEY):
            _ledger_save(rows)
    s = lg.stats(rows)
    cols = st.columns(4)
    for col, key, name in ((cols[0], "ALL", "EP 전체"), (cols[1], "A", "🔥 당일 EP"), (cols[2], "B", "🎯 EP 눌림목"),
                           (cols[3], pb.SETUP, "🔁 눌림 반등")):
        x = s[key]
        if x["n"]:
            col.metric(f"{name} 적중률 ({x['n']}건)", f"{x['win_rate']:.0f}%",
                       ("청산 평균" if key == pb.SETUP else "종가 평균")
                       + f" {x['avg_close']:+.2f}% · 고가 평균 {x['avg_up']:+.2f}%")
        else:
            col.metric(f"{name} 적중률", "-", f"채점 대기 {x['pending']}건")
    recent = [r for r in reversed(rows)][:20]
    if recent:
        table = []
        for r in recent:
            o = r.get("outcome") or {}
            if not o:
                res = "채점 대기"
            elif o.get("missing"):
                res = "일봉 없음"
            elif r.get("setup") == pb.SETUP:
                res = ("✅ " if o["win"] else "❌ ") + f"{o['ret_close_pct']:+.2f}% ({o['hold']}일 보유 · {o['exit_date']} 청산)"
            else:
                res = ("✅ " if o["win"] else "❌ ") + f"{o['ret_close_pct']:+.2f}% (고가 {o['max_up_pct']:+.2f}%)"
            table.append({"세션": r["session"], "티커": r["symbol"], "셋업": r["setup"],
                          "추천가": ai.usd(r["rec_price"]), "추천 시각(KST)": r.get("rec_kst") or "-",
                          "점수": r.get("score"), "결과": res})
        st.dataframe(table, hide_index=True, width="stretch")
    else:
        st.info("아직 기록된 추천이 없습니다. 사냥 데스크에서 장중 재검색 또는 눌림 반등 검색을 하면 기록됩니다.")


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


@st.cache_data(ttl=60, show_spinner=False)
def _agent_rows(token: str):
    return pf.fetch_agent_holdings(token)


def _agent_sync() -> str:
    """v4.5.7: 에이전트(텔레그램·Claude 입력) 보유 목록을 봇 포지션에 합친다. 상태 한 줄을 돌려준다."""
    try:
        token = str(st.secrets.get("AGENT_STORE_TOKEN") or "").strip()
    except Exception:
        token = ""
    token = token or os.environ.get("AGENT_STORE_TOKEN", "").strip()
    if not token:
        return "에이전트 목록 연결 안 됨 (AGENT_STORE_TOKEN 없음)"
    rows = _agent_rows(token)
    if rows is None:
        return "에이전트 목록 연결 안 됨 (읽기 실패)"
    merged, added, removed, changed = pf.merge_agent(st.session_state["positions"], rows)
    if changed:
        st.session_state["positions"] = merged
        note = " · ".join(x for x in (f"추가 {', '.join(added)}" if added else "",
                                      f"매도 반영 {', '.join(removed)}" if removed else "") if x)
        _persist(f"에이전트 목록 반영{(' (' + note + ')') if note else ''} · ")
    return f"에이전트 목록 연결됨 ({len(rows)}종목)"


def tab_guard():
    if "positions" not in st.session_state:
        with st.spinner("Gist 포트폴리오 동기화 중..."):
            pos, msg = pf.load_positions()
        st.session_state["positions"], st.session_state["sync_msg"] = pos, msg
    agent_msg = _agent_sync()
    st.caption(f"동기화 상태: {safe(st.session_state.get('sync_msg', ''))} · {safe(agent_msg)}")

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
        strat = st.radio("전략", ["EP (손절 3~3.5% · 3거래일)", "🔁 눌림 반등 (비상 손절 -15% · 5일선 위 또는 5거래일 청산)"],
                         horizontal=True, key="reg_strategy")
        is_pb = strat.startswith("🔁")
        r = st.columns([1.2, 1, 1, 1])
        r[0].text_input("티커 (입력 후 Enter → 시세 자동 조회)", key="reg_ticker", on_change=_on_ticker)
        r[1].number_input("평단가 (USD)", min_value=0.0, step=0.01, key="reg_avg", format="%.2f")
        qty = r[2].number_input("수량", min_value=0.0, step=1.0, value=1.0, key="reg_qty")
        sp = r[3].number_input("손절폭 %", min_value=pf.STOP_MIN, max_value=pf.STOP_MAX, value=pf.STOP_MAX,
                               step=0.1, key="reg_sp", disabled=is_pb)
        if st.session_state.get("reg_msg"):
            st.caption(safe(st.session_state["reg_msg"]))
        if st.button("원클릭 등록", type="primary"):
            p = pf.new_position(st.session_state.get("reg_ticker", ""), qty, st.session_state["reg_avg"], sp,
                                strategy=pf.PB if is_pb else "EP")
            if p:
                st.session_state["positions"].append(p)
                _persist(f"{p['ticker']} 등록 · ")
                st.rerun()
            else:
                st.error("티커·수량·평단가를 확인하세요.")

    positions = st.session_state["positions"]
    if not positions:
        st.info("등록된 포지션이 없습니다.")
        st.divider()
        ledger_panel()
        return
    marks = get_marks(tuple(p["ticker"] for p in positions), FMP_KEY) if FMP_KEY else {}
    rows = [pf.evaluate(p, marks.get(p["ticker"], {}).get("price"), marks.get(p["ticker"], {}).get("sma10"),
                        sma5=marks.get(p["ticker"], {}).get("sma5"), pb_target=marks.get(p["ticker"], {}).get("pb_target"))
            for p in positions]
    s = pf.summarize(rows)
    k = st.columns(4)
    k[0].metric("투입 원가", ai.usd(s["cost"]))
    k[1].metric("평가금액", ai.usd(s["value"]))
    k[2].metric("순손익 (수수료 0.2% 반영)", ai.usd(s["pnl"]), f"{s['pnl_pct']:+.2f}%")
    k[3].metric("보유 종목", f"{len(positions)}개")

    for i, (p, r) in enumerate(zip(positions, rows)):
        is_pb = p.get("strategy") == pf.PB
        with st.container(border=True):
            st.markdown(f"### {safe(vd.guard_headline(p['ticker'], r))}")
            st.caption(f"{'🔁 눌림 반등' if is_pb else '🔥 EP'} · 현재가 {ai.usd(r['price'])} · 수량 {p['qty']:g} · "
                       f"규칙 판정: {safe(r['reason'])}" + (f" · {safe(r['alert'])}" if r.get("alert") else ""))
            if is_pb:
                color, line = vd.pb_target_line(r["price"], r.get("target"))
                st.markdown(f"#### :{color}[{safe(line)}]")
                st.markdown(f"#### {safe(vd.pb_count_line(r['held_sessions'], pf.PB_MAX_HOLD, pf.pb_last_day(p.get('opened'))))}")
            m = st.columns(4)
            m[0].metric("진입가 (평단)", ai.usd(r["avg"]))
            if is_pb:
                _metric(m[1], "보유 기간", f"{r['held_sessions']}/{pf.PB_MAX_HOLD}거래일",
                            f"{pf.PB_MAX_HOLD}거래일째 종가에 전량 매도")
                _metric(m[2], "비상 손절", ai.usd(r["avg"] * (1 - pf.PB_EMERGENCY_STOP_PCT / 100)),
                        f"평단 -{pf.PB_EMERGENCY_STOP_PCT:.0f}% 토스 예약 (드문 폭락 보험)")
                _metric(m[3], "목표 매도가", ai.usd(r.get("target")), "이 가격 위로 마감하면 종가 매도 (매일 바뀜)")
            else:
                _metric(m[1], "보유 기간", f"{r['held_sessions']}/3거래일", "3거래일 넘기면 전량 매도")
                if r["tp1_done"]:
                    _metric(m[2], "손절 가격", ai.usd(r["stop"]), "본전 (수수료 포함)")
                    _metric(m[3], "매도 목표", "10일선 이탈", f"10일선 {ai.usd(r['sma10'])} 아래면 잔량 매도")
                else:
                    m[2].metric("손절 가격", ai.usd(r["stop"]), f"-{p['stop_pct']:.1f}%")
                    m[3].metric("매도 목표", ai.usd(r["tp1"]), "+5% 에서 절반 매도")
            if r["signal"] == "TIME_EXIT":
                st.error(f"🚨 사이렌 — {safe(r['reason'])} 수익률과 관계없이 전량 청산하라.")
            with st.expander("관리 (손절폭 · 익절 체결 · 삭제)"):
                b = st.columns([1.2, 1.2, 1.2, 3])
                b[0].number_input("손절폭 %", min_value=pf.STOP_MIN, max_value=pf.STOP_MAX, value=float(p["stop_pct"]),
                                  step=0.1, key=f"sp_{i}", on_change=_on_stop_change, args=(i,),
                                  disabled=r["tp1_done"] or is_pb)
                if b[1].button("1차 익절 체결 처리", key=f"tp1_{i}", disabled=r["tp1_done"] or is_pb):
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
            "TIME_EXIT": "🚨 [TIME_EXIT 보유 기한 초과]",
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
    st.divider()
    ledger_panel()


def tab_agency() -> None:
    """입력 → 파이프라인 → 보고서. 사냥/수호 로직은 여기서 돌리지 않는다."""
    from agency.pipeline import run_agency
    from agency.report_ui import load_last_report, render_report, save_last_report

    st.subheader("🏢 에이전시 보고서")
    extra = st.text_input("추가 종목 (쉼표, 최대 3)", key="agency_manual")
    gen = st.button("📑 오늘의 보고서 생성", type="primary")

    if "agency_report" not in st.session_state:
        last = load_last_report()
        if last:
            st.session_state["agency_report"] = last

    if gen:
        if "positions" not in st.session_state:
            pos, msg = pf.load_positions()
            st.session_state["positions"] = pos
            st.session_state.setdefault("sync_msg", msg)
        positions = st.session_state.get("positions") or []
        equity = float(st.session_state.get("equity", 1000.0))
        risk = float(st.session_state.get("risk", 0.75))
        pool = st.session_state.get("pool") or ep.load_pool() or {}
        if pool and "pool" not in st.session_state:
            st.session_state["pool"] = pool
        regime_raw = get_regime(FMP_KEY) if FMP_KEY else None
        phase = regime_phase(regime_raw)
        state, _minutes = ep.market_state()
        sess = ep.session_date().isoformat()  # NYSE session_date. 달력 날짜 아님.
        tickers = tuple(str(p.get("ticker") or "") for p in positions if p.get("ticker"))
        marks = get_marks(tickers, FMP_KEY) if (FMP_KEY and tickers) else {}
        today_new = sum(
            1 for v in (st.session_state.get("orders") or {}).values()
            if isinstance(v, dict) and v.get("verdict") in ("APPROVE", "CONDITIONAL")
        )
        with st.status("① 후보 읽기", expanded=True) as stt:
            def _prog(label: str) -> None:
                stt.update(label=str(label))
            try:
                report = run_agency(
                    pool=pool,
                    manual_raw=extra,
                    positions=positions,
                    marks=marks,
                    account_usd=equity,
                    risk_pct=risk,
                    regime=phase,
                    market_state=state,
                    session_date=sess,
                    api_key=FMP_KEY or "",
                    today_new_count=today_new,
                    progress=_prog,
                )
            except Exception as exc:
                stt.update(label="실패", state="error")
                st.error(f"보고서 생성 실패: {type(exc).__name__}")
                return
            save_last_report(report)
            st.session_state["agency_report"] = report
            stt.update(label="완료", state="complete")

    report = st.session_state.get("agency_report")
    if report:
        render_report(report, today_session=ep.session_date().isoformat())
    else:
        st.info("버튼을 누르면 오늘 사냥 후보로 일일 보고서를 만듭니다. 주문은 보내지 않습니다.")


# ============================================================================
# 엔트리
# ============================================================================
def _sidebar_keys() -> None:
    import env_settings

    keys = env_settings.get_api_keys()
    names = ("DEEPSEEK_API_KEY", "GROQ_API_KEY", "FMP_API_KEY")
    with st.sidebar:
        st.caption(f"🏷️ {version_label()}")
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
        choices = ["DeepSeek 총괄 CIO", "Groq 실무 참모"]
        if st.session_state.get("selected_model") not in choices:
            old = st.session_state.get("secretary_model_label")
            st.session_state["selected_model"] = choices[1] if old == "Groq" else choices[0]
        st.selectbox("비서 모델", choices, key="selected_model")
        picked = st.session_state["selected_model"]
        st.session_state["ai_model"] = "GROQ" if str(picked).startswith("Groq") else "DEEPSEEK"
        if deep:
            st.caption("AI 엔진: DEEPSEEK (deepseek-chat) 🟢")
        else:
            st.caption(f"AI 엔진: {ai.provider_label()}")
        st.divider()
        st.subheader("🔌 분봉 경로 점검")
        day = ep.session_date()
        st.caption(f"SPY {day.isoformat()} 세션 5분봉을 FMP → 알파카 → 야후 각각에서 받아 봅니다. 장이 닫혀 있어도 됩니다.")
        if st.button("분봉 수신 점검", key="probe_5m_btn", width="stretch"):
            keys = env_settings.get_api_keys()
            alp = ((keys.get("ALPACA_API_KEY") or "").strip(), (keys.get("ALPACA_SECRET_KEY") or "").strip())
            with st.spinner("분봉 수신 점검 중..."):
                st.session_state["probe_5m"] = (day.isoformat(), itd.probe("SPY", day, FMP_KEY, alp))
        got = st.session_state.get("probe_5m")
        if got:
            pday, rows = got
            first_ok = next((r["name"] for r in rows if r["ok"]), None)
            for r in rows:
                if r["ok"]:
                    st.caption(f"🟢 {r['name']}: {r['bars']}봉 ({r['first']}~{r['last']} ET)")
                else:
                    st.caption(f"🔴 {r['name']}: {safe(r['error'])}")
            if first_ok:
                st.success(f"장중 진입 타이밍은 {first_ok} 분봉을 씁니다 ({pday} 기준).")
            else:
                st.error("세 공급자 모두 분봉을 못 받았습니다. 진입 타이밍 칸에 '판정 불가'가 표시됩니다.")
        st.divider()
        st.subheader("📡 FMP 데이터 점검")
        st.caption("AAPL로 촉매·유통주식·섹터·일괄 시세 엔드포인트가 지금 플랜에서 열리는지 확인합니다.")
        if st.button("FMP 데이터 점검", key="probe_fmp_btn", width="stretch"):
            with st.spinner("FMP 엔드포인트 점검 중..."):
                st.session_state["probe_fmp"] = fc.probe_endpoints(FMP_KEY, "AAPL")
        probe_rows = st.session_state.get("probe_fmp") or []
        for r in probe_rows:
            color, text = vd.probe_status(r, probe_rows)
            st.caption(f"{color} {safe(text)}")
        if probe_rows:
            level, text = vd.probe_summary(probe_rows)
            {"ok": st.success, "warn": st.warning, "bad": st.error}[level](text)
            st.caption("🟢 정상 · 🟡 플랜 제한, 대체 경로로 정상 · 🟠 플랜 제한, 그 정보 없이 판정 · 🔴 실제 장애")


auth_gate()
import env_settings
env_settings.get_api_keys()
FMP_KEY = get_secret("FMP_API_KEY", "FMP_KEY")
_sidebar_keys()
st.title(f"🎯 Cloud Cockpit {APP_VERSION}")
st.caption(f"🏷️ {version_label()}")
clock_header()
t1, t2, t3 = st.tabs(["🎯 사냥 데스크", "🛡️ 수호 데스크", "🏢 에이전시"])
with t1:
    tab_hunt()
with t2:
    tab_guard()
with t3:
    tab_agency()
