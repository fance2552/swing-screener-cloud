import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from screener import episodic_pivot as ep  # noqa: E402
from screener import fmp_client as fc  # noqa: E402
from screener import ledger as lg  # noqa: E402

SESS = dt.date(2026, 10, 16)


def _cat(table):
    """table: {path: 반환값}. 없는 경로는 None(조회 실패)."""
    def fake(path, key, params=None, retries=2):
        return table.get(path)
    old = fc.fmp_get
    fc.fmp_get = fake
    try:
        fails = {}
        cat, failed = ep.catalyst_check("k", "X", SESS, ep.Cfg(), fails)
        return cat, failed, fails
    finally:
        fc.fmp_get = old


PR = {"date": "2026-10-16 07:00:00", "title": "Acme Reports Third Quarter 2026 Results"}


def test_earnings_blocked_falls_back_to_press_release():
    cat, failed, fails = _cat({"news/press-releases": [PR], "news/stock": [], "grades": []})
    assert cat["type"] == "EARNINGS_PR" and not failed and fails == {"earnings": 1}
    assert ep.score_a({"volx": 8, "gap": 20, "hold": 1, "prior_run": 0}, cat) < \
        ep.score_a({"volx": 8, "gap": 20, "hold": 1, "prior_run": 0}, {"type": "EARNINGS"})


def test_earnings_miss_is_not_rescued_by_press_or_news():
    miss = [{"date": "2026-10-15", "epsActual": 1.0, "epsEstimated": 1.0, "revenueActual": 1, "revenueEstimated": 2}]
    cat, failed, _ = _cat({"earnings": miss, "news/press-releases": [PR],
                           "news/stock": [{"publishedDate": PR["date"], "title": PR["title"]}], "grades": []})
    assert cat is None and not failed


def test_press_release_fact_and_upgrade_catalysts():
    pr = {"date": "2026-10-15 17:30:00", "title": "Acme Wins $2B Navy Contract"}
    cat, _, _ = _cat({"earnings": [], "news/press-releases": [pr], "news/stock": [], "grades": []})
    assert cat["type"] == "NEWS" and cat["source"] == "PRESS"
    up = [{"date": "2026-10-16", "action": "upgrade", "gradingCompany": "Firm", "previousGrade": "Hold",
           "newGrade": "Buy"}, {"date": "2026-10-15", "action": "upgrade"}]
    cat, _, _ = _cat({"earnings": [], "news/press-releases": [], "news/stock": [], "grades": up})
    assert cat["type"] == "UPGRADE" and "Hold" in cat["text"]
    cat, _, _ = _cat({"earnings": [], "news/press-releases": [], "news/stock": [], "grades": up[1:]})
    assert cat is None


def test_all_sources_failing_is_a_data_failure():
    cat, failed, fails = _cat({})
    assert cat is None and failed and len(fails) == 4
    assert "촉매 조회 실패" in ep._DATA_FAIL


def test_float_turnover_and_sector_context():
    assert ep.float_turnover_pct(5_000_000, 20_000_000) == 25.0
    assert ep.float_turnover_pct(5, None) is None
    prof = {"sector": "Technology", "industry": "Semiconductors"}
    movers = {"AAA": "Semiconductors", "BBB": "Semiconductors", "CCC": "Biotechnology"}
    ctx = ep.sector_context("AAA", prof, movers, {"XLK": {"price": 99.0, "open": 100.0}}, "LIVE")
    assert ctx["etf"] == "XLK" and ctx["peers"] == ["BBB"] and round(ctx["etf_vs_open_pct"], 1) == -1.0
    assert ep.sector_context("AAA", prof, movers, {}, "CLOSED")["etf_vs_open_pct"] is None
    assert ep.sector_context("AAA", None, movers, {}, "LIVE") is None


def test_batch_quote_and_probe():
    def fake(path, key, params=None, retries=2):
        if path == "batch-quote":
            return [{"symbol": s, "price": 10, "open": 9} for s in params["symbols"].split(",")]
        return [] if path == "grades" else None
    old = fc.fmp_get
    fc.fmp_get = fake
    try:
        got = fc.fetch_quotes("k", ["aaa", "BBB"], chunk=1)
        assert set(got) == {"AAA", "BBB"} and got["AAA"]["open"] == 9
        rows = {r["path"]: r for r in fc.probe_endpoints("k", "AAPL")}
        assert rows["batch-quote"]["ok"] and not rows["earnings"]["ok"] and not rows["grades"]["ok"]
    finally:
        fc.fmp_get = old


def test_ledger_records_new_signals():
    pool = {"state": "LIVE", "session": "2026-10-16", "scanned_at_kst": "x", "minutes": 40,
            "top": [{"symbol": "AAA", "setup": "A", "price": 10, "float_turn": 31.5,
                     "sector": {"etf_vs_open_pct": 0.4, "peers": ["BBB"]}, "catalyst": {"type": "UPGRADE"}}]}
    rows = []
    assert lg.record(rows, pool) == 1
    assert rows[0]["float_turn"] == 31.5 and rows[0]["peers"] == 1 and rows[0]["catalyst"] == "UPGRADE"


def test_scan_all_uses_batch_quotes_and_enriches_cards():
    import tempfile
    from tests.test_v4 import make_bars
    bars = {"AAA": make_bars(ep_k=0)}
    b = bars["AAA"]
    q = {"symbol": "AAA", "name": "Alpha", "price": float(b.close.iloc[-1]), "open": float(b.open.iloc[-1]),
         "dayHigh": 1, "dayLow": 1, "volume": 4_000_000, "marketCap": 5e9,
         "previousClose": float(b.close.iloc[-2])}
    calls = []

    def fake(path, key, params=None, retries=2):
        calls.append(path)
        if path in ("biggest-gainers", "most-actives"):
            return [{"symbol": "AAA", "price": 60, "changesPercentage": 12}]
        if path == "batch-quote":
            return [q] if "AAA" in params["symbols"] else []
        if path == "earnings":
            return None  # 플랜 미포함
        if path == "news/press-releases":
            return [{"date": "2026-10-16 07:00:00", "title": "Alpha Reports Third Quarter Results"}]
        if path == "profile":
            return [{"sector": "Technology", "industry": "Software"}]
        if path == "shares-float":
            return [{"floatShares": 20_000_000}]
        return None
    old = (fc.fmp_get, fc.fetch_quote, fc.fetch_eod)
    fc.fmp_get, fc.fetch_eod = fake, (lambda key, s, days=140: bars.get(s))
    fc.fetch_quote = lambda key, s: (_ for _ in ()).throw(AssertionError("개별 시세 호출"))
    fc._INFO_CACHE.clear()
    try:
        d = tempfile.mkdtemp()
        pool = ep.scan_all("k", now=dt.datetime(2026, 10, 17, 10, 0, tzinfo=ep.ET),
                           pool_path=os.path.join(d, "p.json"), history_path=os.path.join(d, "h.json"))
    finally:
        fc.fmp_get, fc.fetch_quote, fc.fetch_eod = old
    c = pool["top"][0]
    assert c["catalyst"]["type"] == "EARNINGS_PR" and pool["catalyst_fails"] == {"earnings": 1}
    assert c["float_turn"] == 20.0 and c["sector"]["etf"] == "XLK"
    assert "quote" not in calls
