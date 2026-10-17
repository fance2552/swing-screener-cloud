"""가격이 없는 후보는 순위표에 올리지 않는다.

v4 사냥 Top 은 scan_all 이 이 함수로 자른다. 가격이 없거나 0 이거나
숫자가 아니면 그 행은 버린다.
"""
from __future__ import annotations


def _price(row: dict) -> float | None:
    raw = row.get("price")
    if raw is None or raw == "":
        raw = row.get("current_price")
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return None
    if px != px or px <= 0:
        return None
    return px


def priced_top(rows: list | None, limit: int = 10) -> list:
    """유효한 가격이 있는 행만, 앞선 순서대로 limit 개."""
    kept: list = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        if not symbol or symbol in seen:
            continue
        if _price(row) is None:
            continue
        seen.add(symbol)
        kept.append(row)
        if len(kept) >= int(limit):
            break
    return kept
