"""PEAD 손절이 만기보다 앞선다. 폐기 전략은 LEGACY."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener.portfolio import guardian  # noqa: E402

base = {
    "ticker": "TEST", "strategy": "PEAD", "hold_bdays": 1, "has_quote": True,
    "r_mult": 0.0, "pct": 1.0, "pnl_pct": 1.0,
}
sl = guardian({**base, "pct": -5.0, "pnl_pct": -5.0})
time_exit = guardian({**base, "hold_bdays": 15})
legacy = guardian({**base, "strategy": "OLD"})
assert sl["code"] == "SL", sl
assert time_exit["code"] == "TIME_EXIT", time_exit
assert sl["rank"] < time_exit["rank"]
assert legacy["code"] == "LEGACY", legacy
print("PASS", sl["code"], time_exit["code"], legacy["code"])
