from __future__ import annotations

from pathlib import Path

import pytest

from agency.config import AgencyCfg
from agency.fundamental import analyze, apply_red_flags, facts_all_none, floor_from_flags, merge_verdict
from agency.pipeline import run_agency, validate_report
from agency.risk_gate import evaluate_risk

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path: Path) -> AgencyCfg:
    return AgencyCfg(cache_dir=tmp_path, capabilities_path=tmp_path / "cap.json")


def _facts(**over):
    base = {
        "data_period": "quarter",
        "data_asof": "2026-06-27",
        "revenue_yoy_pct": 12.0,
        "revenue_trend": [90, 95, 100, 110],
        "gross_margin_pct": 44.0,
        "operating_margin_pct": 18.0,
        "op_margin_change_pp": 1.0,
        "net_income_positive_count": 4,
        "op_cashflow": 1e9,
        "fcf": 8e8,
        "debt_to_equity": 0.4,
        "current_ratio": 1.4,
        "interest_coverage": 12.0,
        "cash": 2e9,
        "total_debt": 1e9,
        "valuation": {"pe": 28.0},
        "earnings_surprise_pct": 8.0,
        "sector": "Technology",
        "red_flags": [],
    }
    base.update(over)
    return base


def _card(sym="AAA", warnings=None, lod=False):
    return {
        "symbol": sym, "name": f"{sym} Inc", "setup": "A", "score": 80,
        "price": 100.0, "gap": 9.0, "volx": 4.0, "projected": False,
        "catalyst": {"type": "EARNINGS", "eps_beat_pct": 8.0, "text": "실적 +8%"},
        "warnings": list(warnings or []),
        "plan": {
            "entry_lo": 100.0, "entry_hi": 100.5, "stop": 96.98, "stop_pct": 3.5,
            "t1": 107.0, "t1_pct": 6.5, "rr": 1.86, "rr_net": 1.7,
            "lod_cap_exceeded": lod,
        },
    }


def _hold(ticker, qty=10, avg=100, stop_pct=3.5, tp1_done=False):
    return {"ticker": ticker, "qty": qty, "avg": avg, "stop_pct": stop_pct,
            "tp1_done": tp1_done, "opened": "2026-10-16"}


def test_merge_floor_and_llm_stricter(tmp_path, monkeypatch):
    assert merge_verdict("REJECT", "PASS") == "REJECT"
    assert merge_verdict("PASS", "REJECT") == "REJECT"
    assert merge_verdict("HOLD", "PASS") == "HOLD"
    assert merge_verdict("PASS", "HOLD") == "HOLD"

    facts = _facts(revenue_yoy_pct=-5.0, operating_margin_pct=-2.0, fcf=-1.0)
    flags = apply_red_flags(facts)
    assert "revenue_decline" in flags and "operating_loss" in flags and "negative_fcf" in flags
    assert floor_from_flags(flags) == "REJECT"

    monkeypatch.setattr("ai_advisor.chat_json", lambda *a, **k: (
        {"verdict": "PASS", "headline": "ok", "strengths": ["a"], "concerns": [],
         "one_off_risk": "자료 없음", "confidence": "HIGH"}, "DEEPSEEK", []))
    out = analyze("ZZZ", facts, _cfg(tmp_path))
    assert out["floor"] == "REJECT"
    assert out["verdict"] == "REJECT"

    clean = _facts()
    monkeypatch.setattr("ai_advisor.chat_json", lambda *a, **k: (
        {"verdict": "REJECT", "headline": "no", "strengths": [], "concerns": ["x"],
         "one_off_risk": "자료 없음", "confidence": "LOW"}, "DEEPSEEK", []))
    out2 = analyze("YYY", clean, _cfg(tmp_path))
    assert out2["floor"] == "PASS"
    assert out2["verdict"] == "REJECT"


def test_all_facts_none_is_no_data_not_go(tmp_path, monkeypatch):
    empty = {k: None for k in (
        "revenue_yoy_pct", "revenue_trend", "gross_margin_pct", "operating_margin_pct",
        "op_margin_change_pp", "net_income_positive_count", "op_cashflow", "fcf",
        "debt_to_equity", "current_ratio", "interest_coverage", "cash", "total_debt",
        "valuation", "earnings_surprise_pct")}
    empty.update(data_period=None, data_asof=None, red_flags=[])
    assert facts_all_none(empty)
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return None, None, ["x"]

    monkeypatch.setattr("ai_advisor.chat_json", boom)
    out = analyze("NONE", empty, _cfg(tmp_path))
    assert out["verdict"] == "NO_DATA"
    assert out["status"] == "NO_DATA"
    assert called["n"] == 0

    monkeypatch.setattr("agency.pipeline.fetch_fundamental_facts", lambda *a, **k: (empty, 0))
    monkeypatch.setattr("agency.pipeline.chat_json", boom)
    pool = {"session": "2026-10-17", "top": [_card()]}
    report = run_agency(pool, "", [], {}, 10000, 0.75, "NORMAL", "CLOSED", "2026-10-17",
                        "", cfg=_cfg(tmp_path))
    assert report["candidates"][0]["fundamental"]["verdict"] == "NO_DATA"
    assert report["candidates"][0]["final"]["decision"] != "GO"
    assert report["headline"]["decision"] != "GO"


def test_risk_gate_limits():
    cfg = AgencyCfg()
    holds = [_hold("AA"), _hold("BB"), _hold("CC")]
    marks = {t: {"price": 100.0, "sma10": 98.0} for t in ("AA", "BB", "CC", "DD")}
    cands = [{"ticker": "DD", "technical": {
        "entry_hi": 100.5, "entry_lo": 100, "stop_pct": 3.5, "qty": 2, "lod_cap_exceeded": False}}]
    out = evaluate_risk(cands, holds, marks, 10_000, 0.75, "NORMAL", 0, cfg)
    assert out["by_ticker"]["DD"]["decision"] == "BLOCK"
    names = {c["name"]: c for c in out["by_ticker"]["DD"]["checks"]}
    assert names["max_positions"]["ok"] is False

    dup = evaluate_risk([{"ticker": "AA", "technical": cands[0]["technical"]}],
                        holds[:1], marks, 10_000, 0.75, "NORMAL", 0, cfg)
    assert dup["by_ticker"]["AA"]["decision"] == "BLOCK"
    assert any(c["name"] == "already_held" and not c["ok"] for c in dup["by_ticker"]["AA"]["checks"])

    corr = evaluate_risk(cands, [], marks, 10_000, 0.75, "CORRECTION", 0, cfg)
    assert corr["by_ticker"]["DD"]["decision"] == "BLOCK"
    assert any(c["name"] == "correction_block" and not c["ok"] for c in corr["by_ticker"]["DD"]["checks"])

    fat = [_hold("AA", qty=100, avg=100)]
    fat_out = evaluate_risk(cands, fat, {"AA": {"price": 100.0}, "DD": {"price": 100.0}},
                            10_000, 0.75, "NORMAL", 0, cfg)
    assert fat_out["portfolio"]["open_risk_pct"] > 3.0
    assert any(c["name"] == "open_risk" and not c["ok"] for c in fat_out["by_ticker"]["DD"]["checks"])

    be = evaluate_risk(cands, [_hold("AA", qty=100, avg=100, tp1_done=True)],
                       {"AA": {"price": 110.0}, "DD": {"price": 100.0}},
                       10_000, 0.75, "NORMAL", 0, cfg)
    assert be["portfolio"]["open_risk_pct"] == pytest.approx(0.0, abs=1e-6)

    miss = evaluate_risk(cands, [_hold("AA")], {"DD": {"price": 100.0}}, 10_000, 0.75, "NORMAL", 0, cfg)
    assert miss["portfolio"]["open_risk_is_lower_bound"] is True


def test_headline_and_max_severity(tmp_path, monkeypatch):
    monkeypatch.setattr("agency.pipeline.fetch_fundamental_facts", lambda *a, **k: (_facts(), 0))
    monkeypatch.setattr("ai_advisor.chat_json", lambda *a, **k: (
        {"verdict": "PASS", "headline": "h", "strengths": [], "concerns": [],
         "one_off_risk": "자료 없음", "confidence": "MED", "note": "총평"}, "DEEPSEEK", []))
    monkeypatch.setattr("agency.pipeline.chat_json", lambda *a, **k: (
        {"note": "총평 한 줄"}, "DEEPSEEK", []))

    pool = {"session": "2026-10-17", "top": [_card("GO1"), _card("GO2")]}
    report = run_agency(pool, "", [], {}, 10_000, 0.75, "NORMAL", "CLOSED", "2026-10-17",
                        "k", cfg=_cfg(tmp_path))
    assert report["headline"]["decision"] == "GO"
    assert "진입 후보" in report["headline"]["text"]
    assert not validate_report(report)

    blocked = run_agency(pool, "", [_hold("GO1"), _hold("GO2")], 
                         {"GO1": {"price": 100}, "GO2": {"price": 100}},
                         10_000, 0.75, "CORRECTION", "CLOSED", "2026-10-17",
                         "k", cfg=_cfg(tmp_path))
    assert blocked["headline"]["decision"] == "BLOCK"
    assert blocked["headline"]["text"] == "오늘은 휴식"
    assert all(c["final"]["decision"] == "BLOCK" for c in blocked["candidates"])

    empty = run_agency({"session": "2026-10-17", "top": []}, "", [], {}, 10_000, 0.75,
                       "NORMAL", "CLOSED", "2026-10-17", "k", cfg=_cfg(tmp_path))
    assert empty["headline"] == {"decision": "BLOCK", "text": "오늘은 휴식"}


def test_llm_total_failure_still_builds_report(tmp_path, monkeypatch):
    monkeypatch.setattr("agency.pipeline.fetch_fundamental_facts", lambda *a, **k: (_facts(), 0))
    monkeypatch.setattr("ai_advisor.chat_json", lambda *a, **k: (None, None, ["DOWN"]))
    monkeypatch.setattr("agency.pipeline.chat_json", lambda *a, **k: (None, None, ["DOWN"]))
    pool = {"session": "2026-10-17", "top": [_card()]}
    report = run_agency(pool, "", [], {}, 10_000, 0.75, "NORMAL", "CLOSED", "2026-10-17",
                        "k", cfg=_cfg(tmp_path))
    assert not validate_report(report)
    fund = report["candidates"][0]["fundamental"]
    assert fund["status"] == "LLM_FAILED"
    assert fund["verdict"] == "PASS"
    assert report["cio_note"] is None
    assert any(s["status"] == "LLM_FAILED" for s in report["staff_log"] if s["role"] == "기록·총괄")


def test_report_schema_keys(tmp_path, monkeypatch):
    monkeypatch.setattr("agency.pipeline.fetch_fundamental_facts", lambda *a, **k: (_facts(), 0))
    monkeypatch.setattr("ai_advisor.chat_json", lambda *a, **k: (
        {"verdict": "HOLD", "headline": "h", "strengths": ["s"], "concerns": ["c"],
         "one_off_risk": "자료 없음", "confidence": "MED"}, "GROQ", []))
    monkeypatch.setattr("agency.pipeline.chat_json", lambda *a, **k: ({"note": "n"}, "GROQ", []))
    report = run_agency(
        {"session": "2026-10-17", "top": [_card()]}, "IBM, MSFT, NVDA, EXTRA", [], {},
        10_000, 0.75, "NORMAL", "LIVE", "2026-10-17", "k", cfg=_cfg(tmp_path))
    miss = validate_report(report)
    assert miss == []
    src = {c["ticker"]: c["source"] for c in report["candidates"]}
    assert src["AAA"] == "scan"
    assert src["IBM"] == "manual" and src["MSFT"] == "manual" and src["NVDA"] == "manual"
    assert "EXTRA" not in src
    man = next(c for c in report["candidates"] if c["ticker"] == "IBM")
    assert man["final"]["decision"] != "GO"
    assert "기술 셋업 미평가" in man["final"]["reasons"]


def test_app_has_no_secretary_tab():
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "tab_secretary" not in text
    assert "전략 비서실" not in text
    assert "def tab_agency" in text
    assert "🏢 에이전시" in text


def test_scan_pool_uses_session_date_not_calendar(tmp_path, monkeypatch):
    from agency.pipeline import NO_SCAN_TEXT, scan_matches_session

    assert scan_matches_session({"session": "2026-10-16", "top": []}, "2026-10-16") is True
    assert scan_matches_session({"session": "2026-10-16", "top": [_card()]}, "2026-10-17") is False
    assert scan_matches_session({}, "2026-10-16") is False
    assert scan_matches_session(None, "2026-10-16") is False

    monkeypatch.setattr("agency.pipeline.fetch_fundamental_facts", lambda *a, **k: (_facts(), 0))
    monkeypatch.setattr("ai_advisor.chat_json", lambda *a, **k: (
        {"verdict": "PASS", "headline": "h", "strengths": [], "concerns": [],
         "one_off_risk": "자료 없음", "confidence": "MED"}, "DEEPSEEK", []))
    monkeypatch.setattr("agency.pipeline.chat_json", lambda *a, **k: ({"note": "n"}, "DEEPSEEK", []))
    cfg = _cfg(tmp_path)
    # 달력이 2026-10-18이어도 session_date=2026-10-16 풀은 현재 세션이다.
    report = run_agency(
        {"session": "2026-10-16", "top": [_card("GS")]}, "", [], {},
        10_000, 0.75, "NORMAL", "CLOSED", "2026-10-16", "k", cfg=cfg)
    assert report["headline"]["decision"] == "GO"
    assert report["candidates"][0]["ticker"] == "GS"

    stale = run_agency(
        {"session": "2026-10-15", "top": [_card("OLD")]}, "", [], {},
        10_000, 0.75, "NORMAL", "CLOSED", "2026-10-16", "k", cfg=cfg)
    assert stale["headline"]["decision"] == "NO_SCAN"
    assert stale["headline"]["text"] == NO_SCAN_TEXT
    assert stale["candidates"] == []
    assert stale["headline"] != {"decision": "BLOCK", "text": "오늘은 휴식"}

    missing = run_agency({}, "", [], {}, 10_000, 0.75, "NORMAL", "CLOSED", "2026-10-16", "k", cfg=cfg)
    assert missing["headline"]["decision"] == "NO_SCAN"
    assert missing["headline"]["text"] == NO_SCAN_TEXT

    scanned_empty = run_agency(
        {"session": "2026-10-16", "top": []}, "", [], {},
        10_000, 0.75, "NORMAL", "CLOSED", "2026-10-16", "k", cfg=cfg)
    assert scanned_empty["headline"] == {"decision": "BLOCK", "text": "오늘은 휴식"}


def test_skip_cio_llm_when_no_candidates(tmp_path, monkeypatch):
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        return {"note": "should not run"}, "DEEPSEEK", []

    monkeypatch.setattr("agency.pipeline.fetch_fundamental_facts", lambda *a, **k: (_facts(), 0))
    monkeypatch.setattr("ai_advisor.chat_json", boom)
    monkeypatch.setattr("agency.pipeline.chat_json", boom)
    report = run_agency(
        {"session": "2026-10-16", "top": []}, "", [], {},
        10_000, 0.75, "NORMAL", "CLOSED", "2026-10-16", "k", cfg=_cfg(tmp_path))
    assert report["candidates"] == []
    assert calls["n"] == 0
    assert report["cio_note"] is None
    assert report["meta"]["llm_calls"] == 0
    cio = next(s for s in report["staff_log"] if s["role"] == "기록·총괄")
    assert cio["status"] == "SKIP"
    assert "총평 생략" in cio["reason"]


def test_staff_log_fmp_columns_and_skip_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agency.pipeline.fetch_fundamental_facts",
        lambda *a, **k: (_facts(), {"calls": 5, "fails": 2, "last_http": 402}),
    )
    monkeypatch.setattr("ai_advisor.chat_json", lambda *a, **k: (
        {"verdict": "PASS", "headline": "h", "strengths": [], "concerns": [],
         "one_off_risk": "자료 없음", "confidence": "MED"}, "DEEPSEEK", []))
    monkeypatch.setattr("agency.pipeline.chat_json", lambda *a, **k: ({"note": "n"}, "DEEPSEEK", []))

    with_scan = run_agency(
        {"session": "2026-10-16", "top": [_card()]}, "", [], {},
        10_000, 0.75, "NORMAL", "CLOSED", "2026-10-16", "k", cfg=_cfg(tmp_path))
    fund = next(s for s in with_scan["staff_log"] if s["role"] == "재무·기업 분석가")
    assert fund["fmp_calls"] == 5
    assert fund["fmp_fails"] == 2
    assert fund["last_http"] == 402
    for row in with_scan["staff_log"]:
        assert "fmp_calls" in row and "fmp_fails" in row and "last_http" in row and "reason" in row

    no_scan = run_agency({}, "", [], {}, 10_000, 0.75, "NORMAL", "CLOSED", "2026-10-16",
                         "k", cfg=_cfg(tmp_path))
    by_role = {s["role"]: s for s in no_scan["staff_log"]}
    assert by_role["주식 분석가"]["status"] == "NO_DATA"
    assert by_role["주식 분석가"]["reason"]
    assert "스캔 기록 없음" in by_role["주식 분석가"]["reason"]
    assert by_role["재무·기업 분석가"]["status"] == "SKIP"
    assert "후보 0명" in by_role["재무·기업 분석가"]["reason"]
    assert by_role["기술적 분석가"]["status"] == "SKIP"
    assert by_role["기술적 분석가"]["reason"]
    assert by_role["리스크 매니저"]["status"] == "SKIP"
    assert by_role["리스크 매니저"]["reason"]
    assert by_role["기록·총괄"]["status"] == "SKIP"
    assert by_role["기록·총괄"]["reason"]
