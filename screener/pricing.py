"""3대 전략 공통 가격 뱃지. 스캐너와 분리된 계산만 한다."""
from __future__ import annotations

from typing import Any

import config


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def compute_price_badges(
    current_price: float | None,
    strategy: str,
    consensus_target: float | None = None,
) -> dict[str, float | None]:
    """가격이 있으면 전략별 목표가·손절·상승여력을 채운다. 없으면 전부 None."""
    price = _positive(current_price)
    if price is None:
        return {
            "current_price": None,
            "target_price": None,
            "stop_price": None,
            "upside_pct": None,
            "stop_pct": None,
        }

    consensus = _positive(consensus_target)
    if strategy == "PEAD":
        target = price * 1.10
        stop_pct = float(config.PEAD_SL_PCT)
    elif strategy == "RUNUP":
        target = consensus if consensus is not None else price * 1.08
        stop_pct = float(config.RUNUP_SL_PCT)
    elif strategy == "RSI2":
        target = price * 1.03
        stop_pct = float(config.RSI2_SL_PCT)
    else:
        target = price
        stop_pct = 4.0

    stop = price * (1.0 - stop_pct / 100.0)
    upside_pct = (target - price) / price * 100.0
    return {
        "current_price": round(price, 2),
        "target_price": round(target, 2),
        "stop_price": round(stop, 2),
        "upside_pct": round(upside_pct, 2),
        "stop_pct": round(stop_pct, 2),
    }


def enrich_candidates(candidates: list[dict[str, Any]], strategy: str) -> list[dict[str, Any]]:
    """price / current_price / close 중 있는 값으로 뱃지를 앞에 붙인다."""
    enriched: list[dict[str, Any]] = []
    for row in candidates or []:
        if not isinstance(row, dict):
            continue
        price = row.get("price") or row.get("current_price") or row.get("close")
        consensus = row.get("target_consensus") or row.get("consensus_target")
        badges = compute_price_badges(price, strategy, consensus_target=consensus)
        merged = {**badges, **row}
        merged.update(badges)
        enriched.append(merged)
    return enriched
