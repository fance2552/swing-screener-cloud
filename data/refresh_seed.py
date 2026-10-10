"""Rebuild data/seed/eod_seed.pkl.gz from the Mac EOD cache.

Run on the Mac, from the cloud repo root, after a local scan has written
~/Library/Caches/ldpb-screener/eod_YYYY-MM-DD_1y.pkl. Then commit the seed
and push. Streamlit Cloud does not have that cache.
"""
from __future__ import annotations

import gzip
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

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


if __name__ == "__main__":
    write_eod_seed()
