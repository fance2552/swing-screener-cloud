"""FMP 공통 클라이언트 (stable API).

Streamlit 에 의존하지 않으므로 스레드풀에서 안전하게 호출할 수 있다.
실패 시 예외를 던지지 않고 None 을 돌려준다 (호출부가 '데이터 없음'으로 처리).
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd
import requests

FMP_BASE = "https://financialmodelingprep.com/stable"
_TIMEOUT = 12
ET = ZoneInfo("America/New_York")
LAST_OK_TS = 0.0
LAST_HTTP_DATE = ""
LAST_HTTP_STATUS = None
LAST_OK = False
LAST_FAIL_TS = 0.0
_PULSE_LOCK = threading.Lock()
_PULSE_ON = False


def age_label(now: float, last: float) -> str:
    if last <= 0:
        return "대기"
    age = max(0, int(now - last))
    if age < 60:
        return f"{age}초 전"
    return f"{age // 60}분 전"


def _stamp_fmp(resp, ok: bool) -> None:
    global LAST_OK_TS, LAST_HTTP_DATE, LAST_OK, LAST_FAIL_TS
    now = time.time()
    header = ""
    try:
        header = str(resp.headers.get("Date") or "")
    except Exception:
        header = ""
    if ok:
        LAST_OK_TS = now
        LAST_HTTP_DATE = header
        LAST_OK = True
        try:
            from state import SHARED
            SHARED.note_fmp(now, header, ok=True)
        except Exception:
            pass
        return
    LAST_FAIL_TS = now
    if LAST_OK_TS <= 0:
        LAST_OK = False


def pulse(api_key: str) -> None:
    """15초보다 시세가 오래되면 QQQ 시세를 한 번 받아 배지 시각을 갱신한다."""
    global _PULSE_ON
    if not api_key:
        return
    if LAST_OK_TS and time.time() - LAST_OK_TS < 15:
        return
    with _PULSE_LOCK:
        if _PULSE_ON:
            return
        _PULSE_ON = True

    def _run() -> None:
        global _PULSE_ON
        try:
            fetch_quote(api_key, "QQQ")
        finally:
            with _PULSE_LOCK:
                _PULSE_ON = False

    threading.Thread(target=_run, daemon=True, name="fmp-pulse").start()


def _f(x):
    """안전한 float 변환. 실패 시 None."""
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def fmp_get(path: str, api_key: str, params: dict | None = None, retries: int = 2):
    """GET 호출. 성공 시 JSON, 실패 시 None."""
    global LAST_HTTP_STATUS
    if not api_key:
        return None
    p = dict(params or {})
    p["apikey"] = api_key
    url = f"{FMP_BASE}/{path.lstrip('/')}"
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=p, timeout=_TIMEOUT)
            LAST_HTTP_STATUS = int(r.status_code)
            if r.status_code == 429:  # 레이트리밋
                time.sleep(1.2 * (i + 1))
                continue
            r.raise_for_status()
            _stamp_fmp(r, ok=True)
            return r.json()
        except Exception:
            time.sleep(0.4 * (i + 1))
    _stamp_fmp(None, ok=False)
    return None


def _norm_quote(q: dict | None) -> dict | None:
    if not q:
        return None
    return {
        "symbol": q.get("symbol"),
        "name": q.get("name"),
        "price": _f(q.get("price")),
        "open": _f(q.get("open")),
        "high": _f(q.get("dayHigh")),
        "low": _f(q.get("dayLow")),
        "volume": _f(q.get("volume")),
        "avg_volume": _f(q.get("avgVolume")),
        "market_cap": _f(q.get("marketCap")),
        "prev_close": _f(q.get("previousClose")),
        "change_pct": _f(q.get("changePercentage", q.get("changesPercentage"))),
        "timestamp": q.get("timestamp"),
    }


def fetch_quote(api_key: str, symbol: str) -> dict | None:
    data = fmp_get("quote", api_key, {"symbol": symbol.strip().upper()})
    if isinstance(data, list) and data:
        nq = _norm_quote(data[0])
        if nq and nq.get("price"):
            return nq
    return None


def fetch_eod(api_key: str, symbol: str, days: int = 140) -> pd.DataFrame | None:
    """일봉(오름차순). 컬럼: date, open, high, low, close, volume."""
    end = dt.datetime.now(ET).date() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(days * 1.6))
    data = fmp_get(
        "historical-price-eod/full",
        api_key,
        {"symbol": symbol.strip().upper(), "from": start.isoformat(), "to": end.isoformat()},
    )
    if isinstance(data, dict):
        data = data.get("historical")
    if not data:
        return None
    df = pd.DataFrame(data)
    need = ["date", "open", "high", "low", "close", "volume"]
    if not set(need).issubset(df.columns):
        return None
    df = df[need].copy()
    df["date"] = pd.to_datetime(df["date"])
    for c in need[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().sort_values("date").reset_index(drop=True)
    return df if len(df) else None


def sma(series: pd.Series, n: int) -> float | None:
    if series is None or len(series) < n:
        return None
    return float(series.tail(n).mean())
