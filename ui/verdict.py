"""카드 맨 위 '사라/말라' 한 줄과 4칸(진입가·보유 기간·손절 가격·매도 목표) 계산 (v4.5.1).

표시 전용이다. 추천 선별(점수·필터)은 건드리지 않는다. 가격은 엔진이 만든 계획의 % 규칙을 그대로 쓴다.
Streamlit 에 의존하지 않는다.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
_WD = "월화수목금토일"


def kst_at(day: dt.date, t_et: dt.time) -> dt.datetime:
    """미국 동부 day 의 t_et 시각을 KST 로 (서머타임 자동 반영)."""
    return dt.datetime.combine(day, t_et, ET).astimezone(KST)


def fmt_kst(t: dt.datetime, now: dt.datetime | None = None) -> str:
    """'오늘 밤 22:45' / '내일 밤 22:30' / '10-21(수) 밤 22:30'. 05~18시는 '낮/아침'."""
    t = t.astimezone(KST)
    now = (now or dt.datetime.now(KST)).astimezone(KST)
    part = "밤" if t.hour >= 18 or t.hour < 5 else ("아침" if t.hour < 12 else "낮")
    days = (t.date() - now.date()).days
    head = {0: "오늘", 1: "내일"}.get(days, f"{t:%m-%d}({_WD[t.weekday()]})")
    return f"{head} {part} {t:%H:%M}"


# ----------------------------------------------------------------------------
# 셋업 A/B (EP)
# ----------------------------------------------------------------------------
def ep_order(plan: dict, sig: dict | None = None) -> dict:
    """진입가 범위와, 그 진입 상단 기준의 손절·1차 목표.

    장중에 시가 범위 고점(ORH)이 잡혔으면 ORH +0.1% ~ +1.0% 를 진입가로 쓴다. 손절·목표는
    엔진 계획의 % (손절 2.5~3.5%, 1차 5~7%) 를 이 진입 상단에 그대로 적용한다."""
    stop_pct, t1_pct = float(plan["stop_pct"]), float(plan["t1_pct"])
    if sig and sig.get("entry_lo") and sig.get("entry_hi"):
        lo, hi, basis = float(sig["entry_lo"]), float(sig["entry_hi"]), "ORH"
    else:
        lo, hi, basis = float(plan["entry_lo"]), float(plan["entry_hi"]), "SCAN"
    return {"entry_lo": lo, "entry_hi": hi, "basis": basis, "stop_pct": stop_pct, "t1_pct": t1_pct,
            "stop": hi * (1 - stop_pct / 100.0), "t1": hi * (1 + t1_pct / 100.0)}


# entry_signal 상태 → (카드 맨 위 한 줄, 지금 사도 되는가)
_SIG_LINE = {
    "GO": ("지금 진입 가능 (ORH 거래량 동반 돌파)", True),
    "WEAK": ("돌파했지만 거래량 약함 · 절반 수량만", True),
    "WAIT": ("ORH 돌파 시 진입 · 아직 기다리세요", False),
    "WAIT_OR": ("시가 범위 형성 중 · ET 09:45 이후 판단", False),
    "CHASE": ("추격 금지 · ORH 근처로 눌릴 때만", False),
    "FAIL": ("돌파 실패 · 다시 ORH 위로 올 때만", False),
    "BREAK": ("시가 범위 저점 이탈 · 오늘 진입 금지", False),
    "NO_DATA": ("분봉 없음 · 진입가 범위 안에서만", False),
}


def ep_headline(sym: str, state: str, sig: dict | None) -> str:
    """'✅ 매수 추천: IRON — ORH 돌파 시 진입'. 장 밖이면 다음 본장 판단 안내."""
    if state != "LIVE":
        return f"✅ 매수 추천: {sym} — 다음 본장에서 다시 검색해 확인"
    if not sig:
        return f"✅ 매수 추천: {sym}"
    text, _go = _SIG_LINE.get(sig.get("state"), ("", False))
    if sig.get("state") == "BREAK":
        return f"⛔ {sym} — {text}"
    return f"✅ 매수 추천: {sym} — {text}" if text else f"✅ 매수 추천: {sym}"


def ep_when(state: str, minutes: int | None, now: dt.datetime | None = None, session_day: dt.date | None = None,
            next_session: dt.date | None = None) -> str:
    """진입 시각 안내 한 줄 (KST). 장중: 지금, 진입 타이밍이 ✅ 일 때. 장 밖: 다음 본장 개장 15분 후."""
    now = (now or dt.datetime.now(KST)).astimezone(KST)
    if state == "LIVE" and session_day:
        close = kst_at(session_day, dt.time(16, 0))
        return f"진입 시각: 지금, 카드의 진입 타이밍이 ✅일 때 (본장 마감 {fmt_kst(close, now)})"
    day = next_session or session_day
    if not day:
        return ""
    return (f"진입 시각: {fmt_kst(kst_at(day, dt.time(9, 45)), now)} 이후 (개장 15분 후) · "
            f"후보 판정은 {fmt_kst(kst_at(day, dt.time(10, 0)), now)}부터")


def ep_none_line(state: str) -> str:
    return "⛔ 지금은 추천 없음" if state == "LIVE" else "⛔ 오늘은 추천 없음"


def lod_note(plan: dict) -> str | None:
    """당일 저가가 -3.5% 손절보다 아래일 때의 경고. '조였다'가 아니라 무슨 뜻인지 그대로 쓴다."""
    if not plan.get("lod_cap_exceeded"):
        return None
    dist = float(plan.get("struct_stop_dist_pct") or 0)
    return (f"오늘 저가는 검색 시점 진입가보다 {dist:.1f}% 아래인데 손절은 규칙상 최대 -{float(plan['stop_pct']):.1f}%입니다. "
            "손절이 오늘 흔들린 범위 안에 있어 노이즈에 걸릴 수 있으니, 평소보다 수량을 줄이세요.")


# ----------------------------------------------------------------------------
# 눌림 반등 (PB)
# ----------------------------------------------------------------------------
def pb_when(entry_day: dt.date, now: dt.datetime | None = None) -> str:
    """'오늘 밤 22:30 본장 시가 (미국 10-20 09:30 ET)'. 이미 개장했으면 지났다고 쓴다."""
    t = kst_at(entry_day, dt.time(9, 30))
    text = f"{fmt_kst(t, now)} 본장 시가 (미국 {entry_day:%m-%d} 09:30 ET)"
    if pb_entry_passed(entry_day, now):
        return f"진입 시각 지남: {text}였습니다"
    return text


def pb_entry_passed(entry_day: dt.date, now: dt.datetime | None = None) -> bool:
    now = (now or dt.datetime.now(KST)).astimezone(KST)
    return now >= kst_at(entry_day, dt.time(9, 30))


def pb_search_hint(signal_day: dt.date, ready_hour: int, now: dt.datetime | None = None) -> str:
    t = kst_at(signal_day, dt.time(ready_hour, 0))
    return f"{fmt_kst(t, now)} 이후 검색 (미국 장 마감 2시간 뒤)"


# ----------------------------------------------------------------------------
# 수호 데스크
# ----------------------------------------------------------------------------
_GUARD = {
    "HOLD": "🟢 보유 유지",
    "TRIM": "🟡 50% 익절하세요",
    "EXIT": "🔴 지금 매도하세요",
    "TIME_EXIT": "🚨 보유 기한 · 전량 매도하세요",
    "NO_DATA": "⚪ 시세 조회 실패 · 직접 확인",
}


def guard_headline(ticker: str, r: dict) -> str:
    head = _GUARD.get(r.get("signal"), "⚪ 판정 대기")
    net = r.get("net_pct")
    return f"{head}: {ticker}" + (f" (순손익 {net:+.2f}%)" if net is not None else "")


# ----------------------------------------------------------------------------
# 사이드바 FMP 점검
# ----------------------------------------------------------------------------
# 막혀도 봇이 다른 경로로 같은 일을 하는 엔드포인트: {막힌 경로: 대체 경로}
FALLBACK = {"batch-quote": "quote", "news/press-releases": "news/stock"}
_FALLBACK_NAME = {"quote": "종목별 시세", "news/stock": "종목 뉴스"}
PLAN_LIMIT = (402, 403)


def probe_status(row: dict, rows: list) -> tuple[str, str]:
    """(색, 문구). 초록 = 정상, 노랑 = 플랜 제한이지만 대체 경로 정상, 주황 = 플랜 제한·대체 없음, 빨강 = 장애."""
    if row.get("ok"):
        return "🟢", f"{row['label']}: {row['rows']}건"
    status = row.get("status")
    if status in PLAN_LIMIT:
        alt = FALLBACK.get(row.get("path"))
        alt_ok = alt and any(r.get("path") == alt and r.get("ok") for r in rows)
        if alt_ok:
            return "🟡", f"{row['label']}: 플랜 제한(HTTP {status}) · {_FALLBACK_NAME[alt]}로 대체 중 — 정상"
        return "🟠", f"{row['label']}: 플랜 제한(HTTP {status}) · 대체 경로 없음 — 이 정보 없이 판정"
    if status == 200:
        return "🔴", f"{row['label']}: 응답은 왔지만 데이터 없음"
    return "🔴", f"{row['label']}: " + (f"HTTP {status} 장애" if status else "연결 실패")


def probe_summary(rows: list) -> tuple[str, str]:
    """('ok'|'warn'|'bad', 문구)."""
    colors = [probe_status(r, rows)[0] for r in rows]
    if "🔴" in colors:
        return "bad", f"장애 {colors.count('🔴')}건: 빨간 항목은 실제 조회 실패입니다. 잠시 후 다시 점검하세요."
    if "🟠" in colors:
        return "warn", "플랜에 없는 항목이 있어 그 정보 없이 판정합니다. 장애는 아닙니다."
    return "ok", "정상: 노란 항목은 Starter 플랜 제한이며 봇이 대체 경로로 같은 데이터를 받습니다."
