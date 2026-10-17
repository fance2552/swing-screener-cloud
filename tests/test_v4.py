import datetime as dt
import os
import sys
import tempfile

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from screener import episodic_pivot as ep  # noqa: E402
from screener import fmp_client as fc  # noqa: E402
from screener import portfolio as pf  # noqa: E402
import ai_advisor as ai  # noqa: E402

ET = ep.ET


def make_bars(n=80, ep_k=None, gap=0.10, volmult=4.0, dry=0.35, end=dt.date(2026, 10, 16)):
    """n 개 거래일. ep_k: 마지막 바 기준 몇 세션 전에 EP (0=마지막 바)."""
    dates = pd.bdate_range(end=end, periods=n)
    price, rows = 50.0, []
    for i, d in enumerate(dates):
        o = price
        v = 1_000_000
        k = n - 1 - i
        if ep_k is not None and k == ep_k:
            o = price * (1 + gap)
            price = o * 1.02
            v = int(1_000_000 * volmult)
        elif ep_k is not None and k == ep_k - 1:
            price *= 1.005
            v = 1_500_000
        elif ep_k is not None and k < ep_k - 1:
            price *= 1.0005
            v = int(1_000_000 * dry)
        else:
            price *= 1.001
        h, l = max(o, price) * 1.004, min(o, price) * 0.996
        rows.append({"date": d, "open": o, "high": h, "low": l, "close": price, "volume": v})
    return pd.DataFrame(rows)


def test_a_pass_and_fail():
    sess = dt.date(2026, 10, 16)
    bars = make_bars(ep_k=0)
    q = {"market_cap": 5e9, "price": float(bars.close.iloc[-1]), "open": float(bars.open.iloc[-1])}
    r, why = ep.evaluate_a("X", q, bars, ep.Cfg(), sess, "CLOSED", None)
    assert r and why == "OK" and r["gap"] > 9.9 and r["volx"] > 3.9
    q["market_cap"] = 5e8
    assert ep.evaluate_a("X", q, bars, ep.Cfg(), sess, "CLOSED", None)[0] is None
    weak = make_bars(ep_k=0, gap=0.05)
    assert ep.evaluate_a("X", {"market_cap": 5e9}, weak, ep.Cfg(), sess, "CLOSED", None)[0] is None


def test_live_projection():
    sess = dt.date(2026, 10, 16)
    bars = make_bars(ep_k=0, volmult=0.5)  # 15분 시점 raw 0.5x → 추정 ~5x
    r, why = ep.evaluate_a("X", {"market_cap": 5e9}, bars, ep.Cfg(), sess, "LIVE", 15)
    assert r and r["projected"] and r["volx"] > 3
    assert ep.evaluate_a("X", {"market_cap": 5e9}, bars, ep.Cfg(), sess, "LIVE", 5)[0] is None


def test_plan_rr():
    p = ep.make_plan(100.0, 97.0, 95.0, ep.Cfg())
    assert 2.5 <= p["stop_pct"] <= 3.5 and p["rr_ok"] and p["t1_pct"] >= 5
    far = ep.make_plan(100.0, 90.0, 95.0, ep.Cfg())
    assert far["stop_pct"] == 3.5 and far["lod_cap_exceeded"] and far["t1_pct"] == 7.0


def test_b_pullback():
    cfg = ep.Cfg()
    bars = make_bars(ep_k=4, dry=0.3)
    # 눌림 구간 마지막 바가 10일선 근처가 되도록 보정
    sma = bars.close.tail(10).mean()
    bars.loc[bars.index[-1], "low"] = sma * 0.995
    bars.loc[bars.index[-1], "close"] = sma * 1.002
    r, why = ep.evaluate_b("X", {"price": sma * 1.002, "market_cap": 3e9}, bars, {"type": "EARNINGS"}, cfg)
    assert r, why
    assert r["days_since"] == 4
    assert ep.evaluate_b("X", None, bars, None, cfg)[0] is None
    noisy = bars.copy()
    noisy.loc[noisy.index[-3:], "volume"] = 2_000_000
    assert ep.evaluate_b("X", None, noisy, {"type": "NEWS"}, cfg)[0] is None


def test_session_and_entry_time():
    sat = dt.datetime(2026, 10, 17, 10, 0, tzinfo=ET)
    assert ep.session_date(sat) == dt.date(2026, 10, 16)
    mon_pre = dt.datetime(2026, 10, 19, 8, 0, tzinfo=ET)
    assert ep.session_date(mon_pre) == dt.date(2026, 10, 16)
    mon_live = dt.datetime(2026, 10, 19, 9, 45, tzinfo=ET)
    assert ep.session_date(mon_live) == dt.date(2026, 10, 19)
    assert ep.market_state(mon_live) == ("LIVE", 15)
    assert ep.recommended_entry_kst(mon_live).strftime("%H:%M") == "22:45"      # 서머타임
    nov = dt.datetime(2026, 11, 2, 9, 45, tzinfo=ET)
    assert ep.recommended_entry_kst(nov).strftime("%H:%M") == "23:45"           # 표준시


def test_holiday_blocks_fire_and_time_exit():
    from screener import event_driven as priced
    from screener import radar
    holiday = dt.datetime(2026, 11, 26, 10, 0, tzinfo=ET)
    assert radar.is_market_open(holiday) is False
    assert radar.session_allows_fire_alert(holiday) is False
    assert ep.market_state(holiday)[0] == "CLOSED"
    assert ep.session_date(holiday) < holiday.date()
    assert [row["symbol"] for row in priced.priced_top(
        [{"symbol": "NO", "price": None}, {"symbol": "OK", "price": 12.5}, {"symbol": "Z", "price": 0}],
        10,
    )] == ["OK"]
    stale = pf.new_position("GS", 10, 100.0, 3.5)
    stale["opened"] = "2026-10-01"
    assert pf.evaluate(stale, 110.0, 100.0, today=dt.date(2026, 10, 8))["signal"] == "TIME_EXIT"
    fresh = pf.new_position("GS", 10, 100.0, 3.5)
    fresh["opened"] = "2026-10-05"
    assert pf.evaluate(fresh, 101.0, 100.0, today=dt.date(2026, 10, 7))["signal"] == "HOLD"


def test_net_rr_hard_reject():
    ai.chat_json = lambda *a, **k: (
        {"verdict": "APPROVE", "summary": "사라", "reasons": [], "risks": [], "conditions": []},
        "DEEPSEEK",
        [],
    )
    plan = ep.make_plan(100.0, 97.0, 95.0, ep.Cfg())
    assert plan["rr_net"] < 2.0
    card = {
        "symbol": "AAA", "setup": "A", "setup_label": "EP", "price": 100.0, "gap": 10.0, "volx": 4.0,
        "projected": False, "catalyst": {"text": "earn", "type": "EARNINGS"}, "score": 80,
        "warnings": [], "extra": {}, "sma10": 95.0, "plan": plan,
    }
    out = ai.review_order(card, 1000, 0.75, "22:45")
    assert out["verdict"] == "REJECT"
    assert out["shares"] == 0
    assert out["text"].startswith("[수수료 반영 실질 손익비:")


def test_portfolio_rules():
    p = pf.new_position("GS", 10, 100.0, 3.5)
    assert pf.evaluate(p, 96.0, 99.0)["signal"] == "EXIT"
    r = pf.evaluate(p, 105.5, 100.0)
    assert r["signal"] == "TRIM" and r["net_pct"] < r["gross_pct"]
    p2 = pf.apply_tp1(p)
    assert p2["qty"] == 5 and p2["tp1_done"]
    assert pf.evaluate(p2, 100.1, 99.0)["signal"] == "EXIT"          # 본전(수수료 포함) 아래
    assert pf.evaluate(p2, 104.0, 105.0)["signal"] == "EXIT"         # 10일선 이탈
    assert pf.evaluate(p2, 108.0, 105.0)["signal"] == "HOLD"
    assert pf.evaluate(p, None)["signal"] == "NO_DATA"
    legacy = pf.normalize({"symbol": "aapl", "shares": "3", "avg_price": "10"})
    assert legacy["ticker"] == "AAPL" and legacy["qty"] == 3


def test_sanitize_and_audit_override(monkeypatch=None):
    assert "$" not in ai.sanitize_text({"a": ["$5.00 목표", "US $10"]})["a"][0]
    assert ai.sanitize_text("$10") == "USD 10"
    rows = [pf.evaluate(pf.new_position("GS", 10, 100.0), 96.0, 99.0),
            pf.evaluate(pf.new_position("AAA", 10, 100.0), 101.0, 99.0)]
    ai.chat_json = lambda *a, **k: ({"items": [{"ticker": "GS", "action": "HOLD", "reason": "x"},
                                                {"ticker": "AAA", "action": "TRIM", "reason": "과열"}],
                                      "overall": "ok"}, "DEEPSEEK", [])
    out = {i["ticker"]: i for i in ai.audit_portfolio(rows)["items"]}
    assert out["GS"]["action"] == "EXIT"      # AI 가 HOLD 라고 해도 규칙 EXIT 유지
    assert out["AAA"]["action"] == "TRIM"     # AI 는 상향 가능


def test_fmp_age_and_order_board(monkeypatch=None):
    assert fc.age_label(100, 0) == "대기"
    assert fc.age_label(110, 100) == "10초 전"
    captured = {}

    def fake(system, user, max_tokens=900):
        captured["user"] = user
        return {"verdict": "APPROVE", "summary": "ok", "reasons": [], "risks": [], "conditions": []}, "DEEPSEEK", []

    ai.chat_json = fake
    plan = {"entry_lo": 10, "entry_hi": 10.05, "stop": 9.7, "stop_pct": 3.0, "t1": 10.55, "t1_pct": 6,
            "rr": 2.0, "rr_net": 1.8, "lod_cap_exceeded": False, "rr_ok": True}
    focus = {"symbol": "AAA", "setup": "A", "setup_label": "EP", "price": 10, "gap": 12, "volx": 4,
             "projected": False, "catalyst": {"text": "earn", "type": "EARNINGS"}, "score": 80,
             "warnings": [], "extra": {}, "sma10": 9, "plan": plan}
    pull = {"symbol": "BBB", "setup": "B", "gap": 9, "volx": 4, "score": 70, "projected": False,
            "catalyst": {"type": "EARNINGS"}, "warnings": [], "extra": {"dry_mean": 0.3}}
    ai.review_order(focus, 1000, 0.75, "22:45", [focus], [pull])
    assert "EP_candidates" in captured["user"] and "Pullback_candidates" in captured["user"]
    assert "AAA" in captured["user"] and "BBB" in captured["user"]


def test_secretary_context_and_groq_fallback(monkeypatch):
    text = ai.build_system_context(
        regime="NORMAL",
        hunt={"top": [{"symbol": "AAA", "setup": "A", "score": 80, "price": 10, "gap": 9, "volx": 4, "extra": {}}]},
        holdings=[{"ticker": "GS", "qty": 1, "avg": 100, "stop_pct": 3.5, "opened": "2026-10-01", "tp1_done": False}],
        closed=["OLD"],
    )
    assert "사령관 보유 종목" in text and "GS" in text
    assert "오늘자 사냥터 후보(EP/눌림목)" in text and "AAA" in text
    assert "시장 국면" in text and "NORMAL" in text
    assert "오늘 이미 청산한 티커" in text and "OLD" in text
    assert ai._provider_order("GROQ")[0][0] == "GROQ"

    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["messages"] = (json or {}).get("messages") or []

        class Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "GS는 홀딩"}}]}

        return Resp()

    monkeypatch.setattr(ai, "secret", lambda name: "" if "DEEPSEEK" in name else "k")
    monkeypatch.setattr(ai.requests, "post", fake_post)
    reply, provider, errors = ai.ask_secretary([{"role": "user", "content": "GS 어때?"}], "DEEPSEEK", text)
    assert provider == "GROQ"
    assert "홀딩" in reply
    assert any("DEEPSEEK" in item for item in errors)
    assert seen["messages"][0]["role"] == "system"
    assert "CIO 딥시크" in seen["messages"][0]["content"]
    assert "사칭하지 마라" in seen["messages"][0]["content"]
    assert ai.secretary_badge("deepseek-chat") == "DeepSeek 총괄 CIO"
    assert ai.secretary_badge("llama-3.3-70b-versatile") == "Groq 실무 참모"


def test_provider_fallback():
    os.environ.pop("DEEPSEEK_API_KEY", None)
    os.environ["GROQ_API_KEY"] = "k"
    names = [n for n, *_ in ai.active_providers()]
    assert names == ["GROQ"]
    os.environ["DEEPSEEK_API_KEY"] = "k2"
    assert [n for n, *_ in ai.active_providers()] == ["DEEPSEEK", "GROQ"]


def test_scan_all_end_to_end():
    sess_end = dt.date(2026, 10, 16)
    a_bars, b_bars = make_bars(ep_k=0), make_bars(ep_k=4, dry=0.3)
    sma = b_bars.close.tail(10).mean()
    b_bars.loc[b_bars.index[-1], "low"] = sma * 0.995
    b_bars.loc[b_bars.index[-1], "close"] = sma * 1.002
    bars = {"AAA": a_bars, "BBB": b_bars}

    def fake_get(path, key, params=None, retries=2):
        if path in ("biggest-gainers", "most-actives"):
            return [{"symbol": "AAA", "price": 60, "changesPercentage": 12, "name": "Alpha"}]
        if path == "earnings":
            return [{"date": "2026-10-15", "epsActual": 1.2, "epsEstimated": 1.0,
                     "revenueActual": 110, "revenueEstimated": 100}]
        return None

    fc.fmp_get = fake_get
    fc.fetch_quote = lambda key, s: {"symbol": s, "name": s, "price": float(bars[s].close.iloc[-1]),
                                     "open": float(bars[s].open.iloc[-1]), "high": 1, "low": 1, "volume": 1,
                                     "market_cap": 5e9, "avg_volume": 1e6}
    fc.fetch_eod = lambda key, s, days=140: bars.get(s)
    d = tempfile.mkdtemp()
    now = dt.datetime(2026, 10, 17, 10, 0, tzinfo=ET)
    hist = os.path.join(d, "h.json")
    ep._dump({"events": [{"symbol": "BBB", "date": "2026-10-12", "gap": 10, "volx": 4,
                          "catalyst": {"type": "EARNINGS", "text": "t"}}]}, hist)
    pool = ep.scan_all("k", now=now, pool_path=os.path.join(d, "p.json"), history_path=hist)
    syms = [c["symbol"] for c in pool["top"]]
    assert "AAA" in syms and "BBB" in syms, pool
    assert ep.pool_is_current(ep.load_pool(os.path.join(d, "p.json")), now)
    empty = ep.scan_all("k", now=now, pool_path=os.path.join(d, "p2.json"), history_path=os.path.join(d, "h2.json"))
    assert [c["symbol"] for c in empty["top"]] == ["AAA"]
