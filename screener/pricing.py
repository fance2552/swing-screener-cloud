"""3대 전략 공통 가격 뱃지. 스캐너와 분리된 계산만 한다."""
from __future__ import annotations

from typing import Any


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def compute_price_badges(row: dict[str, Any], strategy: str) -> dict[str, float]:
    """row의 현재가 키를 순서대로 찾고, 전략별 목표가와 손절 -4%를 계산한다."""
    current_price = _num(
        row.get("current_price")
        or row.get("price")
        or row.get("close")
        or 0.0
    )
    consensus_target = _num(
        row.get("target_consensus") or row.get("consensus_target") or row.get("target_price") or 0.0
    )

    if current_price <= 0:
        return {
            "current_price": 0.0,
            "target_price": 0.0,
            "stop_price": 0.0,
            "upside_pct": 0.0,
        }

    if strategy == "PEAD":
        target = consensus_target or (current_price * 1.10)
    elif strategy == "RUNUP":
        target = consensus_target or (current_price * 1.08)
    elif strategy == "RSI2":
        target = consensus_target or (current_price * 1.03)
    else:
        target = current_price

    stop = current_price * 0.96
    upside_pct = max(0.0, (target - current_price) / current_price * 100.0)
    return {
        "current_price": round(current_price, 2),
        "target_price": round(target, 2),
        "stop_price": round(stop, 2),
        "upside_pct": round(upside_pct, 2),
    }


def enrich_candidates(candidates: list[dict[str, Any]], strategy: str) -> list[dict[str, Any]]:
    """후보 리스트 전체에 가격 뱃지 필드를 주입한다."""
    enriched: list[dict[str, Any]] = []
    for row in candidates or []:
        if not isinstance(row, dict):
            continue
        badges = compute_price_badges(row, strategy)
        enriched.append({**row, **badges})
    return enriched
