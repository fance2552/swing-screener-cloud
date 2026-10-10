"""Daily bars: FMP EOD. Cache is a seed. Stale last bars get parallel backfill."""
from __future__ import annotations

import gzip
import os
import pickle
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

import config
import env_settings
from data import fmp
from state import SHARED

try:
    import certifi

    _ca = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", _ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
    os.environ.setdefault("CURL_CA_BUNDLE", _ca)
except Exception:  # noqa: BLE001
    pass

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    _ET = None

CACHE_DIR = Path.home() / "Library" / "Caches" / "ldpb-screener"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
# Bundled bars for Streamlit Cloud. That disk has no ~/Library/Caches seed,
# and a 2,500-symbol FMP fan-out dies in 429s before the scan finishes.
SEED_PATH = Path(__file__).resolve().parent / "seed" / "eod_seed.pkl.gz"
_MEM: dict[str, pd.DataFrame] = {}
_DISK_LOADED = False
_SEED_LOADED = False
_WIDE_GAP = 40


def _store_path(period: str) -> Path:
    return CACHE_DIR / f"eod_{date.today().isoformat()}_{period}.pkl"


def expected_eod_date() -> date:
    """Last completed US cash session (no holiday calendar)."""
    now = datetime.now(_ET) if _ET is not None else datetime.utcnow()
    d = now.date()
    minutes = now.hour * 60 + now.minute
    cutoff = 16 * 60 + 10 if _ET is not None else 20 * 60 + 10
    if minutes < cutoff:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _last_bar_date(df: pd.DataFrame) -> date | None:
    if df is None or getattr(df, "empty", True):
        return None
    return pd.Timestamp(df.index[-1]).date()


def _is_fresh(df: pd.DataFrame, asof: date) -> bool:
    last = _last_bar_date(df)
    return last is not None and last >= asof


def _merge_bars(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if new is None or new.empty:
        return old if old is not None else pd.DataFrame()
    if old is None or old.empty:
        return new
    df = pd.concat([old, new])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _load_disk(period: str) -> None:
    global _DISK_LOADED, _MEM
    if _DISK_LOADED:
        return
    path = _store_path(period)
    if path.exists():
        try:
            blob = pd.read_pickle(path)
            if isinstance(blob, dict):
                _MEM.update(blob)
        except Exception:  # noqa: BLE001
            pass
    _DISK_LOADED = True


def _save_disk(period: str) -> None:
    try:
        pd.to_pickle(_MEM, _store_path(period))
    except Exception:  # noqa: BLE001
        pass


def _frame_from_packed(packed) -> pd.DataFrame | None:
    """Seed row is (dates, closes, volumes). Scanners only need Close and Volume."""
    if not isinstance(packed, (list, tuple)) or len(packed) != 3:
        return None
    dates, closes, vols = packed
    if not dates:
        return None
    idx = pd.to_datetime(list(dates))
    df = pd.DataFrame({"Close": list(closes), "Volume": list(vols)}, index=idx)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _load_seed() -> int:
    """Fill holes from the bundled seed. Never overwrite a frame already in memory."""
    global _SEED_LOADED
    if _SEED_LOADED:
        return 0
    _SEED_LOADED = True
    if not SEED_PATH.exists():
        return 0
    try:
        with gzip.open(SEED_PATH, "rb") as fh:
            blob = pickle.load(fh)
    except Exception:  # noqa: BLE001
        return 0
    symbols = blob.get("symbols") if isinstance(blob, dict) else None
    if not isinstance(symbols, dict):
        return 0
    added = 0
    for sym, packed in symbols.items():
        key = str(sym).upper()
        cached = _MEM.get(key)
        if cached is not None and not getattr(cached, "empty", True):
            continue
        frame = _frame_from_packed(packed)
        if frame is None or frame.empty:
            continue
        _MEM[key] = frame
        added += 1
    return added


def _session_days_after(last: date, asof: date, cap: int = 12) -> list[date]:
    days: list[date] = []
    d = asof
    while d > last and len(days) < cap:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return days


def _bulk_extend(symbols: list[str], asof: date) -> int:
    """Append the missing sessions with one FMP bulk call per day. 0 if the call is rejected."""
    from slog import slog

    lasts = []
    for sym in symbols:
        last = _last_bar_date(_MEM.get(sym))
        if last is not None:
            lasts.append(last)
    if not lasts:
        return 0
    want = set(symbols)
    applied = 0
    for day in _session_days_after(min(lasts), asof):
        book = fmp.eod_bulk(day)
        if book is None:
            slog("EOD 벌크 거절. 가진 일봉으로 채점한다.")
            break
        if not book:
            continue
        ts = pd.Timestamp(day)
        for sym, (close, vol) in book.items():
            if sym not in want:
                continue
            row = pd.DataFrame({"Close": [close], "Volume": [vol]}, index=[ts])
            old = _MEM.get(sym)
            _MEM[sym] = _merge_bars(old if old is not None else pd.DataFrame(), row)
        applied += 1
    return applied


def _absorb(out: dict[str, pd.DataFrame], symbols: list[str]) -> int:
    """Keep a stale frame. An old session still beats an empty board."""
    kept = 0
    for sym in symbols:
        if sym in out:
            continue
        frame = _MEM.get(sym)
        if frame is None or getattr(frame, "empty", True):
            continue
        out[sym] = frame
        kept += 1
    return kept


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [str(c[-1]).title() for c in out.columns]
    else:
        out.columns = [str(c).title() for c in out.columns]
    out = out.rename(columns={"Adj Close": "Close"})
    keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in out.columns]
    return out[keep].dropna(how="all")


def download_daily(tickers: list[str], period: str = config.YF_PERIOD) -> dict[str, pd.DataFrame]:
    from slog import slog

    _load_disk(period)
    seeded = _load_seed()
    if seeded:
        slog(f"일봉 시드 {seeded}종")
    uniq: list[str] = []
    seen: set[str] = set()
    for t in tickers:
        u = t.upper().strip()
        if u and u not in seen and u.isascii():
            seen.add(u)
            uniq.append(u)

    asof = expected_eod_date()
    out: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    stale: list[str] = []
    stale_from: dict[str, str] = {}

    for t in uniq:
        cached = _MEM.get(t)
        if cached is None or getattr(cached, "empty", True):
            missing.append(t)
            continue
        if _is_fresh(cached, asof):
            out[t] = cached
        else:
            stale.append(t)
            last = _last_bar_date(cached) or (asof - timedelta(days=14))
            stale_from[t] = (last - timedelta(days=7)).isoformat()

    slog(
        f"일봉 캐시 {len(out)} 신선 / 결측 {len(missing)} / 만료 {len(stale)} "
        f"(세션 {asof.isoformat()})"
    )

    def _progress(done: int, total: int, sym: str, tag: str) -> None:
        SHARED.set_phase(f"{tag} {done}/{total} {sym}")
        SHARED.scan_done = done
        SHARED.scan_total = total
        if done == 1 or done % 25 == 0 or done == total:
            slog(f"{tag} {done}/{total} {sym}")

    wide = len(missing) >= _WIDE_GAP or len(stale) >= _WIDE_GAP
    if not env_settings.fmp_key():
        wide = False

    if wide:
        # A few thousand per-symbol calls is what zeroed the cloud board.
        applied = _bulk_extend(stale, asof) if stale else 0
        refreshed = 0
        for t in list(stale):
            frame = _MEM.get(t)
            if frame is not None and not getattr(frame, "empty", True) and _is_fresh(frame, asof):
                out[t] = frame
                refreshed += 1
        slog(f"EOD 벌크 {applied}일 / 신선 복귀 {refreshed}/{len(stale)}")
        if missing and len(missing) <= _WIDE_GAP and env_settings.fmp_key():
            got = fmp.historical_many(missing, progress=lambda d, n, s: _progress(d, n, s, "EOD"))
            for t, df in got.items():
                _MEM[t] = df
                out[t] = df
            slog(f"EOD 신규 완료 {len(got)}/{len(missing)}")
        elif missing and not out and env_settings.fmp_key():
            slog(f"시드 없음. FMP EOD 개별 다운로드 {len(missing)}종")
            got = fmp.historical_many(missing, progress=lambda d, n, s: _progress(d, n, s, "EOD"))
            for t, df in got.items():
                _MEM[t] = df
                out[t] = df
            slog(f"EOD 신규 완료 {len(got)}/{len(missing)}")
        elif missing:
            slog(f"결측 {len(missing)}종은 시드 밖. 대량 개별 다운로드는 건너뛴다.")
    else:
        if missing and env_settings.fmp_key():
            slog(f"FMP EOD 신규 다운로드 {len(missing)}종 병렬")
            got = fmp.historical_many(missing, progress=lambda d, n, s: _progress(d, n, s, "EOD"))
            for t, df in got.items():
                _MEM[t] = df
                out[t] = df
            slog(f"EOD 신규 완료 {len(got)}/{len(missing)}")

        if stale and env_settings.fmp_key():
            slog(f"FMP EOD 최신 백필 {len(stale)}종 병렬")
            got = fmp.historical_many(stale, progress=lambda d, n, s: _progress(d, n, s, "BACKFILL"), from_dates=stale_from)
            filled = 0
            for t in stale:
                old = _MEM.get(t)
                new = got.get(t)
                merged = _merge_bars(old if old is not None else pd.DataFrame(), new if new is not None else pd.DataFrame())
                if merged is not None and not merged.empty:
                    _MEM[t] = merged
                    out[t] = merged
                    filled += 1
            slog(f"EOD 백필 완료 {filled}/{len(stale)}")

    kept = _absorb(out, uniq)
    if kept:
        slog(f"일봉 만료 {kept}종은 직전 세션으로 채점")
    if missing or stale:
        _save_disk(period)
    slog(f"일봉 확보 {len(out)}/{len(uniq)}")
    return out


def download_intraday(ticker: str, interval: str) -> pd.DataFrame:
    """분봉 경로는 제거됐다. 호출부는 일봉으로 되돌아간다."""
    del ticker, interval
    return pd.DataFrame()


def spy() -> pd.DataFrame:
    data = download_daily(["SPY"], period="1y")
    return data.get("SPY", pd.DataFrame())


def wikipedia_universe() -> list[dict]:
    frames = []
    urls = [
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://en.wikipedia.org/wiki/Nasdaq-100",
        "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    ]
    for url in urls:
        try:
            tables = pd.read_html(url)
        except Exception:  # noqa: BLE001
            continue
        for tbl in tables:
            cols = {str(c).lower(): c for c in tbl.columns}
            sym = cols.get("symbol") or cols.get("ticker")
            name = cols.get("security") or cols.get("company") or cols.get("name")
            if not sym:
                continue
            chunk = tbl[[sym] + ([name] if name else [])].copy()
            chunk.columns = ["symbol", "companyName"][: len(chunk.columns)]
            frames.append(chunk)
            break
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    df["symbol"] = df["symbol"].astype(str).str.replace(".", "-", regex=False).str.upper()
    df = df.drop_duplicates("symbol")
    rows = []
    for rec in df.to_dict("records"):
        rows.append(
            {
                "symbol": rec["symbol"],
                "companyName": rec.get("companyName") or rec["symbol"],
                "marketCap": config.MARKET_CAP_MIN + 1,
            }
        )
    return rows
