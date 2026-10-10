"""PEAD (Post-Earnings-Announcement Drift) 스캐너.

실적 서프라이즈와 반응일 갭업을 확인한 뒤 D+1~D+2 후보를 뽑는다.
event_driven.fetch_calendar()는 오늘 이후 날짜만 symbol→ISO 로 돌려주고 EPS를 버린다.
그래서 같은 FMP 캘린더 엔드포인트를 과거 구간으로 다시 읽어 원문 행을 유지한다.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

import config
from screener import event_driven as ed

PEAD_FIRE = "🔥 PEAD 진입"


def _num(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _recent_earnings(lookback_days: int = 4) -> list[dict[str, Any]]:
    """최근 N일(오늘 포함)에 잡힌 실적 원문. eps/revenue 필드를 유지한다."""
    today = ed.today_et()
    start = today - timedelta(days=lookback_days)
    params = {"from": start.isoformat(), "to": today.isoformat()}
    data: list[Any] = []
    for path in ("/stable/earnings-calendar", "/api/v3/earning_calendar"):
        payload = ed._fmp_get(path, params)
        if isinstance(payload, list) and payload:
            data = payload
            break
    return [row for row in data if isinstance(row, dict)]


def _is_surprise(row: dict[str, Any]) -> bool:
    """EPS 서프라이즈. FMP는 eps/epsEstimated 와 epsActual/epsEstimated 를 섞어 준다."""
    eps_actual = _num(row, "epsActual", "eps")
    eps_estimate = _num(row, "epsEstimated", "epsEstimate")
    rev_actual = _num(row, "revenueActual", "revenue")
    rev_estimate = _num(row, "revenueEstimated", "revenueEstimate")
    if eps_actual is None or eps_estimate is None or eps_estimate == 0:
        return False
    eps_beat_pct = (eps_actual - eps_estimate) / abs(eps_estimate) * 100.0
    if eps_beat_pct < float(config.PEAD_SURPRISE_EPS_PCT):
        return False
    if rev_actual is not None and rev_estimate is not None and rev_actual < rev_estimate:
        return False
    return True


def _reaction_gap_pct(df: pd.DataFrame | None, earnings_date: date) -> float | None:
    """반응일 시가 / 직전 종가 - 1. _hist_stats()는 시가와 일자 리스트를 안 돌려준다."""
    if df is None or getattr(df, "empty", True):
        return None
    if "Open" not in df.columns or "Close" not in df.columns:
        return None
    prior_close = None
    reaction_open = None
    ordered = df.sort_index()
    for ts, bar in ordered.iterrows():
        bar_day = ts.date() if hasattr(ts, "date") else ts
        try:
            close = float(bar["Close"])
            opened = float(bar["Open"])
        except (TypeError, ValueError):
            continue
        if close <= 0 or opened <= 0:
            continue
        if bar_day < earnings_date:
            prior_close = close
        elif reaction_open is None:
            reaction_open = opened
            break
    if not prior_close or not reaction_open:
        return None
    return (reaction_open - prior_close) / prior_close * 100.0


def scan_pead(limit: int = 7) -> list[dict[str, Any]]:
    """PEAD 후보. 점수 내림차순 Top `limit`."""
    from data.yf_download import download_daily

    today = ed.today_et()
    pending: list[tuple[str, date, str, float]] = []
    for row in _recent_earnings():
        ticker = str(row.get("symbol") or "").upper().strip()
        if not ticker or not _is_surprise(row):
            continue
        earnings_date = ed._parse(row.get("date"))
        if earnings_date is None:
            continue
        days_since = (today - earnings_date).days
        if not (int(config.PEAD_ENTRY_WINDOW_MIN_DAY) <= days_since <= int(config.PEAD_ENTRY_WINDOW_MAX_DAY)):
            continue
        eps_actual = _num(row, "epsActual", "eps") or 0.0
        eps_estimate = _num(row, "epsEstimated", "epsEstimate") or 0.0
        eps_beat_pct = (eps_actual - eps_estimate) / abs(eps_estimate) * 100.0 if eps_estimate else 0.0
        pending.append((ticker, earnings_date, earnings_date.isoformat(), eps_beat_pct))
    if not pending:
        return []

    hist = download_daily(sorted({ticker for ticker, *_ in pending}))
    candidates: list[dict[str, Any]] = []
    for ticker, earnings_date, earnings_date_str, eps_beat_pct in pending:
        gap_pct = _reaction_gap_pct(hist.get(ticker), earnings_date)
        if gap_pct is None or gap_pct < float(config.PEAD_GAP_UP_MIN_PCT):
            continue
        score = round(min(eps_beat_pct, 50.0) * 0.6 + min(gap_pct, 20.0) * 2.0, 1)
        candidates.append({
            "ticker": ticker,
            "score": min(score, 100.0),
            "earnings_date": earnings_date_str,
            "days_since_earnings": (today - earnings_date).days,
            "eps_beat_pct": round(eps_beat_pct, 1),
            "gap_up_pct": round(gap_pct, 1),
            "signal": PEAD_FIRE,
        })
    candidates.sort(key=lambda r: (-float(r["score"]), str(r["ticker"])))
    return candidates[:limit]


def pead_guide() -> str:
    return (
        "PEAD: 실적 서프라이즈(EPS +5% 이상) + 반응일 갭업(+2% 이상) 확인 후 "
        "D+1~D+2 구간에 진입하는 사후 드리프트 전략. 어닝 쇼크 위험 없음."
    )
