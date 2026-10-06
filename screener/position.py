"""Position sizer. Risk is marked from live price to stop, not from pivot."""
from __future__ import annotations

import math


def pack(seed: float, risk_pct: float, price: float, stop: float) -> dict[str, float | int]:
    risk_cash = seed * (risk_pct / 100.0)
    per_share = price - stop
    qty = int(math.floor(risk_cash / per_share)) if risk_cash > 0 and per_share > 0 else 0
    stop_p = (stop / price - 1.0) * 100.0 if price > 0 else 0.0
    return {
        "risk_cash": risk_cash,
        "per_share": per_share,
        "qty": qty,
        "stop_pct": stop_p,
    }


def headline(ticker: str, stop_pct: float, qty: int) -> str:
    return f"👉 [{ticker}] 선택됨: 손절 {stop_pct:.1f}% ➔ 토스에 【 {qty} 주 】 예약 매수!"


def copy_text(ticker: str, pivot: float, stop: float, qty: int, stop_pct: float) -> str:
    return (
        f"티커: {ticker}\n"
        f"피봇 돌파가: {pivot:.2f}\n"
        f"손절가: {stop:.2f} ({stop_pct:.1f}%)\n"
        f"예약수량: {qty}주"
    )
