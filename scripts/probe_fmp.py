"""FMP 재무 엔드포인트 권한 프로브. 키와 apikey 쿼리는 출력하지 않는다."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import env_settings  # noqa: E402

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 12
CACHE = ROOT / ".cache" / "fmp_capabilities.json"

PROBES = [
    ("income-statement", {"symbol": "AAPL", "period": "quarter", "limit": 8}, ("revenue", "operatingIncome", "netIncome", "date")),
    ("income-statement", {"symbol": "AAPL", "period": "annual", "limit": 4}, ("revenue", "operatingIncome", "netIncome", "date")),
    ("balance-sheet-statement", {"symbol": "AAPL", "period": "quarter", "limit": 8}, ("totalDebt", "cashAndCashEquivalents", "totalStockholdersEquity", "totalCurrentAssets", "totalCurrentLiabilities")),
    ("balance-sheet-statement", {"symbol": "AAPL", "period": "annual", "limit": 4}, ("totalDebt", "cashAndCashEquivalents", "totalStockholdersEquity")),
    ("cash-flow-statement", {"symbol": "AAPL", "period": "quarter", "limit": 8}, ("operatingCashFlow", "freeCashFlow", "date")),
    ("cash-flow-statement", {"symbol": "AAPL", "period": "annual", "limit": 4}, ("operatingCashFlow", "freeCashFlow", "date")),
    ("ratios", {"symbol": "AAPL", "period": "quarter", "limit": 8}, ("currentRatio", "debtEquityRatio", "grossProfitMargin", "operatingProfitMargin")),
    ("ratios-ttm", {"symbol": "AAPL"}, ("currentRatioTTM", "debtToEquityRatioTTM", "grossProfitMarginTTM", "operatingProfitMarginTTM", "interestCoverageRatioTTM", "priceToEarningsRatioTTM")),
    ("key-metrics", {"symbol": "AAPL", "period": "quarter", "limit": 8}, ("peRatio", "enterpriseValue", "freeCashFlowPerShare")),
    ("key-metrics-ttm", {"symbol": "AAPL"}, ("enterpriseValueTTM", "marketCap", "earningsYieldTTM")),
    ("profile", {"symbol": "AAPL"}, ("sector", "industry", "marketCap", "companyName")),
    ("earnings", {"symbol": "AAPL", "limit": 2}, ("date", "epsActual", "epsEstimated")),
    ("news/stock", {"symbols": "AAPL", "limit": 2}, ("publishedDate", "title")),
]


def _safe_path(params: dict) -> str:
    shown = {k: v for k, v in params.items() if k.lower() != "apikey"}
    return shown


def _probe(path: str, params: dict, key: str) -> tuple[int | str, int, list[str], object]:
    q = dict(params)
    q["apikey"] = key
    try:
        resp = requests.get(f"{BASE}/{path}", params=q, timeout=TIMEOUT)
        status = resp.status_code
        data = None
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, list):
            rows = len(data)
            fields = sorted((data[0] or {}).keys()) if data and isinstance(data[0], dict) else []
        elif isinstance(data, dict):
            rows = 1
            fields = sorted(data.keys())
        else:
            rows = 0
            fields = []
        return status, rows, fields, data
    except Exception as exc:
        return type(exc).__name__, 0, [], None


def _inject_streamlit_secrets() -> None:
    """CLI에는 st.secrets가 없다. secrets.toml 값만 환경에 넣고 내용은 찍지 않는다."""
    path = ROOT / ".streamlit" / "secrets.toml"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().strip('"').strip("'")
        value = value.strip().strip('"').strip("'")
        if name and value and not str(os.environ.get(name) or "").strip():
            os.environ[name] = value


def main() -> int:
    _inject_streamlit_secrets()
    keys = env_settings.get_api_keys()
    key = str(keys.get("FMP_API_KEY") or os.environ.get("FMP_API_KEY") or "").strip()
    if not key:
        print("FMP_API_KEY 없음")
        return 1
    print("endpoint | params | HTTP | rows | 핵심 필드")
    cap: dict = {}
    for path, params, need in PROBES:
        status, rows, fields, data = _probe(path, params, key)
        present = [n for n in need if n in fields]
        missing = [n for n in need if n not in fields]
        ok = isinstance(status, int) and status == 200 and rows > 0 and not missing
        period = params.get("period")
        label = path if "period" not in params else f"{path}:{period}"
        prev = cap.get(path)
        if prev is None or (ok and not prev.get("ok")):
            cap[path] = {
                "ok": bool(ok),
                "period_used": period,
                "http": status,
                "rows": rows,
                "fields_present": present,
                "fields_missing": missing,
            }
        print(f"{path} | {_safe_path(params)} | {status} | {rows} | present={present} missing={missing}")
        url_path = urlparse(f"{BASE}/{path}").path
        assert "apikey" not in url_path
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {CACHE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
