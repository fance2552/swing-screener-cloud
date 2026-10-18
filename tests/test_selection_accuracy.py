"""추천 티커 선별 정확도 회귀 테스트 (2026-10-18 검토 P0-2·P0-4·P1-1·P1-4)."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from screener import episodic_pivot as ep  # noqa: E402
from screener import fmp_client as fc  # noqa: E402
from agency.risk_gate import evaluate_risk  # noqa: E402
from test_v4 import make_bars  # noqa: E402

SESS = dt.date(2026, 10, 16)


def _pullback():
    bars = make_bars(ep_k=4, dry=0.3)
    sma = bars.close.tail(10).mean()
    bars.loc[bars.index[-1], "low"] = sma * 0.995
    bars.loc[bars.index[-1], "close"] = sma * 1.002
    return bars, float(sma * 1.002)


def test_b_rejects_live_price_below_structure():
    bars, px = _pullback()
    ok, why = ep.evaluate_b("X", {"price": px, "market_cap": 3e9}, bars, {"type": "EARNINGS"}, ep.Cfg(), True)
    assert ok, why
    broken, why = ep.evaluate_b("X", {"price": px * 0.93, "market_cap": 3e9}, bars,
                                {"type": "EARNINGS"}, ep.Cfg(), True)
    assert broken is None, "구조 저점·지지선을 깬 종목이 통과했다"


def test_b_live_requires_quote():
    bars, _ = _pullback()
    r, why = ep.evaluate_b("X", None, bars, {"type": "EARNINGS"}, ep.Cfg(), True)
    assert r is None and why == "실시간 시세 없음"


def test_make_plan_ignores_structure_above_price():
    p = ep.make_plan(50.0, 55.0, None, ep.Cfg())
    assert p["stop"] < 50.0 and p["struct_stop_dist_pct"] > 0


def test_missing_prev_bar_not_glued():
    bars = make_bars(ep_k=0, end=dt.date(2026, 10, 14))  # 10-15 일봉 결측
    q = {"open": 60, "high": 61, "low": 59, "price": 60, "volume": 5e6}
    out = ep.attach_live_bar(bars, q, SESS, "LIVE")
    assert out["date"].iloc[-1].date() == dt.date(2026, 10, 14)
    ok = ep.attach_live_bar(make_bars(ep_k=0, end=dt.date(2026, 10, 15)), q, SESS, "LIVE")
    assert ok["date"].iloc[-1].date() == SESS


def test_prev_close_mismatch_rejected():
    bars = make_bars(ep_k=0)
    prev = float(bars.close.iloc[-2])
    q = {"market_cap": 5e9, "prev_close": prev}
    assert ep.evaluate_a("X", q, bars, ep.Cfg(), SESS, "CLOSED", None)[0]
    q["prev_close"] = prev * 0.9
    r, why = ep.evaluate_a("X", q, bars, ep.Cfg(), SESS, "CLOSED", None)
    assert r is None and "전일 종가" in why


def _with_news(items, earnings=None):
    def fake(path, key, params=None, retries=2):
        if path == "earnings":
            return earnings or []
        if path == "news/stock":
            return items
        return None
    old = fc.fmp_get
    fc.fmp_get = fake
    try:
        return ep.catalyst("k", "X", SESS, ep.Cfg())
    finally:
        fc.fmp_get = old


def test_news_catalyst_timing_and_reaction_filter():
    # 개장 후 반응 기사, 다음 날 기사, 전일 장중 기사는 촉매가 아니다
    assert _with_news([{"publishedDate": "2026-10-16 10:05:00", "title": "Acme wins DoD contract"}]) is None
    assert _with_news([{"publishedDate": "2026-10-17 07:00:00", "title": "Acme wins DoD contract"}]) is None
    assert _with_news([{"publishedDate": "2026-10-15 11:00:00", "title": "Acme wins DoD contract"}]) is None
    assert _with_news([{"publishedDate": "2026-10-16 08:00:00",
                        "title": "Why Acme shares are soaring today"}]) is None
    hit = _with_news([{"publishedDate": "2026-10-16 07:30:00", "title": "Acme wins $2B DoD contract"}])
    assert hit and hit["type"] == "NEWS"
    assert _with_news([{"publishedDate": "2026-10-15 17:10:00", "title": "Acme raises FY guidance"}])


def test_earnings_must_be_today_or_prev_session():
    beat = {"epsActual": 1.2, "epsEstimated": 1.0, "revenueActual": 110, "revenueEstimated": 100}
    assert _with_news([], [{**beat, "date": "2026-10-15"}])["type"] == "EARNINGS"
    assert _with_news([], [{**beat, "date": "2026-10-16"}])["type"] == "EARNINGS"
    assert _with_news([], [{**beat, "date": "2026-10-13"}]) is None


def test_unknown_regime_is_not_go():
    cand = [{"ticker": "AAA", "technical": {"qty": 1, "entry_hi": 10, "stop_pct": 3}}]
    for reg, want in (("NORMAL", "GO"), ("대기", "CAUTION"), ("PRESSURE", "CAUTION"), ("CORRECTION", "BLOCK")):
        out = evaluate_risk(cand, [], {}, 10000, 0.5, reg)
        assert out["by_ticker"]["AAA"]["decision"] == want, (reg, out)
