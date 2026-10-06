"""3번 수정 검증: SL/TP/D3가 서로 다른 rank를 받는다 (동순위 충돌 제거 확인)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from screener.portfolio import guardian  # noqa: E402

base = {
    "ticker": "TEST", "strategy": "EARNINGS", "hold_bdays": 1, "has_quote": True,
    "r_mult": 0.0, "target_consensus": 0.0, "upside": 0.0,
    "d_day": None, "force_exit_d3": False,
}
cases = {
    "SL": {**base, "pct": -float(config.EXIT_SL_PCT) - 1.0},
    "TP": {**base, "pct": float(config.EXIT_TP_PCT) + 1.0},
    "D3": {**base, "pct": 0.0, "force_exit_d3": True, "d_day": 2},
}
seen_ranks = set()
for name, m in cases.items():
    g = guardian(m)
    assert g["code"] == name, f"{name} 조건인데 {g['code']}가 떴다: {g}"
    assert g["rank"] not in seen_ranks, f"rank 충돌 발생: {name} -> {g['rank']} (이미 사용됨)"
    seen_ranks.add(g["rank"])
    print(f"PASS: {name} -> rank={g['rank']}")
print("PASS: 모든 rank가 서로 다름 =", sorted(seen_ranks))
# 참고: FRIDAY FLAT은 is_friday_flat_window()가 실제 요일에 의존하므로
# 금요일이 아닌 날 실행하면 이 케이스는 검증에서 빠진다. 정상이다.
