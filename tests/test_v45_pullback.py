import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from screener import ledger as lg  # noqa: E402
from screener import portfolio as pf  # noqa: E402
from screener import pullback as pb  # noqa: E402


def _bars(closes, start="2025-06-02", vol=1_000_000):
    dates = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, float)
    return pd.DataFrame({"date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": vol})


def _uptrend_then_dip(n=230):
    c = list(np.linspace(50, 100, n - 2)) + [96.0, 92.0]  # 꾸준한 상승 후 이틀 급락
    return c


def test_signal_on_uptrend_dip():
    b = _bars(_uptrend_then_dip())
    c, why = pb.evaluate("X", b)
    assert why == "OK" and c["rsi2"] < 10 and c["r60"] > 0
    assert c["signal_date"] == b["date"].iloc[-1].date().isoformat()


def test_rejects_downtrend_and_illiquid():
    down = _bars(list(np.linspace(100, 50, 228)) + [48.0, 46.0])
    assert pb.evaluate("X", down)[0] is None
    thin = _bars(_uptrend_then_dip(), vol=1_000)
    assert pb.evaluate("X", thin)[1] == "거래대금 부족"
    no_dip = _bars(list(np.linspace(50, 100, 230)))
    assert pb.evaluate("X", no_dip)[1] == "과매도 아님(RSI2)"


def test_pick_top_momentum_and_no_repeat():
    cands = [{"symbol": "A", "r60": 10, "signal_date": "2026-10-15"},
             {"symbol": "B", "r60": 50, "signal_date": "2026-10-15"},
             {"symbol": "C", "r60": 30, "signal_date": "2026-10-15"}]
    assert [c["symbol"] for c in pb.pick(cands)] == ["B"]
    assert [c["symbol"] for c in pb.pick(cands, recent={"B": "2026-10-13"})] == ["C"]
    assert [c["symbol"] for c in pb.pick(cands, recent={"B": "2026-10-01"})] == ["B"]


def test_grade_exit_on_close_above_sma5_and_time_exit():
    closes = [100, 100, 100, 100, 100, 95, 94, 101]  # 신호일 = 94 (idx 6), 다음날 시가 = 101 → 5일선 위 종가
    b = _bars(closes)
    b.loc[7, "open"] = 95.0
    o = pb.grade(b, b["date"].iloc[6].date().isoformat())
    assert o["hold"] == 1 and o["entry"] == 95.0 and o["win"]
    assert abs(o["ret_close_pct"] - ((101 / 95 - 1) * 100 - 0.2)) < 0.01
    falling = _bars([100] * 5 + [95, 90, 89, 88, 87, 86, 85])
    sig = falling["date"].iloc[5].date().isoformat()
    o2 = pb.grade(falling, sig)
    assert o2["hold"] == 5 and not o2["win"]
    assert pb.grade(falling.iloc[:9], sig) is None  # 5거래일 전이면 채점 대기


def test_signal_session_waits_for_eod():
    mon_eve = dt.datetime(2026, 10, 19, 19, 0, tzinfo=pb.ET)
    mon_noon = dt.datetime(2026, 10, 19, 12, 0, tzinfo=pb.ET)
    sun = dt.datetime(2026, 10, 18, 12, 0, tzinfo=pb.ET)
    assert pb.signal_session(mon_eve) == dt.date(2026, 10, 19)
    assert pb.signal_session(mon_noon) == dt.date(2026, 10, 16)
    assert pb.signal_session(sun) == dt.date(2026, 10, 16)
    assert pb.entry_session(dt.date(2026, 10, 16)) == dt.date(2026, 10, 19)


def test_scan_uses_completed_bars_only(tmp_path):
    b = _bars(_uptrend_then_dip(), start="2025-09-01")
    sig = b["date"].iloc[-1].date()
    extra = pd.DataFrame([{"date": pd.Timestamp(sig + dt.timedelta(days=3)), "open": 1, "high": 1, "low": 1,
                           "close": 1, "volume": 1}])
    data = {"X": pd.concat([b, extra], ignore_index=True)}
    now = dt.datetime.combine(sig, dt.time(19, 0), tzinfo=pb.ET)
    pool = pb.scan("k", symbols=["X", "Y"], fetch=data.get, now=now, pool_path=str(tmp_path / "p.json"))
    assert pool["session"] == sig.isoformat() and [c["symbol"] for c in pool["top"]] == ["X"]
    assert pool["data_fail"] == 1 and pb.pool_is_current(pb.load_pool(str(tmp_path / "p.json")), now)


def test_ledger_records_and_grades_pullback():
    rows = []
    pool = {"session": "2026-10-15", "entry_session": "2026-10-16", "scanned_at_kst": "x",
            "top": [{"symbol": "X", "close": 94.0, "r60": 40.0, "rsi2": 3.0}]}
    assert lg.record_pullback(rows, pool) == 1 and lg.record_pullback(rows, pool) == 0
    assert lg.recent_pullbacks(rows) == {"X": "2026-10-15"}
    b = _bars([100, 100, 100, 100, 100, 95, 94, 101], start="2026-10-07")
    assert b["date"].iloc[6].date().isoformat() == "2026-10-15"
    b.loc[7, "open"] = 95.0
    now = dt.datetime(2026, 10, 19, 12, 0, tzinfo=lg.ET)
    assert lg.grade(rows, "k", now=now, fetch=lambda s: b) == 1
    st = lg.stats(rows)
    assert st["PB"]["n"] == 1 and st["PB"]["win_rate"] == 100 and st["ALL"]["n"] == 0


def test_portfolio_pullback_rules():
    p = pf.new_position("X", 10, 100.0, strategy=pf.PB)
    assert p["strategy"] == pf.PB and pf.normalize({"ticker": "Y", "qty": 1, "avg": 1})["strategy"] == "EP"
    d0 = dt.date.fromisoformat(p["opened"])

    def on(n):  # 진입일 포함 n번째 거래일
        d, k = d0, 1
        while k < n:
            d += dt.timedelta(days=1)
            if d.weekday() < 5:
                k += 1
        return d
    hold = pf.evaluate(p, 90.0, None, on(2), sma5=95.0)
    assert hold["signal"] == "HOLD" and hold["stop"] is None  # -10% 여도 손절 없음
    assert pf.evaluate(p, 96.0, None, on(2), sma5=95.0)["signal"] == "EXIT"
    assert pf.evaluate(p, 90.0, None, on(5), sma5=95.0)["signal"] == "TIME_EXIT"
    ep_pos = pf.new_position("Y", 10, 100.0)
    assert pf.evaluate(ep_pos, 96.0, None, on(2))["signal"] == "EXIT"  # EP 는 기존 손절 규칙 그대로


def test_v461_skip_held_and_list_all_passed():
    """v4.6.1: 보유 종목은 1등에서 빼고, 합격 종목은 전부 표시와 함께 보여 준다 (2026-10-25 MPC 재추천)."""
    cands = [{"symbol": "MPC", "r60": 46, "signal_date": "2026-10-23"},
             {"symbol": "AUGO", "r60": 40, "signal_date": "2026-10-23"},
             {"symbol": "C", "r60": 30, "signal_date": "2026-10-23"},
             {"symbol": "D", "r60": 20, "signal_date": "2026-10-23"}]
    top = pb.pick(cands, held={"MPC"}, recent={"AUGO": "2026-10-22"})
    assert [c["symbol"] for c in top] == ["C"]
    pool = {"top": top, "others": cands}
    rows = pb.passed_list(pool, held={"MPC"}, recent={"AUGO": "2026-10-22"})
    assert [(r["rank"], r["symbol"], r["tag"]) for r in rows] == [
        (1, "MPC", "🛡️ 보유 중"), (2, "AUGO", "최근 추천"), (3, "C", "⭐ 추천"), (4, "D", "")]
