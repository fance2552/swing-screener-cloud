"""AI 분석 버튼을 누를 때만 호출되는 뉴스 수집 모듈.
전체 유니버스가 아니라 Top 후보 소수 + 보유종목에만 쓴다 — run_scan()
파이프라인(event_driven.py)과는 완전히 분리되어 있고 그쪽은 건드리지 않는다.
"""
from __future__ import annotations

from typing import Any

import requests

NEWS_URL = "https://financialmodelingprep.com/stable/news/stock"
PRESS_URL = "https://financialmodelingprep.com/stable/news/press-releases"
NEWS_LIMIT_PER_SYMBOL = 3


def _fmp_get(url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_news_digest(symbol: str, api_key: str) -> str:
    items: list[str] = []
    news = _fmp_get(NEWS_URL, {"symbols": symbol, "limit": NEWS_LIMIT_PER_SYMBOL, "apikey": api_key})
    for row in news:
        title = str(row.get("title") or "").strip()
        date = str(row.get("publishedDate") or "")[:10]
        if title:
            items.append(f"[뉴스 {date}] {title}")

    press = _fmp_get(PRESS_URL, {"symbols": symbol, "limit": NEWS_LIMIT_PER_SYMBOL, "apikey": api_key})
    for row in press:
        title = str(row.get("title") or "").strip()
        date = str(row.get("date") or row.get("publishedDate") or "")[:10]
        if title:
            items.append(f"[보도자료 {date}] {title}")

    return "\n".join(items)


def fetch_news_for_candidates(
    pead_top: list[dict[str, Any]],
    runup_top: list[dict[str, Any]],
    rsi2_top: list[dict[str, Any]],
    held: list[dict[str, Any]],
    api_key: str,
    top_n: int = 3,
) -> dict[str, str]:
    symbols: set[str] = set()
    for rows in (pead_top, runup_top, rsi2_top):
        for row in (rows or [])[:top_n]:
            symbols.add(str(row.get("ticker") or "").upper())
    for row in held or []:
        symbols.add(str(row.get("ticker") or "").upper())
    symbols.discard("")
    return {sym: fetch_news_digest(sym, api_key) for sym in symbols}
