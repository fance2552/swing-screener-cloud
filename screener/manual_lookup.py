"""수동 티커 심의용 시세·프로필·실적·컨센서스 수집."""
from __future__ import annotations

from typing import Any

from data import fmp
from data.yf_download import download_daily
from screener import event_driven as ed


def _profile(ticker: str) -> dict[str, Any]:
    for path in ("/stable/profile", "/api/v3/profile"):
        payload = ed._fmp_get(path, {"symbol": ticker})
        row = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(row, dict) and (row.get("companyName") or row.get("symbol")):
            return row
    return {}


def _last_close(frame: Any) -> float | None:
    if frame is None or getattr(frame, "empty", True) or "Close" not in getattr(frame, "columns", []):
        return None
    try:
        price = float(frame["Close"].dropna().iloc[-1])
    except (TypeError, ValueError, IndexError):
        return None
    return price if price > 0 else None


def _sma200(frame: Any) -> float | None:
    if frame is None or getattr(frame, "empty", True) or "Close" not in getattr(frame, "columns", []):
        return None
    closes = []
    for raw in frame["Close"].dropna().tolist():
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price > 0:
            closes.append(price)
    if len(closes) < 200:
        return None
    return sum(closes[-200:]) / 200.0


def fetch_manual_ticker_data(ticker: str) -> dict[str, Any]:
    """시세, 프로필, 다음 실적일, 컨센서스, 50/200일선을 한 번에 모은다."""
    ticker = str(ticker or "").upper().strip()
    result: dict[str, Any] = {"ticker": ticker}
    if not ticker:
        return result

    try:
        quote = (fmp.quotes([ticker]) or {}).get(ticker) or {}
        price = float(quote.get("price") or 0.0)
        result["current_price"] = price if price > 0 else None
    except Exception as exc:  # noqa: BLE001
        result["current_price"] = None
        result["_quote_error"] = str(exc)[:180]

    try:
        profile = _profile(ticker)
        result["company_name"] = str(profile.get("companyName") or ticker)
        result["sector"] = profile.get("sector") or None
        cap = profile.get("mktCap") or profile.get("marketCap")
        result["market_cap"] = float(cap) if cap not in (None, "") else None
    except Exception:  # noqa: BLE001
        result["company_name"] = ticker
        result["sector"] = None
        result["market_cap"] = None

    frame = None
    try:
        frame = (download_daily([ticker]) or {}).get(ticker)
        stats = ed._hist_stats(frame)
        result["sma50"] = float(stats["sma50"]) if stats and stats.get("sma50") else None
        result["sma200"] = _sma200(frame)
        if result["current_price"] is None:
            result["current_price"] = _last_close(frame)
    except Exception:  # noqa: BLE001
        result["sma200"] = None
        result["sma50"] = None

    try:
        upcoming = ed.next_earnings_date(ticker)
        result["earnings_date"] = upcoming or None
    except Exception:  # noqa: BLE001
        result["earnings_date"] = None

    try:
        wall = fmp.fetch_fmp_analyst_data(ticker) or {}
        target = float(wall.get("target_consensus") or 0.0)
        result["target_consensus"] = target if target > 0 else None
    except Exception:  # noqa: BLE001
        result["target_consensus"] = None

    cur = result.get("current_price")
    tgt = result.get("target_consensus") or (cur * 1.08 if cur else None)
    result["price"] = cur
    result["target_price"] = round(float(tgt), 2) if tgt else None
    result["stop_price"] = round(float(cur) * 0.96, 2) if cur else None
    result["upside_pct"] = (
        round(max(0.0, (float(tgt) - float(cur)) / float(cur) * 100.0), 2) if cur and tgt else None
    )
    return result
