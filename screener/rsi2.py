"""RSI(2) 우량주 과매도 반등 스캐너.

SPY가 200일선 위에 있을 때만 후보를 낸다.
_hist_stats()는 DataFrame을 받고 sma50까지만 돌려준다. 200일선과 RSI 종가는
같은 Close 열에서 직접 계산한다.
fetch_universe()에는 시총 인자가 없다. 받아온 뒤 marketCap으로 자른다.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np
import pandas as pd

import config
from screener import event_driven as ed

RSI2_FIRE = "⚡ RSI2 진입"


def _calc_rsi(closes: list[float], period: int = 2) -> float | None:
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _closes(df: pd.DataFrame | None) -> list[float]:
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return []
    out: list[float] = []
    for raw in df["Close"].dropna().tolist():
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            out.append(px)
    return out


def _spy_above_200sma(df: pd.DataFrame | None = None) -> bool:
    """일봉이 없으면 스캐너를 막지 않는다. 200일이 있으면 종가와 비교한다."""
    if df is None:
        from data.yf_download import download_daily
        df = download_daily(["SPY"]).get("SPY")
    closes = _closes(df)
    if len(closes) < 200:
        return True
    sma200 = sum(closes[-200:]) / 200.0
    return closes[-1] >= sma200


def _earnings_window() -> dict[str, str]:
    """오늘 ±가드 구간의 실적일. fetch_calendar()는 과거 발표를 버린다."""
    today = ed.today_et()
    guard = int(config.RSI2_EARNINGS_GUARD_DAYS) + 2
    start = today - timedelta(days=guard + 4)
    end = today + timedelta(days=guard)
    params = {"from": start.isoformat(), "to": end.isoformat()}
    data: list[Any] = []
    for path in ("/stable/earnings-calendar", "/api/v3/earning_calendar"):
        payload = ed._fmp_get(path, params)
        if isinstance(payload, list) and payload:
            data = payload
            break
    out: dict[str, str] = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        parsed = ed._parse(row.get("date"))
        if not sym or parsed is None:
            continue
        iso = parsed.isoformat()
        prev = out.get(sym)
        if prev is None or abs((parsed - today).days) < abs((ed._parse(prev) - today).days):
            out[sym] = iso
    return out


def _near_earnings(ticker: str, calendar: dict[str, str]) -> bool:
    raw = calendar.get(ticker.upper())
    parsed = ed._parse(raw)
    if parsed is None:
        return False
    today = ed.today_et()
    left, right = sorted((parsed, today))
    return int(np.busday_count(left, right)) <= int(config.RSI2_EARNINGS_GUARD_DAYS)


def scan_rsi2(limit: int = 7) -> list[dict[str, Any]]:
    """RSI2 후보. SPY가 200일선 아래면 빈 리스트."""
    from data.yf_download import download_daily

    if not _spy_above_200sma():
        return []

    floor = float(config.RSI2_MIN_MARKET_CAP)
    universe = []
    for row in ed.fetch_universe():
        if not isinstance(row, dict):
            continue
        try:
            cap = float(row.get("marketCap") or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap >= floor and row.get("symbol"):
            universe.append(row)
    calendar = _earnings_window()
    tickers = []
    for row in universe:
        ticker = str(row.get("symbol") or "").upper().strip()
        if ticker and not _near_earnings(ticker, calendar):
            tickers.append(ticker)
    if not tickers:
        return []

    hist = download_daily(sorted(set(tickers)))
    cap_by_ticker: dict[str, float] = {}
    for row in universe:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        raw_cap = row.get("marketCap") or row.get("market_cap") or row.get("mcap") or 0.0
        try:
            cap_by_ticker[symbol] = float(raw_cap or 0.0)
        except (TypeError, ValueError):
            cap_by_ticker[symbol] = 0.0
    candidates: list[dict[str, Any]] = []
    for ticker in tickers:
        closes = _closes(hist.get(ticker))
        rsi2 = _calc_rsi(closes, period=int(config.RSI2_PERIOD))
        if rsi2 is None or rsi2 > float(config.RSI2_ENTRY_MAX):
            continue
        price = closes[-1] if closes else 0.0
        ret_2d = ((closes[-1] / closes[-3]) - 1.0) * 100.0 if len(closes) >= 3 and closes[-3] > 0 else 0.0
        score = min(round(max(0.0, float(config.RSI2_ENTRY_MAX) - rsi2) * 10.0, 1), 100.0)
        market_cap = cap_by_ticker.get(ticker) or 0.0
        market_cap_b = round(market_cap / 1_000_000_000, 2) if market_cap else 0.0
        closes_3d_ago = closes[-4] if len(closes) >= 4 else None
        drop_3d_pct = (
            round((price - closes_3d_ago) / closes_3d_ago * 100, 2)
            if closes_3d_ago and price else 0.0
        )
        sma200 = sum(closes[-200:]) / 200.0 if len(closes) >= 200 else 0.0
        dist_sma200_pct = (
            round((price - sma200) / sma200 * 100, 2)
            if sma200 and price else 0.0
        )
        candidates.append({
            "ticker": ticker,
            "score": score,
            "rsi2": rsi2,
            "price": price or 0.0,
            "ret_2d": round(ret_2d, 2),
            "market_cap_b": market_cap_b,
            "drop_3d_pct": drop_3d_pct,
            "dist_sma200_pct": dist_sma200_pct,
            "signal": RSI2_FIRE,
        })
    # 점수 동률이면 3일 낙폭이 큰 종목이 앞선다. 티커는 마지막 타이브레이크다.
    candidates.sort(key=lambda r: str(r["ticker"]))
    candidates.sort(
        key=lambda r: (float(r["score"]), abs(float(r.get("drop_3d_pct") or 0))),
        reverse=True,
    )
    return candidates[:limit]


def rsi2_guide() -> str:
    return (
        "RSI2: SPY 200일선 위 체제에서만 작동. 시총 $5B+ 대형 우량주가 RSI(2) < 10으로 "
        "급락했을 때 2~4일 기술적 반등을 노린다. 실적 ±5영업일 이내 종목은 제외."
    )
