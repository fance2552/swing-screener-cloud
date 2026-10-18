"""에이전시 파이프라인: 5직원 순서 → 보고서 dict."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from ai_advisor import chat_json, position_size, sanitize_text
from screener import episodic_pivot as ep

from .config import AgencyCfg, DEC_SEV
from .fundamental import analyze, fetch_fundamental_facts, load_capabilities
from .risk_gate import evaluate_risk

KST = ZoneInfo("Asia/Seoul")

FUND_TO_DEC = {"PASS": "GO", "HOLD": "CAUTION", "REJECT": "BLOCK", "NO_DATA": "CAUTION"}
REQUIRED_META = ("generated_kst", "session_date", "market_state", "regime",
                 "account_usd", "risk_pct", "fmp_capabilities", "llm_calls")
REQUIRED_CAND = ("ticker", "name", "source", "setup", "score",
                 "technical", "fundamental", "risk", "final")
REQUIRED_FUND = ("status", "floor", "verdict", "headline", "strengths",
                 "concerns", "one_off_risk", "confidence", "facts")
REQUIRED_RISK = ("decision", "checks")
REQUIRED_FINAL = ("decision", "reasons")
REQUIRED_PORT = ("positions", "open_risk_pct", "open_risk_is_lower_bound", "rows")

_CIO_SYS = (
    "너는 스윙 책상의 CIO다. 아래 보고서 JSON의 판정(GO/CAUTION/BLOCK)을 바꾸지 마라. "
    "새 숫자를 만들지 마라. 달러 기호 대신 USD 를 써라. "
    "한국어 총평 3줄 이내만 JSON으로 출력한다: {\"note\":\"...\"}"
)


def _now_kst() -> datetime:
    return datetime.now(KST)


def _staff(role: str, status: str, seconds: float, llm_calls: int = 0, error: str | None = None) -> dict:
    return {"role": role, "status": status, "seconds": round(float(seconds), 3),
            "llm_calls": int(llm_calls), "error": error}


def _tech_from_card(card: dict, equity: float, risk_pct: float) -> dict | None:
    plan = card.get("plan") if isinstance(card.get("plan"), dict) else None
    if not plan:
        return None
    qty = position_size(equity, risk_pct, plan.get("entry_hi"), plan.get("stop_pct"))
    warns = list(card.get("warnings") or [])
    if plan.get("lod_cap_exceeded"):
        warns.append("손절이 -3.5% 한도로 조여짐")
    try:
        entry_kst = ep.recommended_entry_kst().strftime("%H:%M")
    except Exception:
        entry_kst = None
    return {
        "entry_lo": plan.get("entry_lo"),
        "entry_hi": plan.get("entry_hi"),
        "stop": plan.get("stop"),
        "stop_pct": plan.get("stop_pct"),
        "tp1": plan.get("t1"),
        "t1_pct": plan.get("t1_pct"),
        "rr": plan.get("rr"),
        "rr_net": plan.get("rr_net"),
        "qty": qty,
        "warnings": warns,
        "lod_cap_exceeded": bool(plan.get("lod_cap_exceeded")),
        "entry_kst": entry_kst,
        "gap": card.get("gap"),
        "volx": card.get("volx"),
        "projected": card.get("projected"),
        "catalyst": card.get("catalyst") if isinstance(card.get("catalyst"), dict) else {},
        "score": card.get("score"),
        "setup": card.get("setup"),
        "price": card.get("price"),
    }


def _tech_decision(tech: dict | None, source: str) -> tuple[str, list[str]]:
    if source == "manual" or not tech:
        return "CAUTION", ["기술 셋업 미평가"]
    warns = list(tech.get("warnings") or [])
    if warns or tech.get("lod_cap_exceeded"):
        return "CAUTION", ["기술 경고"]
    return "GO", []


def _max_dec(*vals: str) -> str:
    best = "GO"
    for v in vals:
        if DEC_SEV.get(v, 0) > DEC_SEV.get(best, 0):
            best = v
    return best


def _headline(cands: list) -> dict:
    if not cands:
        return {"decision": "BLOCK", "text": "오늘은 휴식"}
    n_go = sum(1 for c in cands if (c.get("final") or {}).get("decision") == "GO")
    n_caut = sum(1 for c in cands if (c.get("final") or {}).get("decision") == "CAUTION")
    if n_go:
        return {"decision": "GO", "text": f"진입 후보 {n_go}개"}
    if n_caut:
        return {"decision": "CAUTION", "text": f"주의 {n_caut}개 · 진입 확정 없음"}
    return {"decision": "BLOCK", "text": "오늘은 휴식"}


def _parse_manual(raw: str, limit: int) -> list[str]:
    out, seen = [], set()
    for part in str(raw or "").replace(";", ",").split(","):
        t = part.strip().upper()
        if not t or "." in t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def validate_report(report: dict) -> list[str]:
    missing = []
    if not isinstance(report, dict):
        return ["report"]
    for k in ("meta", "headline", "candidates", "portfolio", "cio_note", "staff_log"):
        if k not in report:
            missing.append(k)
    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    for k in REQUIRED_META:
        if k not in meta:
            missing.append(f"meta.{k}")
    hl = report.get("headline") if isinstance(report.get("headline"), dict) else {}
    for k in ("decision", "text"):
        if k not in hl:
            missing.append(f"headline.{k}")
    port = report.get("portfolio") if isinstance(report.get("portfolio"), dict) else {}
    for k in REQUIRED_PORT:
        if k not in port:
            missing.append(f"portfolio.{k}")
    if not isinstance(report.get("candidates"), list):
        missing.append("candidates")
        return missing
    for i, c in enumerate(report["candidates"]):
        if not isinstance(c, dict):
            missing.append(f"candidates[{i}]")
            continue
        for k in REQUIRED_CAND:
            if k not in c:
                missing.append(f"candidates[{i}].{k}")
        fund = c.get("fundamental") if isinstance(c.get("fundamental"), dict) else {}
        for k in REQUIRED_FUND:
            if k not in fund:
                missing.append(f"candidates[{i}].fundamental.{k}")
        risk = c.get("risk") if isinstance(c.get("risk"), dict) else {}
        for k in REQUIRED_RISK:
            if k not in risk:
                missing.append(f"candidates[{i}].risk.{k}")
        fin = c.get("final") if isinstance(c.get("final"), dict) else {}
        for k in REQUIRED_FINAL:
            if k not in fin:
                missing.append(f"candidates[{i}].final.{k}")
    if not isinstance(report.get("staff_log"), list):
        missing.append("staff_log")
    return missing


def run_agency(
    pool: dict | None,
    manual_raw: str,
    positions: list,
    marks: dict,
    account_usd: float,
    risk_pct: float,
    regime: str,
    market_state: str,
    session_date: str,
    api_key: str,
    today_new_count: int = 0,
    cfg: AgencyCfg | None = None,
    progress=None,
) -> dict:
    cfg = cfg or AgencyCfg()
    log: list[dict] = []
    llm_calls = 0
    fmp_calls = 0
    cap = load_capabilities(cfg.capabilities_path)

    def note(step: str) -> None:
        if callable(progress):
            try:
                progress(step)
            except Exception:
                pass

    t0 = time.time()
    note("① 후보 읽기")
    pool = pool if isinstance(pool, dict) else {}
    pool_session = str(pool.get("session") or "")
    pool_note = ""
    scan_cards = []
    if not pool.get("top"):
        pool_note = "오늘 스캔 기록이 없습니다. 사냥 데스크에서 재검색하십시오."
    elif pool_session != str(session_date):
        pool_note = f"스캔 세션 {pool_session or '-'} ≠ 오늘 {session_date}. 후보 없이 진행."
    else:
        top = [r for r in (pool.get("top") or []) if isinstance(r, dict) and r.get("symbol")]
        scan_cards = top[: cfg.max_scan]
    manual = _parse_manual(manual_raw, cfg.max_manual)
    scan_syms = {str(c["symbol"]).upper() for c in scan_cards}
    manual = [t for t in manual if t not in scan_syms]

    candidates: list[dict] = []
    for card in scan_cards:
        tech = _tech_from_card(card, account_usd, risk_pct)
        candidates.append({
            "ticker": str(card["symbol"]).upper(),
            "name": str(card.get("name") or ""),
            "source": "scan",
            "setup": card.get("setup"),
            "score": card.get("score"),
            "technical": tech,
            "card": card,
        })
    for t in manual:
        candidates.append({
            "ticker": t, "name": "", "source": "manual",
            "setup": None, "score": None, "technical": None, "card": None,
        })
    log.append(_staff("주식 분석가", "OK" if scan_cards else "NO_DATA", time.time() - t0, 0,
                      pool_note or None))

    t1 = time.time()
    note("② 재무")
    fund_err = None

    def _one_fund(c: dict) -> tuple[str, dict, int, int]:
        cat = ((c.get("technical") or {}).get("catalyst") if c.get("technical") else None)
        if not cat and isinstance(c.get("card"), dict):
            cat = c["card"].get("catalyst")
        facts, n = fetch_fundamental_facts(c["ticker"], api_key, cfg, catalyst=cat, capabilities=cap)
        analyzed = analyze(c["ticker"], facts, cfg)
        return c["ticker"], analyzed, n, int(analyzed.get("llm_calls") or 0)

    fund_map: dict[str, dict] = {}
    if candidates:
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = [ex.submit(_one_fund, c) for c in candidates]
                for fut in as_completed(futs):
                    ticker, analyzed, n_fmp, n_llm = fut.result()
                    fund_map[ticker] = analyzed
                    fmp_calls += n_fmp
                    llm_calls += n_llm
        except Exception as e:
            fund_err = f"{type(e).__name__}: {e}"
            for c in candidates:
                fund_map[c["ticker"]] = {
                    "status": "NO_DATA", "floor": "PASS", "verdict": "NO_DATA",
                    "headline": "재무 수집 실패", "strengths": [], "concerns": [],
                    "one_off_risk": "자료 없음", "confidence": "LOW",
                    "facts": {}, "llm_calls": 0, "llm_status": "FAILED",
                }
    fund_llm = llm_calls
    for c in candidates:
        analyzed = fund_map.get(c["ticker"]) or {
            "status": "NO_DATA", "floor": "PASS", "verdict": "NO_DATA",
            "headline": "", "strengths": [], "concerns": [],
            "one_off_risk": "자료 없음", "confidence": "LOW", "facts": {},
        }
        c["fundamental"] = {k: analyzed.get(k) for k in
                            ("status", "floor", "verdict", "headline", "strengths",
                             "concerns", "one_off_risk", "confidence", "facts")}
        if not c.get("name"):
            c["name"] = str((analyzed.get("facts") or {}).get("name") or "")
    fund_status = "OK"
    if fund_err:
        fund_status = "ERROR"
    elif not candidates:
        fund_status = "SKIP"
    elif all((c["fundamental"].get("status") == "NO_DATA") for c in candidates):
        fund_status = "NO_DATA"
    log.append(_staff("재무·기업 분석가", fund_status, time.time() - t1, fund_llm, fund_err))

    t2 = time.time()
    note("③ 기술")
    for c in candidates:
        if c["source"] == "scan" and c.get("technical"):
            continue
        # 수동은 기술 미평가. 숫자는 다시 계산하지 않는다.
    log.append(_staff("기술적 분석가", "OK" if any(c.get("technical") for c in candidates) else "NO_DATA",
                      time.time() - t2, 0, None if scan_cards else "사냥 카드 없음"))

    t3 = time.time()
    note("④ 리스크")
    sectors = {}
    for c in candidates:
        sec = ((c.get("fundamental") or {}).get("facts") or {}).get("sector")
        if sec:
            sectors[c["ticker"]] = sec
    slim = [{"ticker": c["ticker"], "technical": c.get("technical")} for c in candidates]
    gated = evaluate_risk(slim, positions or [], marks or {}, account_usd, risk_pct,
                          regime, today_new_count, cfg, sectors)
    by = gated.get("by_ticker") or {}
    log.append(_staff("리스크 매니저", "OK", time.time() - t3, 0, None))

    t4 = time.time()
    note("⑤ 조립")
    for c in candidates:
        fund_v = (c.get("fundamental") or {}).get("verdict") or "NO_DATA"
        fund_dec = FUND_TO_DEC.get(fund_v, "CAUTION")
        tech_dec, tech_reasons = _tech_decision(c.get("technical"), c["source"])
        risk = by.get(c["ticker"]) or {"decision": "CAUTION", "checks": []}
        risk_dec = risk.get("decision") or "CAUTION"
        final_dec = _max_dec(fund_dec, tech_dec, risk_dec)
        if c["source"] == "manual" and final_dec == "GO":
            final_dec = "CAUTION"
        reasons = []
        if fund_dec != "GO":
            reasons.append(f"재무:{fund_v}")
        reasons.extend(tech_reasons)
        for chk in risk.get("checks") or []:
            if not chk.get("ok"):
                reasons.append(str(chk.get("name") or "risk"))
        # 수동 캡은 사유에 항상
        if c["source"] == "manual" and "기술 셋업 미평가" not in reasons:
            reasons.append("기술 셋업 미평가")
        c["risk"] = {"decision": risk_dec, "checks": list(risk.get("checks") or [])}
        c["final"] = {"decision": final_dec, "reasons": reasons}
        c.pop("card", None)

    headline = _headline(candidates)
    cio_note = None
    cio_err = None
    cio_llm = 0
    try:
        payload = {
            "headline": headline,
            "candidates": [
                {"ticker": c["ticker"], "final": c["final"],
                 "fundamental": c["fundamental"].get("verdict"),
                 "risk": c["risk"].get("decision"),
                 "setup": c.get("setup")}
                for c in candidates
            ],
            "regime": regime,
        }
        parsed, _p, errors = chat_json(
            _CIO_SYS, json.dumps(payload, ensure_ascii=False, default=str), max_tokens=250)
        cio_llm = 1
        llm_calls += 1
        if isinstance(parsed, dict):
            cio_note = sanitize_text(str(parsed.get("note") or parsed.get("cio_note") or ""))[:600]
        else:
            cio_err = "; ".join(errors) if errors else "CIO JSON 실패"
    except Exception as e:
        cio_err = f"{type(e).__name__}"
        cio_llm = 0
    log.append(_staff("기록·총괄", "OK" if cio_note else "LLM_FAILED", time.time() - t4, cio_llm, cio_err))

    report = {
        "meta": {
            "generated_kst": _now_kst().strftime("%Y-%m-%d %H:%M:%S"),
            "session_date": str(session_date),
            "market_state": str(market_state or ""),
            "regime": str(regime or "대기"),
            "account_usd": float(account_usd or 0),
            "risk_pct": float(risk_pct or 0),
            "fmp_capabilities": {k: {"ok": bool((v or {}).get("ok")),
                                     "period_used": (v or {}).get("period_used")}
                                 for k, v in (cap or {}).items()},
            "llm_calls": llm_calls,
            "fmp_calls": fmp_calls,
            "pool_note": pool_note,
        },
        "headline": headline,
        "candidates": [{k: c.get(k) for k in
                        ("ticker", "name", "source", "setup", "score",
                         "technical", "fundamental", "risk", "final")}
                       for c in candidates],
        "portfolio": gated.get("portfolio") or {
            "positions": 0, "open_risk_pct": 0.0, "open_risk_is_lower_bound": False, "rows": [],
        },
        "cio_note": cio_note,
        "staff_log": log,
    }
    return report
