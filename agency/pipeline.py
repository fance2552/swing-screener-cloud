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

NO_SCAN_TEXT = "스캔 기록 없음 — 사냥 데스크에서 먼저 재검색"
NO_CAND_LIVE_TEXT = "현재 통과 후보 없음 · 잠시 후 재검색"
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


def session_key(value) -> str:
    """풀·호출부 모두 NYSE session_date. 달력 date.today()를 쓰지 않는다."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())[:10]
    s = str(value).strip()
    return s[:10] if s else ""


def scan_matches_session(pool, session_date) -> bool:
    """기록 유무는 pool['session'] == session_date 로만 판정. top 빈 배열은 스캔 성공이다."""
    if not isinstance(pool, dict) or "session" not in pool:
        return False
    a = session_key(pool.get("session"))
    b = session_key(session_date)
    return bool(a) and a == b


def _staff(role: str, status: str, seconds: float, llm_calls: int = 0, error: str | None = None,
           reason: str | None = None, fmp_calls: int = 0, fmp_fails: int = 0, last_http=None) -> dict:
    return {
        "role": role, "status": status, "seconds": round(float(seconds), 3),
        "llm_calls": int(llm_calls), "error": error, "reason": reason or "",
        "fmp_calls": int(fmp_calls or 0), "fmp_fails": int(fmp_fails or 0),
        "last_http": last_http,
    }


def _fetch_stats(extra) -> tuple[int, int, object]:
    if isinstance(extra, dict):
        return int(extra.get("calls") or 0), int(extra.get("fails") or 0), extra.get("last_http")
    return int(extra or 0), 0, None


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


def _headline(cands: list, scan_ok: bool, regime: str = "", market_state: str = "",
              session_date: str = "") -> dict:
    """판정(decision)은 그대로 두고, 문구만 상황에 맞게 고른다. BLOCK 은 '진입 금지'가 아니라 '대기'다."""
    if not cands and not scan_ok:
        return {"decision": "NO_SCAN", "text": NO_SCAN_TEXT}
    if not cands:
        state = str(market_state or "").upper()
        if state == "LIVE":
            return {"decision": "BLOCK", "text": NO_CAND_LIVE_TEXT}
        base = f"{session_date} 기준 통과 후보 없음" if session_date else "통과 후보 없음"
        return {"decision": "BLOCK", "text": f"{base} · 본장 개장 후 재검색"}
    n_go = sum(1 for c in cands if (c.get("final") or {}).get("decision") == "GO")
    n_caut = sum(1 for c in cands if (c.get("final") or {}).get("decision") == "CAUTION")
    if n_go:
        return {"decision": "GO", "text": f"진입 후보 {n_go}개"}
    if n_caut:
        return {"decision": "CAUTION", "text": f"주의 {n_caut}개 · 진입 확정 없음"}
    if str(regime or "").upper() == "CORRECTION":
        return {"decision": "BLOCK", "text": f"조정장 주의 · 후보 {len(cands)}개 모두 리스크 기준 보류"}
    return {"decision": "BLOCK", "text": f"후보 {len(cands)}개 모두 리스크 기준 보류 (보유·한도 확인)"}


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
    scan_ok = scan_matches_session(pool, session_date)
    pool_session = session_key(pool.get("session")) if "session" in pool else ""
    want_session = session_key(session_date)
    pool_note = ""
    scan_cards = []
    if scan_ok:
        top = [r for r in (pool.get("top") or []) if isinstance(r, dict) and r.get("symbol")]
        scan_cards = top[: cfg.max_scan]
        if not scan_cards:
            pool_note = f"세션 {want_session} 스캔 있음, 통과 후보 0"
    elif not pool or "session" not in pool:
        pool_note = NO_SCAN_TEXT
    else:
        pool_note = (
            f"{NO_SCAN_TEXT} (pool.session={pool_session or '-'} ≠ session_date={want_session})"
        )
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
    if scan_cards:
        eq_status, eq_reason = "OK", f"세션 {want_session} 스캔 후보 {len(scan_cards)}개"
    elif scan_ok:
        eq_status, eq_reason = "SKIP", pool_note
    else:
        eq_status, eq_reason = "NO_DATA", pool_note
    log.append(_staff("주식 분석가", eq_status, time.time() - t0, reason=eq_reason))

    t1 = time.time()
    note("② 재무")
    fund_err = None

    def _one_fund(c: dict) -> tuple[str, dict, int, int, int, object]:
        cat = ((c.get("technical") or {}).get("catalyst") if c.get("technical") else None)
        if not cat and isinstance(c.get("card"), dict):
            cat = c["card"].get("catalyst")
        facts, extra = fetch_fundamental_facts(c["ticker"], api_key, cfg, catalyst=cat, capabilities=cap)
        n_fmp, n_fail, http = _fetch_stats(extra)
        analyzed = analyze(c["ticker"], facts, cfg)
        return c["ticker"], analyzed, n_fmp, n_fail, http, int(analyzed.get("llm_calls") or 0)

    fund_map: dict[str, dict] = {}
    fmp_fails = 0
    last_http = None
    if candidates:
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = [ex.submit(_one_fund, c) for c in candidates]
                for fut in as_completed(futs):
                    ticker, analyzed, n_fmp, n_fail, http, n_llm = fut.result()
                    fund_map[ticker] = analyzed
                    fmp_calls += n_fmp
                    fmp_fails += n_fail
                    if http is not None:
                        last_http = http
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
    fund_reason = f"FMP {fmp_calls}회 · 실패 {fmp_fails}회"
    if fund_err:
        fund_status, fund_reason = "ERROR", fund_err
    elif not candidates:
        fund_status, fund_reason = "SKIP", "후보 0명 — 재무 수집 생략"
    elif all((c["fundamental"].get("status") == "NO_DATA") for c in candidates):
        fund_status, fund_reason = "NO_DATA", "facts 없음 — FMP 실패 또는 권한 없음"
    log.append(_staff("재무·기업 분석가", fund_status, time.time() - t1, fund_llm, fund_err,
                      reason=fund_reason, fmp_calls=fmp_calls, fmp_fails=fmp_fails, last_http=last_http))

    t2 = time.time()
    note("③ 기술")
    for c in candidates:
        if c["source"] == "scan" and c.get("technical"):
            continue
        # 수동은 기술 미평가. 숫자는 다시 계산하지 않는다.
    if any(c.get("technical") for c in candidates):
        tech_status, tech_reason = "OK", f"사냥 카드 {sum(1 for c in candidates if c.get('technical'))}개 재사용"
    elif not candidates:
        tech_status, tech_reason = "SKIP", "후보 0명 — 기술 평가 생략"
    else:
        tech_status, tech_reason = "NO_DATA", "사냥 카드 없음 — 기술 셋업 미평가"
    log.append(_staff("기술적 분석가", tech_status, time.time() - t2, reason=tech_reason))

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
    if candidates:
        risk_status, risk_reason = "OK", f"게이트 {len(candidates)}명"
    else:
        risk_status, risk_reason = "SKIP", "후보 0명 — 신규 게이트 생략"
    log.append(_staff("리스크 매니저", risk_status, time.time() - t3, reason=risk_reason))

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

    headline = _headline(candidates, scan_ok, regime, market_state, want_session)
    cio_note = None
    cio_err = None
    cio_llm = 0
    cio_status, cio_reason = "OK", ""
    if not candidates:
        cio_status, cio_reason = "SKIP", "후보 0명 — 총평 생략"
    else:
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
                cio_reason = "총평 1회"
            else:
                cio_err = "; ".join(errors) if errors else "CIO JSON 실패"
                cio_status, cio_reason = "LLM_FAILED", cio_err
        except Exception as e:
            cio_err = f"{type(e).__name__}"
            cio_llm = 0
            cio_status, cio_reason = "LLM_FAILED", cio_err
        if cio_status == "OK" and not cio_note:
            cio_status, cio_reason = "LLM_FAILED", cio_err or "총평 없음"
    log.append(_staff("기록·총괄", cio_status, time.time() - t4, cio_llm, cio_err, reason=cio_reason))

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
