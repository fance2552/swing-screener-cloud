"""Rebuild data/seed from the Mac EOD cache and Yahoo short float.

Run on the Mac, from the cloud repo root, after a local scan has written
~/Library/Caches/ldpb-screener/eod_YYYY-MM-DD_1y.pkl. Then commit the two
seed files and push. Streamlit Cloud does not have that cache, and Yahoo
blocks its datacenter IPs.
"""
from __future__ import annotations

import gzip
import json
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests
import yfinance as yf

from screener.event_driven import _pct_from_yf_info

ROOT = Path(__file__).resolve().parent
SEED_DIR = ROOT / "seed"
CACHE_DIR = Path.home() / "Library" / "Caches" / "ldpb-screener"


def _latest_cache() -> Path:
    files = sorted(CACHE_DIR.glob("eod_*_1y.pkl"))
    if not files:
        raise SystemExit(f"no EOD cache in {CACHE_DIR}")
    return files[-1]


def write_eod_seed() -> tuple[Path, str, list[str]]:
    raw = pickle.loads(_latest_cache().read_bytes())
    symbols: dict[str, tuple] = {}
    last = ""
    for sym, df in raw.items():
        if df is None or getattr(df, "empty", True):
            continue
        if "Close" not in df.columns or "Volume" not in df.columns:
            continue
        tail = df.tail(260)
        dates = [pd.Timestamp(i).strftime("%Y-%m-%d") for i in tail.index]
        closes = [round(float(x), 4) for x in tail["Close"].tolist()]
        vols = [int(float(x)) for x in tail["Volume"].tolist()]
        symbols[str(sym).upper()] = (dates, closes, vols)
        last = dates[-1]
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    path = SEED_DIR / "eod_seed.pkl.gz"
    with gzip.open(path, "wb") as fh:
        pickle.dump({"asof": last, "symbols": symbols}, fh, protocol=4)
    print(f"eod {len(symbols)} asof {last} {path.stat().st_size} bytes")
    return path, last, sorted(symbols)


def write_short_seed(symbols: list[str], asof: str) -> None:
    dest = SEED_DIR / "short_float_seed.json"
    have: dict[str, list[float]] = {}
    if dest.exists():
        try:
            old = json.loads(dest.read_text(encoding="utf-8"))
            if isinstance(old.get("symbols"), dict):
                have = old["symbols"]
        except (OSError, ValueError):
            have = {}
    pending = [s for s in symbols if s not in have]
    print(f"short have {len(have)} pending {len(pending)}")
    for i, sym in enumerate(pending, 1):
        pct = 0.0
        dtc = 0.0
        for attempt in range(3):
            try:
                session = requests.Session()
                session.headers["User-Agent"] = "Mozilla/5.0"
                info = yf.Ticker(sym, session=session).info or {}
                pct, dtc = _pct_from_yf_info(info)
                break
            except Exception:
                time.sleep(8 + attempt * 6)
        if pct > 0:
            have[sym] = [round(float(pct), 2), round(float(dtc), 2)]
        if i % 25 == 0 or i == len(pending):
            dest.write_text(
                json.dumps({"asof": asof, "symbols": have}, separators=(",", ":")),
                encoding="utf-8",
            )
            print(f"short {i}/{len(pending)} saved {len(have)}")
        time.sleep(0.6)
    print(f"short total {len(have)}")


if __name__ == "__main__":
    _, asof, symbols = write_eod_seed()
    write_short_seed(symbols, asof)
