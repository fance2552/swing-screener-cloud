"""v4.2 당일 상승 확률 중심 진입 판정 회귀 테스트."""
import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from screener import episodic_pivot as ep  # noqa: E402
from screener import fmp_client as fc  # noqa: E402
from screener import ledger as lg  # noqa: E402
from test_v4 import make_bars  # noqa: E402

SESS = dt.date(2026, 10, 16)
ET = ep.ET
Q = {"market_cap": 5e9}


def _set_last(bars, **kw):
    for k, v in kw.items():
        bars.loc[bars.index[-1], k] = v
    return bars


def test_gap_must_hold_above_open():
    bars = make_bars(ep_k=0)
    o = float(bars.open.iloc[-1])
    _set_last(bars, close=o * 0.99, low=o * 0.97, high=o * 1.03)
    r, why = ep.evaluate_a("X", Q, bars, ep.Cfg(), SESS, "CLOSED", None)
    assert r is None and "시가 이탈" in why


def test_gap_must_sit_in_upper_half_of_range():
    bars = make_bars(ep_k=0)
    o = float(bars.open.iloc[-1])
    _set_last(bars, close=o * 1.01, low=o * 0.995, high=o * 1.08)   # 위치 약 18%
    r, why = ep.evaluate_a("X", Q, bars, ep.Cfg(), SESS, "CLOSED", None)
    assert r is None and "범위 하단" in why
    _set_last(bars, close=o * 1.06)                                   # 위치 약 76%
    r, why = ep.evaluate_a("X", Q, bars, ep.Cfg(), SESS, "CLOSED", None)
    assert r and 0.7 < r["hold"] < 0.8, why


def test_live_raw_volume_floor_is_one_day():
    bars = make_bars(ep_k=0, volmult=0.9)
    r, why = ep.evaluate_a("X", Q, bars, ep.Cfg(), SESS, "LIVE", 60)
    assert r is None and "원" in why
    ok = make_bars(ep_k=0, volmult=1.6)
    assert ep.evaluate_a("X", Q, ok, ep.Cfg(), SESS, "LIVE", 60)[0]


def test_extended_stock_scores_lower_and_warns():
    cat = {"type": "EARNINGS"}
    calm = make_bars(ep_k=0)
    r_calm, _ = ep.evaluate_a("X", Q, calm, ep.Cfg(), SESS, "CLOSED", None)
    hot = make_bars(ep_k=0)
    # 직전 60세션 동안 2배 오른 종목으로 만든다
    ramp = pd.Series([1 + i / 60 for i in range(len(hot) - 1)] + [1.0])
    for c in ("open", "high", "low", "close"):
        hot.loc[: len(hot) - 2, c] = hot.loc[: len(hot) - 2, c] * ramp[: len(hot) - 1].values / ramp.iloc[-2]
    r_hot, why = ep.evaluate_a("X", Q, hot, ep.Cfg(), SESS, "CLOSED", None)
    assert r_calm and r_hot, why
    assert r_hot["prior_run"] > 60 and any("과열" in w for w in r_hot["warnings"])
    assert ep.score_a(r_hot, cat) < ep.score_a(r_calm, cat) - 9


def test_score_weights_volume_and_hold_over_catalyst():
    base = {"gap": 10.0, "volx": 4.0, "hold": 0.9, "prior_run": 5.0}
    earn, news = ep.score_a(base, {"type": "EARNINGS"}), ep.score_a(base, {"type": "NEWS"})
    assert earn - news == 7.0
    full = {"gap": 20.0, "volx": 8.0, "hold": 1.0, "prior_run": 0.0}
    assert ep.score_a(full, {"type": "EARNINGS"}) == 100.0
    # 최저 조건(갭 8%, 3배, 범위 중간)은 최소 점수에 못 미친다
    weak = {"gap": 8.0, "volx": 3.0, "hold": 0.5, "prior_run": 40.0}
    assert ep.score_a(weak, {"type": "EARNINGS"}) < ep.Cfg().min_score


def _fake_market(spy_px, qqq_px, trend_up):
    def quote(key, s):
        if s == "SPY":
            return {"price": spy_px, "open": 500.0}
        if s == "QQQ":
            return {"price": qqq_px, "open": 400.0}
        return None
    closes = [100 + i * (0.5 if trend_up else -0.5) for i in range(40)]
    spy = pd.DataFrame({"date": pd.bdate_range(end=SESS, periods=40), "close": closes})
    return quote, (lambda key, s, days=140: spy if s == "SPY" else None)


def test_market_day_limits_candidates():
    old_q, old_e = fc.fetch_quote, fc.fetch_eod
    try:
        fc.fetch_quote, fc.fetch_eod = _fake_market(505, 405, True)
        assert ep.market_day("k", "LIVE")["limit"] is None
        fc.fetch_quote, fc.fetch_eod = _fake_market(495, 405, True)
        assert ep.market_day("k", "LIVE")["limit"] == 1
        fc.fetch_quote, fc.fetch_eod = _fake_market(495, 395, False)
        assert ep.market_day("k", "LIVE")["limit"] == 0
        # 장 마감 후에는 시가 비교를 하지 않는다 (이평 하나만)
        assert ep.market_day("k", "CLOSED")["limit"] == 1
        fc.fetch_quote, fc.fetch_eod = (lambda key, s: None), (lambda key, s, days=140: None)
        md = ep.market_day("k", "LIVE")
        assert md["limit"] is None and md["unknown"]
    finally:
        fc.fetch_quote, fc.fetch_eod = old_q, old_e


def _pool(state="LIVE", top=None):
    return {"session": "2026-10-16", "state": state, "scanned_at_kst": "2026-10-16 23:30", "minutes": 60,
            "top": top if top is not None else [
                {"symbol": "AAA", "setup": "A", "price": 50.0, "score": 70, "gap": 10, "volx": 4,
                 "catalyst": {"type": "EARNINGS"}},
                {"symbol": "BBB", "setup": "B", "price": 20.0, "score": 60, "gap": 9, "volx": 3,
                 "catalyst": {"type": "NEWS"}}]}


def test_ledger_records_live_only_and_dedupes():
    rows = []
    assert lg.record(rows, _pool(state="CLOSED")) == 0
    assert lg.record(rows, _pool()) == 2
    later = _pool()
    later["top"][0]["price"] = 55.0
    assert lg.record(rows, later) == 0
    assert rows[0]["rec_price"] == 50.0   # 첫 추천가를 지킨다


def test_ledger_grades_after_session_close():
    rows = []
    lg.record(rows, _pool())
    bars = {
        "AAA": pd.DataFrame({"date": pd.to_datetime(["2026-10-16"]), "open": [49.0], "high": [53.0],
                             "low": [48.0], "close": [52.0], "volume": [1]}),
        "BBB": pd.DataFrame({"date": pd.to_datetime(["2026-10-16"]), "open": [20.0], "high": [20.4],
                             "low": [19.0], "close": [19.5], "volume": [1]}),
    }
    during = dt.datetime(2026, 10, 16, 15, 0, tzinfo=ET)
    assert lg.grade(rows, "k", during, fetch=bars.get) == 0
    after = dt.datetime(2026, 10, 16, 16, 30, tzinfo=ET)
    assert lg.grade(rows, "k", after, fetch=bars.get) == 2
    a, b = rows
    assert a["outcome"]["win"] and a["outcome"]["ret_close_pct"] == 4.0 and a["outcome"]["max_up_pct"] == 6.0
    assert not b["outcome"]["win"]
    s = lg.stats(rows)
    assert s["ALL"]["n"] == 2 and s["ALL"]["win_rate"] == 50.0 and s["A"]["win_rate"] == 100.0


def test_ledger_gives_up_on_missing_bars():
    rows = []
    lg.record(rows, _pool(top=[{"symbol": "ZZZ", "setup": "A", "price": 10.0}]))
    soon = dt.datetime(2026, 10, 19, 10, 0, tzinfo=ET)
    assert lg.grade(rows, "k", soon, fetch=lambda s: None) == 0 and rows[0]["outcome"] is None
    late = dt.datetime(2026, 10, 30, 10, 0, tzinfo=ET)
    assert lg.grade(rows, "k", late, fetch=lambda s: None) == 1 and rows[0]["outcome"]["missing"]
    assert lg.stats(rows)["ALL"]["n"] == 0


def test_ledger_save_load_roundtrip(tmp_path):
    path = str(tmp_path / "rec.json")
    rows = []
    lg.record(rows, _pool())
    lg.save(rows, path)
    assert lg.load(path) == rows
    assert lg.load(str(tmp_path / "none.json")) == []
