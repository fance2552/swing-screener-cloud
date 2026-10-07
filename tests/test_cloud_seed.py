"""Cloud seed: bundled bars and short-float must survive a dead Yahoo / thin download."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import fmp  # noqa: E402
from data import yf_download as yfd  # noqa: E402
from screener import event_driven as ed  # noqa: E402


def test_parse_eod_bulk():
    assert fmp.parse_eod_bulk({"Error Message": "restricted"}) is None
    assert fmp.parse_eod_bulk([]) == {}
    book = fmp.parse_eod_bulk([
        {"symbol": "smmt", "close": 12.5, "volume": 100},
        {"symbol": "", "close": 1, "volume": 1},
        {"symbol": "BAD", "close": 0, "volume": 5},
    ])
    assert book == {"SMMT": (12.5, 100.0)}


def test_frame_from_packed_and_session_gap():
    df = yfd._frame_from_packed((["2026-10-05", "2026-10-06"], [1.5, 2.5], [10, 30]))
    assert list(df["Close"]) == [1.5, 2.5]
    assert int(df["Volume"].iloc[-1]) == 30
    assert yfd._frame_from_packed(("only-one",)) is None
    # 2026-10-02 is Friday. Next sessions through Tuesday the 6th.
    assert yfd._session_days_after(date(2026, 10, 2), date(2026, 10, 6)) == [
        date(2026, 10, 5),
        date(2026, 10, 6),
    ]


def test_absorb_keeps_stale_frame():
    idx = pd.bdate_range("2026-09-01", periods=3)
    yfd._MEM["ZZKEEP"] = pd.DataFrame({"Close": [1, 2, 3], "Volume": [1, 1, 1]}, index=idx)
    try:
        out: dict = {}
        assert yfd._absorb(out, ["ZZKEEP", "MISSING"]) == 1
        assert out["ZZKEEP"]["Close"].iloc[-1] == 3
        assert yfd._absorb(out, ["ZZKEEP"]) == 0
    finally:
        yfd._MEM.pop("ZZKEEP", None)


def test_short_seed_fills_yahoo_gaps_and_does_not_override_live():
    previous = ed._SHORT_SEED
    ed._SHORT_SEED = {"asof": "2026-10-07", "symbols": {"SMMT": [22.5, 3.2], "ZERO": [0, 1]}}
    try:
        live: dict = {}
        asof = ed._fill_short_gaps(["SMMT", "ZERO", "NOPE"], live)
        assert asof == "2026-10-07"
        assert live["SMMT"]["short_float_pct"] == 22.5
        assert live["SMMT"]["days_to_cover"] == 3.2
        assert "ZERO" not in live
        live_hit = {"SMMT": {"short_float_pct": 9.0, "days_to_cover": 1.0}}
        assert ed._fill_short_gaps(["SMMT"], live_hit) == ""
        assert live_hit["SMMT"]["short_float_pct"] == 9.0
    finally:
        ed._SHORT_SEED = previous
