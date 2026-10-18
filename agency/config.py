"""에이전시 임계값. 여기 외에는 숫자를 박지 않는다."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / ".cache"
CAPABILITIES_PATH = CACHE_DIR / "fmp_capabilities.json"
FUND_CACHE_DIR = CACHE_DIR / "fundamentals"
LAST_REPORT_PATH = CACHE_DIR / "agency_last.json"

# Phase 0 프로브 결과. .cache 파일이 없는 Cloud에서도 402 엔드포인트를 치지 않는다.
DEFAULT_CAPABILITIES = {
    "income-statement": {"ok": True, "period_used": "quarter"},
    "balance-sheet-statement": {"ok": True, "period_used": "quarter"},
    "cash-flow-statement": {"ok": True, "period_used": "quarter"},
    "ratios": {"ok": False, "period_used": "quarter"},
    "ratios-ttm": {"ok": True, "period_used": None},
    "key-metrics": {"ok": False, "period_used": "quarter"},
    "key-metrics-ttm": {"ok": True, "period_used": None},
    "profile": {"ok": True, "period_used": None},
    "earnings": {"ok": True, "period_used": None},
    "news/stock": {"ok": True, "period_used": None},
}

FINANCIAL_SECTORS = frozenset({
    "Financial Services", "Financial", "Banks", "Insurance",
    "Capital Markets", "Asset Management",
})

FACT_KEYS = (
    "revenue_yoy_pct", "revenue_trend", "gross_margin_pct", "operating_margin_pct",
    "op_margin_change_pp", "net_income_positive_count", "op_cashflow", "fcf",
    "debt_to_equity", "current_ratio", "interest_coverage", "cash", "total_debt",
    "valuation", "earnings_surprise_pct",
)

FUND_SEV = {"PASS": 0, "HOLD": 1, "REJECT": 2}
DEC_SEV = {"GO": 0, "CAUTION": 1, "BLOCK": 2}


@dataclass
class AgencyCfg:
    max_scan: int = 3
    max_manual: int = 3
    max_positions: int = 3
    max_open_risk_pct: float = 3.0
    max_new_per_day: int = 2
    max_position_pct: float = 35.0
    max_same_sector: int = 2
    block_on_correction: bool = True
    revenue_yoy_min_pct: float = 0.0
    operating_margin_min_pct: float = 0.0
    margin_shrink_pp: float = -5.0
    current_ratio_min: float = 1.0
    debt_to_equity_max: float = 2.0
    fcf_min: float = 0.0
    loss_streak_max: int = 1
    floor_hold_min_flags: int = 1
    floor_reject_min_flags: int = 3
    fund_cache_hours: float = 24.0
    cache_dir: Path = field(default_factory=lambda: FUND_CACHE_DIR)
    capabilities_path: Path = field(default_factory=lambda: CAPABILITIES_PATH)
    last_report_path: Path = field(default_factory=lambda: LAST_REPORT_PATH)
