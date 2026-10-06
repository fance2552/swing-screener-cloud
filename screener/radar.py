"""Strike radar: regime, volume-confirmed breakout, pocket pivot, Toss guide."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import config

NEAR_PCT = 1.5
_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")
_RTH_MINUTES = 390.0
_PRE_START_MIN = 4 * 60
_RTH_START_MIN = 9 * 60 + 30
_RTH_END_MIN = 16 * 60
_AH_END_MIN = 20 * 60
_FIRE_LOCK_MIN = 15.0

# NYSE / Nasdaq full-day closures. America/New_York DST is applied by zoneinfo;
# these dates are calendar dates in ET, not KST.
_US_MARKET_HOLIDAYS = frozenset({
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
})


def now_et(now: datetime | None = None) -> datetime:
    """Always return an aware America/New_York datetime. DST is zoneinfo's job."""
    if now is None:
        return datetime.now(_ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=_ET)
    return now.astimezone(_ET)


def now_kst(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(_KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=_KST)
    return now.astimezone(_KST)


def is_us_market_holiday(d: date | None = None) -> bool:
    return (d or now_et().date()) in _US_MARKET_HOLIDAYS


def _session_parts(ts: datetime) -> tuple[str, str, str]:
    if ts.weekday() >= 5 or is_us_market_holiday(ts.date()):
        return "CLOSED", "💤 미국장 휴장 (Closed)", "sess-CLOSED"
    minutes = ts.hour * 60 + ts.minute
    if _PRE_START_MIN <= minutes < _RTH_START_MIN:
        return "PRE", "🌙 프리마켓 (Pre-Market)", "sess-PRE"
    if _RTH_START_MIN <= minutes < _RTH_END_MIN:
        return "RTH", "☀️ 정규장 (Regular Market)", "sess-RTH"
    if _RTH_END_MIN <= minutes < _AH_END_MIN:
        return "AH", "🌆 애프터마켓 (After-Market)", "sess-AH"
    return "CLOSED", "💤 미국장 휴장 (Closed)", "sess-CLOSED"


def us_session(now: datetime | None = None) -> dict[str, str]:
    """Canonical US cash session. Input tz is converted to ET. Never use KST wall clock."""
    ts = now_et(now)
    key, badge, css = _session_parts(ts)
    return {
        "key": key,
        "badge": badge,
        "css": css,
        "et_clock": ts.strftime("%H:%M:%S"),
        "et_date": ts.date().isoformat(),
        "clock": ts.strftime("%H:%M"),
        "label": f"{badge} · {ts.strftime('%H:%M')} ET",
    }


def rth_elapsed_minutes(now: datetime | None = None) -> float:
    """Minutes since 09:30 ET. Negative before the open. 0 on non-RTH days before open."""
    ts = now_et(now)
    return (
        ts.hour * 60
        + ts.minute
        + ts.second / 60.0
        + ts.microsecond / 60_000_000.0
        - float(_RTH_START_MIN)
    )


def session_allows_fire_alert(now: datetime | None = None) -> bool:
    """True only in RTH after 09:45:00.000 ET. Premarket / first 15 minutes / weekend / holiday = False."""
    ts = now_et(now)
    key, _badge, _css = _session_parts(ts)
    if key != "RTH":
        return False
    return rth_elapsed_minutes(ts) >= _FIRE_LOCK_MIN


def fire_unlock_time_label(now: datetime | None = None) -> str:
    """09:45 ET를 현재 미국의 서머타임(DST) 상태에 맞춰 KST(한국시간) 문자열로 변환한다.
    - 서머타임 적용 중: '밤 10:45' (22:45)
    - 서머타임 해제 후: '밤 11:45' (23:45)
    화면 표시 전용. 사격 판정은 session_allows_fire_alert()가 ET로 한다.
    다음 해제가 일어날 거래일의 09:45 ET로 계산해야 DST 전환 주말에도 맞는다.
    """
    ts = now_et(now)
    day = ts.date()
    if ts.hour * 60 + ts.minute >= _RTH_START_MIN + _FIRE_LOCK_MIN:
        day += timedelta(days=1)
    while day.weekday() >= 5 or is_us_market_holiday(day):
        day += timedelta(days=1)
    unlock_et = datetime(day.year, day.month, day.day, 9, 45, tzinfo=_ET)
    unlock_kst = unlock_et.astimezone(_KST)
    return f"밤 {unlock_kst.strftime('%I:%M').lstrip('0')}"


def is_rth_open_chime_window(now: datetime | None = None) -> bool:
    """First five seconds of 09:30 ET on a live RTH day. DST-safe."""
    ts = now_et(now)
    key, _badge, _css = _session_parts(ts)
    if key != "RTH":
        return False
    return ts.hour == 9 and ts.minute == 30 and ts.second <= 5


def calculate_sri(score: float, dist_pct: float, lam: float = 0.25) -> float:
    """Strike readiness. Near the pivot keeps the score. Distance decays it."""
    try:
        s = float(score or 0.0)
        d = abs(float(dist_pct or 0.0))
        return round(s * math.exp(-lam * d), 2)
    except Exception:  # noqa: BLE001
        return 0.0


def dist_pct(price: float, pivot: float) -> float:
    """((피봇돌파가 - 현재가) / 피봇돌파가) * 100. Through-pivot is negative."""
    if pivot <= 0:
        return 0.0
    return ((pivot - price) / pivot) * 100.0


def session_elapsed_minutes(now: datetime | None = None) -> tuple[str, float]:
    """US cash clock. Returns (PRE|RTH|AH|CLOSED, minutes since 09:30, capped at 390)."""
    ts = now_et(now)
    key, _badge, _css = _session_parts(ts)
    if key == "CLOSED":
        return "CLOSED", _RTH_MINUTES
    if key == "PRE":
        return "PRE", 0.0
    if key == "AH":
        return "AH", _RTH_MINUTES
    return "RTH", min(max(rth_elapsed_minutes(ts), 0.0), _RTH_MINUTES)


def check_breakout_volume(
    current_volume: float,
    avg_volume_50d: float,
    now: datetime | None = None,
) -> bool | None:
    """U-shaped full-day projection versus the 50-day average.

    None means premarket or the first 15 minutes: volume is not confirmed yet.
    """
    phase, elapsed = session_elapsed_minutes(now)
    if phase == "PRE" or (phase == "RTH" and elapsed < 15.0):
        return None
    avg = float(avg_volume_50d or 0.0)
    vol = float(current_volume or 0.0)
    if avg <= 0 or vol <= 0:
        return False
    if elapsed < 30.0:
        factor = 0.55 * (_RTH_MINUTES / max(elapsed, 1.0))
    elif elapsed < 60.0:
        factor = 0.75 * (_RTH_MINUTES / max(elapsed, 1.0))
    else:
        factor = _RTH_MINUTES / max(elapsed, 1.0)
    projected = vol * factor
    return projected >= avg * config.PIVOT_VOLUME_MULT


def regime_state(regime: Any) -> str:
    """Map the shared regime dict onto NORMAL / PRESSURE / CORRECTION."""
    if isinstance(regime, dict):
        raw = str(regime.get("state") or regime.get("label") or "").upper()
    else:
        raw = str(regime or "").upper()
    if raw in {"CORRECTION", "RISK-OFF", "RED"}:
        return "CORRECTION"
    if raw in {"PRESSURE", "CAUTION"}:
        return "PRESSURE"
    return "NORMAL"


def classify_signal(
    price: float,
    pivot: float,
    *,
    regime: Any = "NORMAL",
    is_pocket_pivot: bool = False,
    current_volume: float = 0.0,
    avg_volume_50d: float = 0.0,
    score: float = 0.0,
    now: datetime | None = None,
) -> tuple[str, str, float]:
    """Shot gate. Correction allows a shot only for a score of 85 or more with volume."""
    if pivot <= 0:
        return "⏳ 눌림 관망 (반등 대기)", "반등 대기", 2.0
    state = regime_state(regime)
    distance = (price / pivot - 1.0) * 100.0
    if distance > 3.5:
        return f"🚫 추격 금지 (과열 +{distance:.1f}%)", f"과열 (+{distance:.1f}%)", 3.0
    if distance < 0.0:
        if is_pocket_pivot and (state != "CORRECTION" or score >= 85.0):
            return "💎 포켓 피봇 (선행 매집)", "피봇 하단 선행 매집", 1.5
        return "⏳ 눌림 관망 (반등 대기)", "반등 대기", 2.0
    vol_status = check_breakout_volume(current_volume, avg_volume_50d, now=now)
    sweet = f"${pivot:.2f} ~ ${pivot * 1.035:.2f}"
    if vol_status is None:
        if state == "CORRECTION" and score < 85.0:
            return "⏳ 눌림 관망 (조정장: 85점 이상 대기)", "조정장 엄격 대기", 2.0
        return "🌙 장전 돌파 (개장 후 확증 필수)", f"장전 포착 {sweet}", 1.2
    if state == "CORRECTION":
        if score < 85.0:
            return "⏳ 눌림 관망 (조정장: 85점 이상 대기)", "조정장 엄격 대기", 2.0
        if vol_status is True:
            return "🎯 지금 사격! (조정장 엄선)", sweet, 1.0
        return "🟡 저볼륨 돌파 (거래량 관찰)", "거래량 확증 대기", 1.8
    if vol_status is True:
        return "🎯 지금 사격!", sweet, 1.0
    return "🟡 저볼륨 돌파 (거래량 관찰)", "거래량 확증 대기", 1.8


def fire_signal(price: float, pivot: float, **kwargs: Any) -> tuple[str, str]:
    """Signal text plus the sweet-spot line. Extra kwargs feed classify_signal."""
    sig, guide, _pri = classify_signal(price, pivot, **kwargs)
    return sig, guide


def status_text(price: float, pivot: float, distance: float | None = None, **kwargs: Any) -> str:
    _ = distance
    sig, _guide = fire_signal(price, pivot, **kwargs)
    return sig


def shot_priority(status: str) -> float:
    """🎯 1, 🌙 1.2, 💎 1.5, 🟡 1.8, ⏳ 2, 🚫 3, ⏸️ 4."""
    text = str(status or "")
    if text.startswith("🎯"):
        return 1.0
    if text.startswith("🌙"):
        return 1.2
    if text.startswith("💎"):
        return 1.5
    if text.startswith("🟡"):
        return 1.8
    if text.startswith("⏳"):
        return 2.0
    if text.startswith("🚫"):
        return 3.0
    if text.startswith("⏸"):
        return 4.0
    return 3.0


def rank_targets(
    rows: list[dict[str, Any]],
    regime: Any = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Priority, then strike readiness, then absolute distance to the pivot."""
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        price = float(row.get("price") or 0.0)
        pivot = float(row.get("pivot") or 0.0)
        gap = dist_pct(price, pivot)
        sig, sweet, pri = classify_signal(
            price,
            pivot,
            regime=regime if regime is not None else "NORMAL",
            is_pocket_pivot=bool(row.get("is_pocket_pivot")),
            current_volume=float(row.get("day_volume") or 0.0),
            avg_volume_50d=float(row.get("avg_volume_50d") or 0.0),
            score=float(row.get("score") or 0.0),
            now=now,
        )
        row["dist_pct"] = gap
        row["status"] = sig
        row["sweet"] = sweet
        row["_priority"] = pri
        row["sri"] = calculate_sri(row.get("score"), gap)
        out.append(row)
    out.sort(key=lambda r: (float(r["_priority"]), -float(r.get("sri") or 0.0), abs(float(r["dist_pct"]))))
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


def kst_session(now: datetime | None = None) -> dict[str, str]:
    """UI clock helper. Session key is ET. Display clock is KST."""
    ts_kst = now_kst(now)
    info = us_session(ts_kst)
    clock = ts_kst.strftime("%H:%M")
    return {
        "key": info["key"],
        "badge": info["badge"],
        "css": info["css"],
        "clock": clock,
        "et_clock": info["et_clock"],
        "et_date": info["et_date"],
        "label": f"{info['badge']} · {clock} KST",
    }


def toss_guide(row: dict[str, Any]) -> dict[str, str]:
    """Two-line Toss path. Uses the ranked status so volume and regime are not recomputed blind."""
    ticker = str(row.get("ticker") or "")
    price = float(row.get("price") or 0.0)
    pivot = float(row.get("pivot") or 0.0)
    stop = float(row.get("stop") or 0.0)
    gap = dist_pct(price, pivot)
    stop_pct = (stop / price - 1.0) * 100.0 if price > 0 else 0.0
    sig = str(row.get("status") or "")
    sweet = str(row.get("sweet") or "")
    if not sig:
        sig, sweet = fire_signal(
            price,
            pivot,
            is_pocket_pivot=bool(row.get("is_pocket_pivot")),
            current_volume=float(row.get("day_volume") or 0.0),
            avg_volume_50d=float(row.get("avg_volume_50d") or 0.0),
            score=float(row.get("score") or 0.0),
        )
    line_px = f"• 시세: 현재가 ${price:.2f} | 피봇 ${pivot:.2f} | {sweet} | 손절 ${stop:.2f} ({stop_pct:.1f}%)"
    if sig.startswith("⏸"):
        title = f"⏸️ [{ticker} 시장국면: 신규진입 보류]"
        line1 = line_px
        line2 = "• 행동: SPY 조정. 신규 예약주문 금지. 보유만 수호."
        kind = "halt"
    elif sig.startswith("🚫"):
        title = f"🚫 [{ticker} 추격 금지: 과열 구간]"
        line1 = line_px
        line2 = "• 행동: 추격 매수 금지. 피봇 근처 눌림만 조건주문."
        kind = "chase"
    elif sig.startswith("🎯"):
        title = f"🎯 [{ticker} 지금 사격: 엄선 대장주]"
        line1 = f"• 시세: 현재가 ${price:.2f} | 진입 {sweet} | 손절 ${stop:.2f} ({stop_pct:.1f}%)"
        if "조정장" in sig:
            line2 = "• 행동: 토스 [일반주문 ➔ 지정가] (⚠️ 조정장 리스크: 평소 50% 비중 진입 권장) ➔ 체결 즉시 조건주문 손절가 설정"
        else:
            line2 = "• 행동: 토스 [일반주문 ➔ 지정가 매수] ➔ 체결 즉시 조건주문 손절가 설정"
        kind = "go"
    elif sig.startswith("🌙"):
        title = f"🌙 [{ticker} 장전 돌파: 정규장 확증 대기]"
        line1 = f"• 시세: 현재가 ${price:.2f} | 진입 {sweet} | 손절 ${stop:.2f} ({stop_pct:.1f}%)"
        line2 = "• 행동: 프리장 돌파 확인! 토스 [지정가 예약]만 걸고, 10시 반 개장 후 거래량 폭발 시 체결 확인"
        kind = "wait"
    elif sig.startswith("💎"):
        title = f"💎 [{ticker} 포켓 피봇: 선행 매집]"
        line1 = (
            f"• 시세: 현재가 ${price:.2f} (피봇 ${pivot:.2f} 까지 {max(gap, 0):.2f}%) | "
            f"손절 ${stop:.2f} ({stop_pct:.1f}%)"
        )
        line2 = f"• 행동: 피봇 ${pivot:.2f} 예약 지정가. 돌파 거래량은 아직 없다."
        kind = "pocket"
    elif sig.startswith("🟡"):
        title = f"🟡 [{ticker} 저볼륨 돌파: 거래량 관찰]"
        line1 = line_px
        line2 = "• 행동: 가격은 피봇 위다. 50일 평균의 1.4배가 안 되면 사격하지 마라."
        kind = "soft"
    else:
        title = f"⏳ [{ticker} 눌림 관망: 길목 지키기]"
        line1 = (
            f"• 시세: 현재가 ${price:.2f} (피봇 ${pivot:.2f} 까지 {max(gap, 0):.2f}%) | "
            f"손절 ${stop:.2f} ({stop_pct:.1f}%)"
        )
        line2 = f"• 행동: 토스 [조건주문 ➔ 구매(더 살래요)] 감시가 ${pivot:.2f} · 정규장일 때만"
        kind = "wait"
    if kind == "wait" and gap > 10.0 and sig.startswith("⏳"):
        line2 = f"• 행동: 피봇까지 거리가 {gap:.1f}%로 멉니다 (사정권 밖). 상위 1~3위 사정권 종목을 우선 관찰하십시오."
    html = (
        f'<div class="guide-title">{title}</div>'
        f'<div class="guide-line">{line1}</div>'
        f'<div class="guide-line">{line2}</div>'
    )
    return {"kind": kind, "html": html, "md": f"**{title}**\n\n{line1}\n\n{line2}"}
