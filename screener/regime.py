"""SPY regime gate: 200 SMA plus distribution days in the last 25 sessions."""
from __future__ import annotations

from typing import Any

import pandas as pd

_DIST_WINDOW = 25


def _distribution_days(close: pd.Series, volume: pd.Series, window: int = _DIST_WINDOW) -> int:
    """Down close on higher volume than the prior session, counted inside `window`."""
    n = min(len(close), len(volume))
    if n < 2:
        return 0
    c = close.astype(float).to_numpy()[-n:]
    v = volume.astype(float).to_numpy()[-n:]
    start = max(1, n - window)
    count = 0
    for i in range(start, n):
        if c[i] < c[i - 1] and v[i] > v[i - 1]:
            count += 1
    return count


def get_market_regime(spy: pd.DataFrame) -> dict[str, Any]:
    """NORMAL / PRESSURE / CORRECTION from the SPY daily bar.

    NORMAL: close > 200 SMA and fewer than 5 distribution days in 25 sessions.
    PRESSURE: 5 to 7 distribution days, and the close is not under the 200 SMA.
    CORRECTION: close < 200 SMA, or 8+ distribution days.
    """
    if spy is None or not isinstance(spy, pd.DataFrame) or spy.empty or "Close" not in spy.columns:
        return {
            "state": "UNKNOWN",
            "color": "YELLOW",
            "label": "UNKNOWN",
            "detail": "SPY 일봉 없음",
            "distribution_days": 0,
        }
    close = spy["Close"].dropna()
    if len(close) < 200:
        return {
            "state": "UNKNOWN",
            "color": "YELLOW",
            "label": "UNKNOWN",
            "detail": "SPY 200일 부족",
            "distribution_days": 0,
        }
    last = float(close.iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    if "Volume" in spy.columns:
        volume = spy["Volume"].reindex(close.index).fillna(0)
        dist = _distribution_days(close, volume)
    else:
        dist = 0
    above = last > sma200
    if (not above) or dist >= 8:
        state, color = "CORRECTION", "RED"
    elif dist >= 5:
        state, color = "PRESSURE", "YELLOW"
    else:
        state, color = "NORMAL", "GREEN"
    side = ">" if above else "<"
    return {
        "state": state,
        "color": color,
        "label": state,
        "detail": f"SPY {last:.2f} {side} SMA200 {sma200:.2f} · 분산 {dist}/25",
        "distribution_days": dist,
    }


def classify(spy: pd.DataFrame) -> dict[str, Any]:
    """Same gate. Kept so older call sites keep working."""
    return get_market_regime(spy)
