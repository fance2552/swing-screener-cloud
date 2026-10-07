"""청산 랭크. TRAIL 과 SQZ_TP 는 rank 3 을 일부러 공유한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from screener.portfolio import guardian  # noqa: E402

base = {
    "ticker": "TEST", "strategy": "EARNINGS", "hold_bdays": 1, "has_quote": True,
    "r_mult": 0.0, "target_consensus": 0.0, "upside": 0.0,
    "d_day": None, "force_exit_d3": False,
    "entry_price": 100.0, "current_price": 100.0, "peak_price": 100.0,
}
cases = {
    "SL": {**base, "pct": -float(config.EXIT_SL_PCT) - 1.0, "current_price": 95.0},
    "D3": {**base, "pct": 0.0, "force_exit_d3": True, "d_day": 2},
    "TRAIL": {
        **base, "pct": 0.8, "current_price": 100.8, "peak_price": 104.0,
    },
}
seen = {}
for name, m in cases.items():
    g = guardian(m)
    assert g["code"] == name, f"{name} 조건인데 {g['code']}가 떴다: {g}"
    seen[name] = g["rank"]
    print(f"PASS: {name} -> rank={g['rank']}")
sqz = guardian({
    **base, "strategy": "SQUEEZE", "pct": float(config.SQUEEZE_EXIT_TP_PCT),
    "current_price": 112.0, "hold_bdays": 1,
})
assert sqz["code"] == "SQZ_TP", sqz
assert sqz["rank"] == seen["TRAIL"] == 3
print("PASS: TRAIL == SQZ_TP == 3 (의도된 동점)")
assert len({seen["SL"], seen["D3"], seen["TRAIL"]}) == 3
print("PASS: SL/D3/TRAIL rank 분리", seen)
