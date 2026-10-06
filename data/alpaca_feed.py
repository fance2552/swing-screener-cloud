"""Alpaca Market Data v2 — latest trade for Top-N. No websocket tape."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

import env_settings

CLOCK_URL = "https://paper-api.alpaca.markets/v2/clock"
LATEST_URL = "https://data.alpaca.markets/v2/stocks/trades/latest"
_ET = ZoneInfo("America/New_York")

# IEX last print is often Friday RTH. delayed_sip carries extended-hours last.
# overnight covers 20:00–04:00 ET. Pick the newest timestamp per ticker.
_FEEDS = ("iex", "delayed_sip", "overnight")


def _headers() -> dict[str, str] | None:
    key, secret = env_settings.alpaca_keys()
    if not key or not secret:
        return None
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def ping() -> bool:
    hdr = _headers()
    if not hdr:
        return False
    try:
        r = requests.get(CLOCK_URL, headers=hdr, timeout=8)
        ok = r.status_code == 200
        r.close()
        return ok
    except Exception:  # noqa: BLE001
        return False


def session_label(now: datetime | None = None) -> str:
    ts = now or datetime.now(_ET)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_ET)
    else:
        ts = ts.astimezone(_ET)
    minutes = ts.hour * 60 + ts.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return "프리마켓 반영"
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "정규장"
    if 16 * 60 <= minutes < 20 * 60:
        return "애프터아워 반영"
    return "오버나잇 반영"


def _parse_trades(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    blob = payload.get("trades")
    if blob is None and isinstance(payload.get("trade"), dict) and payload.get("symbol"):
        blob = {str(payload.get("symbol")).upper(): payload.get("trade")}
    if not isinstance(blob, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for raw_sym, trade in blob.items():
        if not isinstance(trade, dict):
            continue
        try:
            px = float(trade.get("p") or 0)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        out[str(raw_sym).upper()] = {
            "price": px,
            "t": str(trade.get("t") or ""),
            "s": trade.get("s"),
            "x": trade.get("x"),
        }
    return out


def _fetch_feed(tickers: list[str], feed: str, hdr: dict[str, str]) -> dict[str, dict[str, Any]]:
    r = None
    try:
        r = requests.get(
            LATEST_URL,
            headers=hdr,
            params={"symbols": ",".join(tickers), "feed": feed},
            timeout=8,
        )
        if r.status_code == 403:
            return {}
        r.raise_for_status()
        parsed = _parse_trades(r.json())
        for row in parsed.values():
            row["feed"] = feed
        return parsed
    except Exception:  # noqa: BLE001
        return {}
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass


def latest_trades(tickers: list[str]) -> dict[str, float]:
    """Premarket-capable last print. {TICKER: trade.p}. Newest timestamp wins."""
    hdr = _headers()
    uniq = [t.upper().strip() for t in tickers if t]
    if not hdr or not uniq:
        return {}

    merged: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(_FEEDS))) as pool:
        futs = [pool.submit(_fetch_feed, uniq, feed, hdr) for feed in _FEEDS]
        for fut in as_completed(futs):
            try:
                chunk = fut.result()
            except Exception:  # noqa: BLE001
                continue
            for sym, row in chunk.items():
                prev = merged.get(sym)
                if prev is None or str(row.get("t") or "") > str(prev.get("t") or ""):
                    merged[sym] = row

    return {sym: float(row["price"]) for sym, row in merged.items() if row.get("price")}


def get_latest_trades(tickers: list[str]) -> dict[str, float]:
    return latest_trades(tickers)
