"""Yahoo v7(+Nasdaq 폴백) 1.0s quote poll + Alpaca IEX websocket. Display prices only. No orders.

fmp_realtime_polling_loop()는 완전히 구현되어 있으나 start_dual_realtime_engine()이
스케줄링하지 않는 미사용 함수다. 레거시 v3 엔드포인트(/api/v3/quote/...)를 쓰므로
재활성화 전 /stable/quote로 교체할 것.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

import aiohttp
import websockets

import env_settings
from slog import slog

WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
_started = False
_start_lock = threading.Lock()
_feed_thread: threading.Thread | None = None


def _term(line: str) -> None:
    """Streamlit이 stdout을 삼켜도 기동 터미널에 남긴다."""
    text = line if line.endswith("\n") else line + "\n"
    try:
        sys.__stderr__.write(text)
        sys.__stderr__.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        print(text, end="", flush=True)
    except Exception:  # noqa: BLE001
        pass


def _keys() -> tuple[str, str, str]:
    env_settings.load()
    fmp = env_settings.fmp_key()
    key, secret = env_settings.alpaca_keys()
    return fmp, key, secret


def _note_feed(feed: str, tick: bool = True) -> None:
    from state import SHARED

    SHARED.note_feed(feed, tick=tick)


def _put(store: dict[str, Any], lock: threading.Lock, sym: str, **fields: Any) -> None:
    sym = sym.upper().strip()
    if not sym:
        return
    with lock:
        row = store.get(sym)
        if not isinstance(row, dict):
            row = {}
        for name, value in fields.items():
            if name == "price" and not value:
                continue
            row[name] = value
        store[sym] = row


def _apply_ws(message: str | bytes, store: dict[str, Any], lock: threading.Lock) -> None:
    try:
        data = json.loads(message)
    except Exception:  # noqa: BLE001
        return
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return
    for event in data:
        if not isinstance(event, dict):
            continue
        sym = str(event.get("S") or "").upper().strip()
        if not sym:
            continue
        kind = event.get("T")
        if kind == "t":
            try:
                price = float(event.get("p") or 0)
            except (TypeError, ValueError):
                continue
            if price > 0:
                _put(
                    store,
                    lock,
                    sym,
                    price=price,
                    last_trade=price,
                    trade_ts=time.time(),
                    source="IEX_TRADE",
                )
                _note_feed("alpaca", tick=True)
        elif kind == "q":
            try:
                bid = float(event.get("bp") or 0)
                ask = float(event.get("ap") or 0)
            except (TypeError, ValueError):
                continue
            with lock:
                row = store.get(sym)
                has_trade = isinstance(row, dict) and "last_trade" in row
                src = str(row.get("source") or "") if isinstance(row, dict) else ""
            fields: dict[str, Any] = {"bid": bid, "ask": ask}
            if (
                bid > 0
                and ask > 0
                and not has_trade
                and src not in ("PRE_MARKET", "POST_MARKET", "REGULAR", "LIVE")
            ):
                fields["price"] = round((bid + ask) / 2, 2)
                fields["source"] = "IEX_QUOTE"
            _put(store, lock, sym, **fields)
            _note_feed("alpaca", tick=True)


async def _sync_subs(websocket: Any, subscribed: set[str], tickers: list[str]) -> None:
    want = {str(t).upper().strip() for t in tickers if t}
    drop = subscribed - want
    add = want - subscribed
    if drop:
        await websocket.send(
            json.dumps({"action": "unsubscribe", "trades": sorted(drop), "quotes": sorted(drop)})
        )
        subscribed -= drop
    if add:
        names = sorted(add)
        await websocket.send(json.dumps({"action": "subscribe", "trades": names, "quotes": names}))
        subscribed |= add
        _term(f"[Alpaca IEX] 신규 실시간 구독 반영: {names}")


async def alpaca_iex_ws_loop(
    get_tickers_func: Callable[[], list[str]],
    live_quotes_dict: dict[str, Any],
    lock: threading.Lock,
) -> None:
    while True:
        try:
            _, key, secret = _keys()
            if not key or not secret:
                await asyncio.sleep(2)
                continue
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as websocket:
                conn_msg = await asyncio.wait_for(websocket.recv(), timeout=5)
                await websocket.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
                auth_res = await asyncio.wait_for(websocket.recv(), timeout=5)
                if "authenticated" not in str(auth_res):
                    raise RuntimeError(f"auth rejected after {conn_msg!r}: {auth_res!r}"[:240])
                _note_feed("alpaca", tick=False)
                _term(f"[Alpaca IEX] 웹소켓 인증 완벽 성공: {auth_res}")
                subscribed: set[str] = set()
                last_sync = 0.0
                while True:
                    now_mono = time.monotonic()
                    if now_mono - last_sync >= 1.0:
                        last_sync = now_mono
                        current = {
                            str(t).upper().strip()
                            for t in (get_tickers_func() or [])
                            if t and "=" not in str(t)
                        }
                        if current != subscribed:
                            await _sync_subs(websocket, subscribed, sorted(current))
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        _apply_ws(message, live_quotes_dict, lock)
                    except asyncio.TimeoutError:
                        _note_feed("alpaca", tick=False)
                        continue
        except Exception as exc:  # noqa: BLE001
            slog(f"Alpaca IEX 웹소켓 재접속: {exc}")
            _term(f"[WS ERROR] Alpaca IEX 연결 재시도: {exc}")
            await asyncio.sleep(2)


def _dual_sample(store: dict[str, Any], lock: threading.Lock, tickers: list[str]) -> str:
    with lock:
        pairs = []
        for sym, row in list(store.items())[:2]:
            px = row.get("price") if isinstance(row, dict) else row
            pairs.append(f"{sym}: ${px}")
    if not pairs:
        pairs = [f"{sym}: $—" for sym in tickers[:2]]
    return "[DUAL 1.0s 실시간] " + ", ".join(pairs)


def _apply_fmp_row(row: dict[str, Any], store: dict[str, Any], lock: threading.Lock) -> None:
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym:
        return
    try:
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        day_high = float(row.get("dayHigh") or row.get("day_high") or 0)
    except (TypeError, ValueError):
        day_high = 0.0
    try:
        volume = int(float(row.get("volume") or 0))
    except (TypeError, ValueError):
        volume = 0
    with lock:
        current = store.get(sym)
        if not isinstance(current, dict):
            current = {}
        current["dayHigh"] = day_high
        current["volume"] = volume
        trade_ts = float(current.get("trade_ts") or 0)
        fresh_trade = (
            current.get("source") == "IEX_TRADE" and (time.time() - trade_ts) < 1.0
        )
        if price > 0 and not fresh_trade:
            current["price"] = price
            current["source"] = "FMP"
        store[sym] = current
    _note_feed("fmp")


async def _fmp_one(session: aiohttp.ClientSession, sym: str, key: str, base: str) -> dict[str, Any] | int | None:
    timeout = aiohttp.ClientTimeout(total=2)
    try:
        async with session.get(
            f"{base}/stable/quote",
            params={"symbol": sym, "apikey": key},
            timeout=timeout,
        ) as resp:
            if resp.status == 429:
                return 429
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


# ⚠️ 미사용 — start_dual_realtime_engine()이 호출하지 않는다 (아래 참조).
async def fmp_realtime_polling_loop(
    get_tickers_func: Callable[[], list[str]],
    live_quotes_dict: dict[str, Any],
    lock: threading.Lock,
) -> None:
    timeout = aiohttp.ClientTimeout(total=3.0)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tickers = [str(t).upper().strip() for t in (get_tickers_func() or []) if t]
                fmp_key, _, _ = _keys()
                base = env_settings.fmp_base()
                if tickers and fmp_key:
                    symbol_str = ",".join(tickers)
                    url = f"{base}/api/v3/quote/{symbol_str}"
                    async with session.get(url, params={"apikey": fmp_key}, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and data:
                                for quote in data:
                                    if isinstance(quote, dict):
                                        _apply_fmp_row(quote, live_quotes_dict, lock)
                                _term(_dual_sample(live_quotes_dict, lock, tickers))
                                await asyncio.sleep(1.0)
                                continue
                        elif resp.status == 429:
                            await asyncio.sleep(3.0)
                            continue
                    rows = await asyncio.gather(
                        *[_fmp_one(session, sym, fmp_key, base) for sym in tickers],
                        return_exceptions=True,
                    )
                    if any(row == 429 for row in rows):
                        await asyncio.sleep(3.0)
                        continue
                    applied = False
                    for row in rows:
                        if isinstance(row, dict):
                            _apply_fmp_row(row, live_quotes_dict, lock)
                            applied = True
                    if applied:
                        _term(_dual_sample(live_quotes_dict, lock, tickers))
                        await asyncio.sleep(1.0)
                        continue
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.0)


_fx_px = 0.0
_fx_at = 0.0
_YAHOO_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
_yahoo_blocked = False
_yahoo_blocked_until = 0.0
_yahoo_backoff_sec = 60.0
_YAHOO_BACKOFF_MIN = 60.0
_YAHOO_BACKOFF_MAX = 15 * 60.0


def _yahoo_in_backoff() -> bool:
    return time.time() < _yahoo_blocked_until


def _yahoo_trip_429() -> None:
    """Stop hitting Yahoo. 60s, then 120s, 240s, 480s, cap 15 minutes."""
    global _yahoo_blocked, _yahoo_blocked_until, _yahoo_backoff_sec
    wait = max(_YAHOO_BACKOFF_MIN, min(_yahoo_backoff_sec, _YAHOO_BACKOFF_MAX))
    _yahoo_blocked = True
    _yahoo_blocked_until = time.time() + wait
    nxt = min(_YAHOO_BACKOFF_MAX, wait * 2.0)
    _yahoo_backoff_sec = nxt
    _term(f"[LIVE] Yahoo quote 429. {wait:.0f}s 백오프. 다음 대기 {nxt:.0f}s. Nasdaq 시세 유지.")


def _yahoo_clear_backoff() -> None:
    global _yahoo_blocked, _yahoo_blocked_until, _yahoo_backoff_sec
    _yahoo_blocked = False
    _yahoo_blocked_until = 0.0
    _yahoo_backoff_sec = _YAHOO_BACKOFF_MIN


def _money(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _pick_session_price(
    market_state: str, reg_price: float, pre_price: float, post_price: float
) -> tuple[float, str]:
    state = market_state.upper()
    if pre_price > 0 and state in ("PRE", "PREPRE"):
        return pre_price, "PRE_MARKET"
    if post_price > 0 and state in ("POST", "POSTPOST"):
        return post_price, "POST_MARKET"
    if reg_price > 0:
        return reg_price, "REGULAR"
    price = pre_price or reg_price or post_price or 0.0
    return price, "LIVE"


def _write_live_price(
    store: dict[str, Any],
    lock: threading.Lock,
    sym: str,
    price: float,
    day_high: float,
    volume: int,
    source: str,
) -> None:
    """1초 이내의 Alpaca IEX 실거래가(IEX_TRADE)는 Yahoo/Nasdaq 폴링이
    덮어쓰지 못한다. _apply_fmp_row()의 fresh_trade 가드와 동일한 보호를
    실제 활성 경로(이 함수)에도 적용한 것 — 기존에는 이 함수에만 없었다."""
    if price <= 0:
        return
    with lock:
        current = store.get(sym)
        if not isinstance(current, dict):
            current = {}
        trade_ts = float(current.get("trade_ts") or 0)
        fresh_trade = current.get("source") == "IEX_TRADE" and (time.time() - trade_ts) < 1.0
        if not fresh_trade:
            current["price"] = price
            current["source"] = source
        if day_high > 0:
            current["dayHigh"] = day_high
        if volume > 0:
            current["volume"] = volume
        store[sym] = current


def _nasdaq_fields(sym: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    primary = data.get("primaryData") if isinstance(data.get("primaryData"), dict) else {}
    secondary = data.get("secondaryData") if isinstance(data.get("secondaryData"), dict) else {}
    status = str(data.get("marketStatus") or "")
    live = _money(primary.get("lastSalePrice"))
    prev = _money(secondary.get("lastSalePrice"))
    state = status.upper()
    pre = post = reg = 0.0
    if "PRE" in state:
        session = "PRE"
        pre, reg = live, prev
    elif "POST" in state or "AFTER" in state:
        session = "POST"
        post, reg = live, prev
    else:
        session = "REGULAR"
        reg = live or prev
    try:
        volume = int(_money(primary.get("volume")))
    except (TypeError, ValueError):
        volume = 0
    if volume < 1000:
        volume = 0
    return {
        "symbol": sym,
        "marketState": session,
        "regularMarketPrice": reg,
        "preMarketPrice": pre,
        "postMarketPrice": post,
        "regularMarketDayHigh": 0.0,
        "regularMarketVolume": volume,
    }


async def _nasdaq_one(session: aiohttp.ClientSession, sym: str) -> dict[str, Any] | None:
    url = f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass=stocks"
    try:
        async with session.get(
            url,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001
        return None
    return _nasdaq_fields(sym, payload if isinstance(payload, dict) else {})


def _apply_quote_item(item: dict[str, Any], store: dict[str, Any], lock: threading.Lock) -> bool:
    sym = str(item.get("symbol") or "").upper().strip()
    if not sym:
        return False
    reg_price = _money(item.get("regularMarketPrice"))
    pre_price = _money(item.get("preMarketPrice"))
    post_price = _money(item.get("postMarketPrice"))
    day_high = _money(item.get("regularMarketDayHigh"))
    try:
        volume = int(_money(item.get("regularMarketVolume")))
    except (TypeError, ValueError):
        volume = 0
    price, source = _pick_session_price(
        str(item.get("marketState") or ""), reg_price, pre_price, post_price
    )
    if price <= 0:
        return False
    _write_live_price(store, lock, sym, price, day_high, volume, source)
    return True


async def _krw_rate(session: aiohttp.ClientSession) -> float:
    """Yahoo KRW=X is 429 here. Frankfurter is the USD/KRW print until Yahoo answers."""
    global _fx_px, _fx_at
    if _fx_px > 500 and time.time() - _fx_at < 60:
        return _fx_px
    try:
        async with session.get(
            "https://api.frankfurter.app/latest?from=USD&to=KRW",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            if resp.status != 200:
                return _fx_px
            data = await resp.json(content_type=None)
        px = float(((data or {}).get("rates") or {}).get("KRW") or 0)
    except Exception:  # noqa: BLE001
        return _fx_px
    if px > 500:
        _fx_px = px
        _fx_at = time.time()
    return _fx_px


async def realtime_quote_polling_loop(
    get_tickers_func: Callable[[], list[str]],
    live_quotes_dict: dict[str, Any],
    lock: threading.Lock,
) -> None:
    """Yahoo v7 batch first. 429 trips exponential backoff 60s→15m. That cycle uses Nasdaq."""
    timeout = aiohttp.ClientTimeout(total=8.0)
    async with aiohttp.ClientSession(headers=_YAHOO_UA, timeout=timeout) as session:
        while True:
            try:
                tickers = [str(t).upper().strip() for t in (get_tickers_func() or []) if t]
                if tickers:
                    applied = False
                    if not _yahoo_in_backoff():
                        symbol_str = ",".join(quote(sym, safe="") for sym in tickers)
                        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol_str}"
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                            if resp.status == 200:
                                _yahoo_clear_backoff()
                                data = await resp.json(content_type=None)
                                results = []
                                if isinstance(data, dict):
                                    results = (data.get("quoteResponse") or {}).get("result") or []
                                for item in results:
                                    if isinstance(item, dict) and _apply_quote_item(item, live_quotes_dict, lock):
                                        applied = True
                            elif resp.status == 429:
                                _yahoo_trip_429()
                    if not applied:
                        stocks = [sym for sym in tickers if "=" not in sym]
                        rows = await asyncio.gather(
                            *[_nasdaq_one(session, sym) for sym in stocks],
                            return_exceptions=True,
                        )
                        for row in rows:
                            if isinstance(row, dict) and _apply_quote_item(row, live_quotes_dict, lock):
                                applied = True
                    if "KRW=X" in tickers:
                        with lock:
                            fx_row = live_quotes_dict.get("KRW=X")
                            yahoo_fx = (
                                isinstance(fx_row, dict)
                                and float(fx_row.get("price") or 0) > 500
                                and fx_row.get("source") != "FX"
                            )
                        if not yahoo_fx:
                            fx = await _krw_rate(session)
                            if fx > 500:
                                _write_live_price(live_quotes_dict, lock, "KRW=X", fx, 0, 0, "FX")
                                applied = True
                    if applied:
                        _note_feed("fmp")
                        _term(_dual_sample(live_quotes_dict, lock, tickers))
                        await asyncio.sleep(1.0)
                        continue
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.0)


def start_dual_realtime_engine(
    get_tickers_func: Callable[[], list[str]],
    live_quotes_dict: dict[str, Any],
    lock: threading.Lock | None = None,
) -> None:
    """One daemon. A dead thread is started again. Watchlist is re-read once a second."""
    global _started, _feed_thread
    gate = lock or threading.Lock()

    def run_async_loop() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(alpaca_iex_ws_loop(get_tickers_func, live_quotes_dict, gate))
        loop.create_task(realtime_quote_polling_loop(get_tickers_func, live_quotes_dict, gate))
        loop.run_forever()

    with _start_lock:
        if _feed_thread is not None and _feed_thread.is_alive():
            return
        _started = True
        _feed_thread = threading.Thread(target=run_async_loop, daemon=True, name="ldpb-dual-feed")
        _feed_thread.start()
    slog("듀얼 실시간 엔진 가동. LIVE 1.0s + Alpaca IEX")
