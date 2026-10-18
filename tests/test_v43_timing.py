"""v4.3 실시간 진입 타이밍 (시가 범위 고점 돌파) 테스트."""
import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from screener import intraday as itd  # noqa: E402

ET = itd.ET
DAY = dt.date(2026, 10, 16)


def _bars(spec):
    """spec: [(HH:MM, open, high, low, close, volume)] ET."""
    rows = [[dt.datetime.combine(DAY, dt.time.fromisoformat(t), ET), o, h, l, c, v] for t, o, h, l, c, v in spec]
    return itd._frame(rows)


OR = [("09:30", 50.0, 51.0, 49.5, 50.8, 900), ("09:35", 50.8, 51.5, 50.4, 51.2, 700),
      ("09:40", 51.2, 51.4, 50.6, 50.9, 500)]


def at(hhmm):
    return dt.datetime.combine(DAY, dt.time.fromisoformat(hhmm), ET)


def test_parsers_keep_only_that_day_and_regular_session():
    fmp = [{"date": "2026-10-16 09:35:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
           {"date": "2026-10-16 09:30:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
           {"date": "2026-10-15 15:55:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
           {"date": "2026-10-16 08:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]
    df = itd.regular_session(itd.parse_fmp(fmp, DAY))
    assert [t.strftime("%H:%M") for t in df["t"]] == ["09:30", "09:35"]
    alp = {"bars": [{"t": "2026-10-16T13:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3}]}
    df = itd.parse_alpaca(alp, DAY)
    assert df["t"].iloc[0].strftime("%H:%M") == "09:30"           # 서머타임 UTC-4
    epoch = int(dt.datetime(2026, 10, 16, 13, 35, tzinfo=dt.timezone.utc).timestamp())
    yh = {"chart": {"result": [{"timestamp": [epoch], "indicators": {"quote": [
        {"open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [4]}]}}]}}
    df = itd.parse_yahoo(yh, DAY)
    assert df["t"].iloc[0].strftime("%H:%M") == "09:35"
    assert itd.parse_yahoo({}, DAY).empty and itd.parse_alpaca(None, DAY).empty


def test_provider_fallback_order():
    calls = []

    def boom():
        calls.append("FMP")
        raise RuntimeError("402")

    def empty():
        calls.append("ALPACA")
        return itd._frame([])

    def good():
        calls.append("YAHOO")
        return _bars(OR)

    df, src, errs = itd.fetch_5min("x", DAY, providers=(("FMP", boom), ("ALPACA", empty), ("YAHOO", good)))
    assert src == "YAHOO" and len(df) == 3 and calls == ["FMP", "ALPACA", "YAHOO"]
    assert errs == ["FMP: RuntimeError", "ALPACA: 분봉 없음"]
    df, src, errs = itd.fetch_5min("x", DAY, providers=(("FMP", empty),))
    assert src == "" and df.empty


def test_opening_range():
    orh, orl, closed = itd.opening_range(_bars(OR), DAY)
    assert (orh, orl, closed) == (51.5, 49.5, True)
    assert itd.opening_range(_bars(OR[:2]), DAY)[2] is False


def test_states():
    df = _bars(OR)
    assert itd.entry_signal(df, 51.0, DAY, at("09:40"))["state"] == "WAIT_OR"
    wait = itd.entry_signal(df, 51.2, DAY, at("09:50"))
    assert wait["state"] == "WAIT" and wait["orh"] == 51.5
    assert abs(wait["entry_lo"] - 51.5 * 1.001) < 1e-9
    go = _bars(OR + [("09:45", 51.4, 51.8, 51.3, 51.7, 900)])
    assert itd.entry_signal(go, 51.7, DAY, at("09:50"))["state"] == "GO"
    weak = _bars(OR + [("09:45", 51.4, 51.8, 51.3, 51.7, 100)])
    assert itd.entry_signal(weak, 51.7, DAY, at("09:50"))["state"] == "WEAK"
    assert itd.entry_signal(go, 52.5, DAY, at("09:50"))["state"] == "CHASE"
    assert itd.entry_signal(go, 51.3, DAY, at("09:55"))["state"] == "FAIL"
    assert itd.entry_signal(go, 49.0, DAY, at("09:55"))["state"] == "BREAK"
    assert itd.entry_signal(itd._frame([]), 50.0, DAY, at("10:00"))["state"] == "NO_DATA"
