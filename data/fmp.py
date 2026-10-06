"""FMP Standard: screener + quote + EOD history. Never log keys. No shared Session."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any

import pandas as pd
import requests

import config
import env_settings

TIMEOUT = 20
_EXCHANGES = {x.upper() for x in config.SCREENER_EXCHANGES}


def _get(url: str, params: dict[str, Any]) -> Any:
    last_exc: Exception | None = None
    for attempt in range(5):
        r = None
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(1.0 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.35 * (attempt + 1))
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:  # noqa: BLE001
                    pass
    if last_exc:
        raise last_exc
    return None


def ping() -> bool:
    key = env_settings.fmp_key()
    if not key:
        return False
    try:
        data = _get(
            f"{env_settings.fmp_base()}/stable/quote",
            {"symbol": "SPY", "apikey": key},
        )
        return bool(data)
    except Exception:  # noqa: BLE001
        return False


def _screener_once(params: dict[str, Any]) -> list[dict[str, Any]]:
    base = env_settings.fmp_base()
    data = _get(f"{base}/stable/company-screener", params)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data.get("Error Message"):
        raise RuntimeError(str(data.get("Error Message")))
    return []


def _keep_row(row: dict[str, Any]) -> bool:
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym or not sym.replace("-", "").replace(".", "").isalnum():
        return False
    exch = str(row.get("exchangeShortName") or row.get("exchange") or "").upper()
    nyse = "NYSE" in exch or "NEW YORK" in exch
    nasdaq = "NASDAQ" in exch
    if not (nyse or nasdaq):
        return False
    try:
        price = float(row.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    try:
        mcap = float(row.get("marketCap") or 0)
    except (TypeError, ValueError):
        mcap = 0.0
    if price < config.PRICE_MIN:
        return False
    if mcap and mcap < config.MARKET_CAP_MIN:
        return False
    return True


def company_screener() -> list[dict[str, Any]]:
    """NYSE+NASDAQ quality universe. Target ~2000–3000, not a 500-name cap."""
    key = env_settings.fmp_key()
    if not key:
        raise RuntimeError("FMP_API_KEY 없음")

    base = {
        "priceMoreThan": int(config.PRICE_MIN),
        "marketCapMoreThan": int(config.MARKET_CAP_MIN),
        "volumeMoreThan": int(config.VOLUME_MORE_THAN),
        "exchange": ",".join(config.SCREENER_EXCHANGES),
        "isActivelyTrading": "true",
        "isEtf": "false",
        "isFund": "false",
        "limit": int(config.SCREENER_LIMIT),
        "apikey": key,
    }

    merged: dict[str, dict[str, Any]] = {}

    def _ingest(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            if not _keep_row(row):
                continue
            sym = str(row.get("symbol") or "").upper()
            merged[sym] = row

    _ingest(_screener_once(base))

    # volumeMoreThan=1M share vol cuts the tape. Fill without it to hit 2000–3000.
    if len(merged) < 2000:
        fill = dict(base)
        fill.pop("volumeMoreThan", None)
        _ingest(_screener_once(fill))

    if len(merged) < 2000:
        for exch in config.SCREENER_EXCHANGES:
            extra = dict(base)
            extra["exchange"] = exch
            extra.pop("volumeMoreThan", None)
            _ingest(_screener_once(extra))

    if not merged:
        raise RuntimeError("FMP company-screener 빈 응답")
    return list(merged.values())


def _one_quote(ticker: str, key: str) -> dict[str, Any] | None:
    try:
        data = _get(
            f"{env_settings.fmp_base()}/stable/quote",
            {"symbol": ticker, "apikey": key},
        )
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not data.get("symbol"):
        return None
    chg = data.get("changePercentage")
    if chg is None:
        chg = data.get("changesPercentage") or 0
    return {
        "price": float(data.get("price") or 0),
        "volume": float(data.get("volume") or 0),
        "name": str(data.get("name") or ""),
        "change_pct": float(chg or 0),
        "day_high": float(data.get("dayHigh") or data.get("high") or 0),
        "day_low": float(data.get("dayLow") or data.get("low") or 0),
        "source": "fmp",
        "symbol": str(data.get("symbol")).upper(),
    }


def quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:
    key = env_settings.fmp_key()
    uniq = [t.upper() for t in tickers if t]
    if not key or not uniq:
        return {}
    out: dict[str, dict[str, Any]] = {}
    workers = min(5, len(uniq))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one_quote, t, key) for t in uniq]
        for fut in as_completed(futs):
            try:
                row = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if row and row.get("price"):
                out[row["symbol"]] = row
    return out


def _bars_from_payload(data: Any) -> pd.DataFrame:
    rows = data
    if isinstance(data, dict):
        rows = data.get("historical") or data.get("records") or []
    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"], utc=False, errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").set_index("date")
    rename = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    df = df.rename(columns=rename)
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
    if len(keep) < 5:
        return pd.DataFrame()
    out = df[keep].astype(float)
    return out[~out.index.duplicated(keep="last")]


def historical_daily(ticker: str, from_dt: str | None = None) -> pd.DataFrame:
    key = env_settings.fmp_key()
    if not key or not ticker:
        return pd.DataFrame()
    start = from_dt or (date.today() - timedelta(days=400)).isoformat()
    try:
        data = _get(
            f"{env_settings.fmp_base()}/stable/historical-price-eod/full",
            {"symbol": ticker.upper(), "from": start, "apikey": key},
        )
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    return _bars_from_payload(data)


def historical_many(
    tickers: list[str],
    progress=None,
    from_dates: dict[str, str] | None = None,
) -> dict[str, pd.DataFrame]:
    uniq = [t.upper() for t in tickers if t]
    out: dict[str, pd.DataFrame] = {}
    if not uniq or not env_settings.fmp_key():
        return out
    starts = from_dates or {}

    def _one(sym: str) -> tuple[str, pd.DataFrame]:
        return sym, historical_daily(sym, from_dt=starts.get(sym))

    done = 0
    total = len(uniq)
    workers = min(int(config.EOD_WORKERS), max(1, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one, t) for t in uniq]
        for fut in as_completed(futs):
            done += 1
            try:
                sym, df = fut.result()
            except Exception:  # noqa: BLE001
                if progress:
                    progress(done, total, "")
                continue
            if df is not None and not df.empty:
                out[sym] = df
            if progress:
                progress(done, total, sym)
    return out


def _analyst_once(url: str, params: dict[str, Any]) -> Any:
    """Single GET. 403/empty → None. Do not use from the 1s heartbeat."""
    r = None
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        return None
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass


def fetch_fmp_analyst_data(ticker: str) -> dict[str, float]:
    """Wall Street consensus. Call once at position register. Never from the 1s loop."""
    empty = {"target_consensus": 0.0, "buy_ratio": 0.0, "strong_buys": 0.0, "buys": 0.0}
    key = env_settings.fmp_key()
    sym = (ticker or "").upper().strip()
    if not key or not sym:
        return empty
    base = env_settings.fmp_base()

    target = 0.0
    for url, params in (
        (f"{base}/api/v4/price-target-consensus", {"symbol": sym, "apikey": key}),
        (f"{base}/stable/price-target-consensus", {"symbol": sym, "apikey": key}),
    ):
        data = _analyst_once(url, params)
        row = data[0] if isinstance(data, list) and data else data
        if not isinstance(row, dict):
            continue
        try:
            target = float(row.get("targetConsensus") or row.get("targetMedian") or 0)
        except (TypeError, ValueError):
            target = 0.0
        if target > 0:
            break

    strong = buys = holds = sells = 0.0
    rec_urls = (
        (f"{base}/api/v3/analyst-stock-recommendations/{sym}", {"apikey": key}),
        (f"{base}/stable/analyst-stock-recommendations", {"symbol": sym, "apikey": key}),
        (f"{base}/stable/grades-consensus", {"symbol": sym, "apikey": key}),
    )
    for url, params in rec_urls:
        data = _analyst_once(url, params)
        rows = data if isinstance(data, list) else []
        if not rows and isinstance(data, dict):
            rows = [data]
        if not rows or not isinstance(rows[0], dict):
            continue
        rec = rows[0]

        def _n(*names: str) -> float:
            for n in names:
                if rec.get(n) is not None:
                    try:
                        return float(rec.get(n) or 0)
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0

        strong = _n("analystRatingsStrongBuy", "strongBuy", "analystRatingsstrongBuy")
        buys = _n("analystRatingsBuy", "analystRatingsbuy", "buy")
        holds = _n("analystRatingsHold", "hold")
        sells = _n("analystRatingsSell", "analystRatingssell", "sell")
        strong_sell = _n("analystRatingsStrongSell", "strongSell")
        if strong or buys or holds or sells or strong_sell:
            sells = sells + strong_sell
            break

    total = strong + buys + holds + sells
    buy_ratio = ((strong + buys) / total * 100.0) if total > 0 else 0.0
    return {
        "target_consensus": target,
        "buy_ratio": round(buy_ratio, 1),
        "strong_buys": strong,
        "buys": buys,
    }


analyst_snapshot = fetch_fmp_analyst_data
