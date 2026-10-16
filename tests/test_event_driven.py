import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import event_driven as ed  # noqa: E402
from screener import portfolio as pf  # noqa: E402

TODAY = date(2026, 9, 28)  # Monday
_ET = ZoneInfo("America/New_York")


def test_earn_dday_points():
    assert [ed._earn_dday_points(d) for d in (12, 7, 13, 14, 6, 5, 4, 3, 2, 8, 15)] == [0, 40, 0, 0, 40, 40, 40, 0, 0, 0, 0]


def test_earn_signals():
    friday = date(2026, 10, 16)
    assert ed.earn_fire_signal(7, 80) == (ed.EARN_FIRE, 1)
    assert ed.earn_fire_signal(4, 80) == (ed.EARN_FIRE, 1)
    assert ed.earn_fire_signal(4, 70) == (ed.EARN_LOW, 2)
    assert ed.earn_fire_signal(3, 90) == (ed.EARN_BAN, 3)
    assert ed.earn_fire_signal(8, 90) == (ed.EARN_CLOSED, 2)
    assert ed.earn_fire_signal(2, 90) == (ed.EARN_BAN, 3)
    assert ed.earn_fire_signal(4, 90, "2026-10-19", friday) == (ed.EARN_BAN, 3)


def test_runup_window_excludes_d3_and_one_session():
    friday = date(2026, 10, 16)
    assert ed.trading_days_until("2026-10-19", friday) == 1
    assert ed.runup_entry_allowed(3, "2026-10-19", friday) is False
    assert ed.runup_entry_allowed(4, "2026-10-19", friday) is False
    assert ed.runup_entry_allowed(5, "2026-10-21", friday) is True
    rows = [
        {"ticker": "SMMT", "price": 20, "earnings_date": "2026-10-19", "buy_ratio": 80, "sma50": 10, "target_consensus": 30},
        {"ticker": "NEW", "price": 20, "earnings_date": "2026-10-21", "buy_ratio": 80, "sma50": 10, "target_consensus": 30},
    ]
    ranked = ed.earn_rank_live(rows, today=friday)
    assert [row["ticker"] for row in ranked] == ["NEW"]


def test_must_exit_and_deadline():
    assert ed.must_exit_for_earnings("2026-10-08", date(2026, 10, 5)) is True
    assert ed.must_exit_for_earnings("2026-10-08", date(2026, 10, 2)) is False
    assert ed.exit_deadline("2026-10-06") == date(2026, 10, 2)
    assert ed.must_exit_for_earnings("", TODAY) is False


def test_business_days_held():
    assert ed.business_days_held("2026-09-22", date(2026, 9, 24)) == 3
    assert ed.business_days_held("2026-09-22", date(2026, 9, 28)) == 5


def test_friday_flat_window():
    fri_before = datetime(2026, 10, 2, 15, 0, tzinfo=_ET)
    fri_after = datetime(2026, 10, 2, 15, 45, tzinfo=_ET)
    thursday = datetime(2026, 10, 1, 20, 0, tzinfo=_ET)
    assert ed.is_friday_flat_window(fri_before) is False
    assert ed.is_friday_flat_window(fri_after) is True
    assert ed.is_friday_flat_window(thursday) is False


def _m(**kw):
    base = {
        "ticker": "NET", "strategy": "EARNINGS", "current_price": 100.0, "entry_price": 100.0,
        "pct": 0.0, "r_mult": 0.0, "hold_bdays": 2, "d_day": 8, "force_exit_d3": False, "has_quote": True,
    }
    base.update(kw)
    return base


def test_guardian_priority_order():
    assert pf.guardian(_m(strategy="OLD", pct=-5.0))["code"] == "LEGACY"
    assert pf.guardian(_m(strategy="EARNINGS"))["code"] == "LEGACY"
    assert pf.guardian(_m(strategy="PEAD", pct=-5.0))["code"] == "SL"
    assert pf.guardian(_m(strategy="PEAD", pct=1.0, hold_bdays=15))["code"] == "TIME_EXIT"
    assert pf.guardian(_m(strategy="PEAD", pct=1.0, hold_bdays=3))["code"] == "HOLD"
    assert pf.guardian(_m(strategy="PEAD", has_quote=False))["code"] == "NO_QUOTE"
    assert pf.guardian(_m(strategy="RUNUP", pct=-5.0, d_day=4))["code"] == "SL"
    assert pf.guardian(_m(strategy="RUNUP", pct=-5.0, d_day=6))["code"] == "SL"
    assert pf.guardian(_m(strategy="RUNUP", pct=1.0, d_day=2, force_exit_runup=False))["code"] == "D3_EXIT"
    assert pf.guardian(_m(strategy="RUNUP", pct=1.0, d_day=3, force_exit_runup=False))["code"] == "D3_EXIT"
    assert pf.guardian(_m(strategy="RUNUP", pct=1.0, d_day=6, force_exit_runup=True))["code"] == "HOLD"
    assert pf.guardian(_m(strategy="RUNUP", pct=1.0, d_day=4, force_exit_runup=False))["code"] == "HOLD"
    assert pf.guardian(_m(strategy="RUNUP", pct=1.0, d_day=7, has_quote=False))["code"] == "HOLD"
    assert pf.guardian(_m(strategy="RSI2", pct=-4.0))["code"] == "SL"
    assert pf.guardian(_m(strategy="RSI2", pct=1.0, sma5_recaptured=True, hold_bdays=1))["code"] == "SMA5_EXIT"
    assert pf.guardian(_m(strategy="RSI2", pct=1.0, sma5_recaptured=False, hold_bdays=4))["code"] == "TIME_EXIT"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
