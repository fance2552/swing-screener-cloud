"""재무·기업 분석가: FMP 수집 → 파이썬 계산 → LLM 해석."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from screener import fmp_client as fc

from .config import (
    DEFAULT_CAPABILITIES,
    FACT_KEYS,
    FINANCIAL_SECTORS,
    FUND_SEV,
    AgencyCfg,
)

KST = ZoneInfo("Asia/Seoul")

_ANALYZE_SYS = (
    "너는 미국 주식 재무·기업 분석가다. 아래 FACTS(JSON)에 있는 숫자만 근거로 판단한다.\n"
    "- FACTS에 없거나 null인 항목은 \"자료 없음\"이라고 쓰고 추측하지 않는다.\n"
    "- 새로운 수치, 가격, 목표주가, 미래 예측을 만들지 않는다.\n"
    "- 이 종목은 며칠~몇 주짜리 스윙 진입 후보다. 질문은 두 가지다:\n"
    "  (1) 재무 상태가 이번 촉매(실적/뉴스)를 뒷받침하는가?\n"
    "  (2) 재무상 위험 신호는 무엇인가?\n"
    "- red_flags가 있으면 반드시 concerns에 반영한다.\n"
    "JSON만 출력한다:\n"
    '{"verdict":"PASS|HOLD|REJECT","headline":"한 줄","strengths":["최대3"],'
    '"concerns":["최대3"],"one_off_risk":"일회성 이익 의심 여부 한 줄 또는 \'자료 없음\'",'
    '"confidence":"HIGH|MED|LOW"}'
)


def _f(x):
    return fc._f(x)


def _pct_points(raw) -> float | None:
    v = _f(raw)
    if v is None:
        return None
    if abs(v) <= 1.5:
        return v * 100.0
    return v


def load_capabilities(path: Path | None = None) -> dict:
    cfg = AgencyCfg()
    p = path or cfg.capabilities_path
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw:
            return raw
    except Exception:
        pass
    return dict(DEFAULT_CAPABILITIES)


def facts_all_none(facts: dict) -> bool:
    for k in FACT_KEYS:
        v = facts.get(k)
        if k == "revenue_trend":
            if isinstance(v, list) and v:
                return False
            continue
        if k == "valuation":
            if isinstance(v, dict) and any(x is not None for x in v.values()):
                return False
            continue
        if v is not None:
            return False
    return True


def floor_from_flags(red_flags: list, cfg: AgencyCfg | None = None) -> str:
    cfg = cfg or AgencyCfg()
    n = len(red_flags)
    if n >= cfg.floor_reject_min_flags:
        return "REJECT"
    if n >= cfg.floor_hold_min_flags:
        return "HOLD"
    return "PASS"


def merge_verdict(floor: str, llm_verdict: str | None) -> str:
    """더 엄격한 쪽. LLM은 완화할 수 없다."""
    a = floor if floor in FUND_SEV else "HOLD"
    if llm_verdict not in FUND_SEV:
        return a
    return a if FUND_SEV[a] >= FUND_SEV[llm_verdict] else llm_verdict


def apply_red_flags(facts: dict, cfg: AgencyCfg | None = None) -> list[str]:
    cfg = cfg or AgencyCfg()
    flags: list[str] = []
    yoy = facts.get("revenue_yoy_pct")
    if yoy is not None and yoy < cfg.revenue_yoy_min_pct:
        flags.append("revenue_decline")
    om = facts.get("operating_margin_pct")
    if om is not None and om < cfg.operating_margin_min_pct:
        flags.append("operating_loss")
    ch = facts.get("op_margin_change_pp")
    if ch is not None and ch <= cfg.margin_shrink_pp:
        flags.append("margin_shrink")
    cr = facts.get("current_ratio")
    if cr is not None and cr < cfg.current_ratio_min:
        flags.append("weak_liquidity")
    de = facts.get("debt_to_equity")
    if de is not None and de > cfg.debt_to_equity_max:
        flags.append("high_leverage")
    fcf = facts.get("fcf")
    if fcf is not None and fcf < cfg.fcf_min:
        flags.append("negative_fcf")
    ni = facts.get("net_income_positive_count")
    if ni is not None and ni <= cfg.loss_streak_max:
        flags.append("loss_streak")
    return flags


def _sort_rows(rows) -> list:
    if not isinstance(rows, list):
        return []
    out = [r for r in rows if isinstance(r, dict) and r.get("date")]
    out.sort(key=lambda r: str(r.get("date")))
    return out


def compute_facts(
    income_rows=None,
    balance_rows=None,
    cash_rows=None,
    ratios_ttm=None,
    profile=None,
    catalyst=None,
    period: str | None = "quarter",
) -> dict:
    income = _sort_rows(income_rows)
    balance = _sort_rows(balance_rows)
    cash = _sort_rows(cash_rows)
    rt = ratios_ttm[0] if isinstance(ratios_ttm, list) and ratios_ttm else (
        ratios_ttm if isinstance(ratios_ttm, dict) else {}
    )
    prof = profile[0] if isinstance(profile, list) and profile else (
        profile if isinstance(profile, dict) else {}
    )
    cat = catalyst if isinstance(catalyst, dict) else {}

    latest_inc = income[-1] if income else {}
    asof = str(latest_inc.get("date") or (balance[-1].get("date") if balance else "") or "")
    lag = 4 if (period or "quarter") == "quarter" else 1
    prev_inc = income[-1 - lag] if len(income) > lag else {}

    rev = _f(latest_inc.get("revenue"))
    prev_rev = _f(prev_inc.get("revenue"))
    yoy = None
    if rev is not None and prev_rev not in (None, 0):
        yoy = (rev / prev_rev - 1.0) * 100.0

    trend = [_f(r.get("revenue")) for r in income[-4:]]
    trend = [x for x in trend if x is not None]

    gp = _f(latest_inc.get("grossProfit"))
    oi = _f(latest_inc.get("operatingIncome"))
    gm = (gp / rev * 100.0) if gp is not None and rev not in (None, 0) else _pct_points(rt.get("grossProfitMarginTTM"))
    om = (oi / rev * 100.0) if oi is not None and rev not in (None, 0) else _pct_points(rt.get("operatingProfitMarginTTM"))

    prev_oi = _f(prev_inc.get("operatingIncome"))
    prev_rev2 = _f(prev_inc.get("revenue"))
    prev_om = (prev_oi / prev_rev2 * 100.0) if prev_oi is not None and prev_rev2 not in (None, 0) else None
    om_chg = (om - prev_om) if om is not None and prev_om is not None else None

    ni_vals = [_f(r.get("netIncome")) for r in income[-4:]]
    ni_pos = sum(1 for x in ni_vals if x is not None and x > 0) if any(x is not None for x in ni_vals) else None

    latest_cf = cash[-1] if cash else {}
    ocf = _f(latest_cf.get("operatingCashFlow"))
    fcf = _f(latest_cf.get("freeCashFlow"))

    latest_bs = balance[-1] if balance else {}
    cash_usd = _f(latest_bs.get("cashAndCashEquivalents"))
    debt = _f(latest_bs.get("totalDebt"))
    equity = _f(latest_bs.get("totalStockholdersEquity") or latest_bs.get("totalEquity"))
    tca = _f(latest_bs.get("totalCurrentAssets"))
    tcl = _f(latest_bs.get("totalCurrentLiabilities"))

    sector = str(prof.get("sector") or "")
    financial = sector in FINANCIAL_SECTORS

    de = None if financial else (
        (debt / equity) if debt is not None and equity not in (None, 0) else _f(rt.get("debtToEquityRatioTTM"))
    )
    cr = None if financial else (
        (tca / tcl) if tca is not None and tcl not in (None, 0) else _f(rt.get("currentRatioTTM"))
    )
    ie = _f(latest_inc.get("interestExpense"))
    ic = None if financial else (
        (oi / abs(ie)) if oi is not None and ie not in (None, 0) else _f(rt.get("interestCoverageRatioTTM"))
    )

    pe = _f(rt.get("priceToEarningsRatioTTM"))
    ps = _f(rt.get("priceToSalesRatioTTM"))
    valuation = {k: v for k, v in (("pe", pe), ("ps", ps)) if v is not None} or None

    surprise = _f(cat.get("eps_beat_pct") or cat.get("earnings_surprise_pct"))

    return {
        "data_period": period or None,
        "data_asof": asof or None,
        "revenue_yoy_pct": yoy,
        "revenue_trend": trend or None,
        "gross_margin_pct": gm,
        "operating_margin_pct": om,
        "op_margin_change_pp": om_chg,
        "net_income_positive_count": ni_pos,
        "op_cashflow": ocf,
        "fcf": fcf,
        "debt_to_equity": de,
        "current_ratio": cr,
        "interest_coverage": ic,
        "cash": cash_usd,
        "total_debt": debt,
        "valuation": valuation,
        "earnings_surprise_pct": surprise,
        "sector": sector or None,
        "industry": (str(prof.get("industry") or "") or None),
        "name": (str(prof.get("companyName") or "") or None),
        "red_flags": [],
    }


def _cache_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / f"{symbol.upper()}.json"


def _read_cache(path: Path, max_age_s: float) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    ts = float(raw.get("fetched_at") or 0)
    if ts <= 0 or (time.time() - ts) > max_age_s:
        return None
    return raw


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def fetch_fundamental_facts(
    symbol: str,
    api_key: str,
    cfg: AgencyCfg | None = None,
    catalyst: dict | None = None,
    capabilities: dict | None = None,
) -> tuple[dict, int]:
    """(facts, fmp_calls). 실패는 None 필드. 예외를 올리지 않는다."""
    cfg = cfg or AgencyCfg()
    sym = str(symbol or "").strip().upper()
    cap = capabilities if isinstance(capabilities, dict) else load_capabilities(cfg.capabilities_path)
    cache_dir = Path(cfg.cache_dir)
    cached = _read_cache(_cache_path(cache_dir, sym), cfg.fund_cache_hours * 3600)
    if cached and isinstance(cached.get("facts"), dict):
        facts = dict(cached["facts"])
        if catalyst and facts.get("earnings_surprise_pct") is None:
            facts["earnings_surprise_pct"] = _f(
                (catalyst or {}).get("eps_beat_pct") or (catalyst or {}).get("earnings_surprise_pct")
            )
        return facts, 0

    calls = 0
    raw: dict = {}

    def grab(path: str, params: dict):
        nonlocal calls
        info = cap.get(path) or {}
        if not info.get("ok"):
            return None
        calls += 1
        try:
            return fc.fmp_get(path, api_key, params)
        except Exception:
            return None

    period = (cap.get("income-statement") or {}).get("period_used") or "quarter"
    if api_key:
        raw["income"] = grab("income-statement", {"symbol": sym, "period": period, "limit": 8})
        raw["balance"] = grab("balance-sheet-statement", {"symbol": sym, "period": period, "limit": 8})
        raw["cash"] = grab("cash-flow-statement", {"symbol": sym, "period": period, "limit": 8})
        raw["ratios_ttm"] = grab("ratios-ttm", {"symbol": sym})
        raw["profile"] = grab("profile", {"symbol": sym})
    facts = compute_facts(
        raw.get("income"), raw.get("balance"), raw.get("cash"),
        raw.get("ratios_ttm"), raw.get("profile"), catalyst, period=period,
    )
    _write_json(_cache_path(cache_dir, sym), {
        "fetched_at": time.time(), "symbol": sym, "facts": facts,
    })
    return facts, calls


def _facts_hash(facts: dict) -> str:
    blob = json.dumps(facts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _llm_cache_path(cache_dir: Path, symbol: str, h: str, day: str) -> Path:
    return cache_dir / f"{symbol.upper()}.llm.{day}.{h}.json"


def analyze(symbol: str, facts: dict, cfg: AgencyCfg | None = None) -> dict:
    """플로어 + LLM. LLM 실패 시 플로어만. 숫자는 facts에서만."""
    from ai_advisor import chat_json, sanitize_text

    cfg = cfg or AgencyCfg()
    flags = apply_red_flags(facts, cfg)
    facts = dict(facts)
    facts["red_flags"] = flags
    floor = floor_from_flags(flags, cfg)
    empty = facts_all_none(facts)
    out = {
        "status": "OK",
        "floor": floor,
        "verdict": "NO_DATA" if empty else floor,
        "headline": "",
        "strengths": [],
        "concerns": [],
        "one_off_risk": "자료 없음",
        "confidence": "LOW",
        "facts": facts,
        "llm_calls": 0,
        "llm_status": "SKIPPED",
    }
    if empty:
        out["status"] = "NO_DATA"
        out["floor"] = "PASS"
        out["verdict"] = "NO_DATA"
        return out

    day = datetime.now(KST).date().isoformat()
    h = _facts_hash({k: facts.get(k) for k in (*FACT_KEYS, "red_flags")})
    cpath = _llm_cache_path(Path(cfg.cache_dir), symbol, h, day)
    try:
        cached = json.loads(cpath.read_text(encoding="utf-8"))
        if isinstance(cached, dict) and cached.get("verdict"):
            cached["facts"] = facts
            cached["llm_calls"] = 0
            cached["llm_status"] = "CACHE"
            return cached
    except Exception:
        pass

    parsed, _prov, errors = chat_json(_ANALYZE_SYS, json.dumps(facts, ensure_ascii=False, default=str), max_tokens=500)
    out["llm_calls"] = 1
    if not isinstance(parsed, dict):
        out["status"] = "LLM_FAILED"
        out["llm_status"] = "FAILED"
        out["headline"] = "AI 해석 실패 — 코드 플로어만 사용"
        out["concerns"] = list(flags)[:3]
        if errors:
            out["headline"] = sanitize_text(out["headline"])
        return out

    parsed = sanitize_text(parsed)
    llm_v = str(parsed.get("verdict") or "").upper()
    if llm_v not in FUND_SEV:
        llm_v = "HOLD"
    out["verdict"] = merge_verdict(floor, llm_v)
    out["headline"] = str(parsed.get("headline") or "")[:200]
    out["strengths"] = list(parsed.get("strengths") or [])[:3]
    out["concerns"] = list(parsed.get("concerns") or [])[:3]
    out["one_off_risk"] = str(parsed.get("one_off_risk") or "자료 없음")[:200]
    conf = str(parsed.get("confidence") or "MED").upper()
    out["confidence"] = conf if conf in ("HIGH", "MED", "LOW") else "MED"
    out["llm_status"] = "OK"
    out["status"] = "OK"
    _write_json(cpath, {k: v for k, v in out.items() if k != "facts"})
    out["facts"] = facts
    return out
