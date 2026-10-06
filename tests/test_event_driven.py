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
    assert [ed._earn_dday_points(d) for d in (12, 7, 13, 14, 6, 5, 4, 3, 15)] == [40, 40, 25, 25, 10, 10, 0, 0, 0]


def test_earn_signals():
    assert ed.earn_fire_signal(9, 80) == (ed.EARN_FIRE, 1)
    assert ed.earn_fire_signal(9, 70) == (ed.EARN_LOW, 2)
    assert ed.earn_fire_signal(13, 90) == (ed.EARN_WAIT, 2)
    assert ed.earn_fire_signal(4, 90) == (ed.EARN_BAN, 3)


def test_sqz_score_and_signal():
    parts = ed.sqz_score_row(short_float_pct=30.0, rvol=3.0, upside_pct=20.0)
    assert parts["score"] == 100.0
    assert parts["s_shortfloat"] == 40.0
    assert parts["s_rvol"] == 40.0
    assert parts["s_upside"] == 20.0
    thin = ed.sqz_score_row(short_float_pct=15.0, rvol=1.5, upside_pct=5.0)
    assert thin["s_upside"] == 5.0
    assert ed.sqz_fire_signal(80.0, 2.0, 5.0) == ed.SQZ_FIRE
    assert ed.sqz_fire_signal(80.0, 2.0, 45.0) == ed.SQZ_CHASE
    assert ed.sqz_fire_signal(40.0, 1.0, 5.0) == ed.SQZ_WAIT


def test_partial_last_bar_dropped():
    import pandas as pd

    idx = pd.bdate_range("2026-07-01", periods=60)
    vol = [1_000_000.0] * 60
    vol[-1] = 100_000.0
    df = pd.DataFrame({"Close": [20.0] * 60, "Volume": vol}, index=idx)
    fixed = ed._drop_partial_last_bar(df)
    assert len(fixed) == 59
    assert ed._vol50_ratio(fixed) < 1.2
    vol[-1] = 2_000_000.0
    hot = pd.DataFrame({"Close": [20.0] * 60, "Volume": vol}, index=idx)
    assert len(ed._drop_partial_last_bar(hot)) == 60
    assert ed._vol50_ratio(hot) > 1.5


def test_short_float_fraction():
    pct, dtc = ed._pct_from_yf_info({
        "shortPercentOfFloat": 0.15, "shortRatio": 4.2,
    })
    assert pct == 15.0
    assert dtc == 4.2
    pct2, _ = ed._pct_from_yf_info({"sharesShort": 200.0, "floatShares": 1000.0})
    assert pct2 == 20.0
    assert ed._pct_from_yf_info({}) == (0.0, 0.0)


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
    assert pf.guardian(_m(force_exit_d3=True, pct=5.0))["code"] == "D3"
    assert pf.guardian(_m(pct=-5.0))["code"] == "SL"
    assert pf.guardian(_m(force_exit_d3=True))["code"] == "D3"
    assert pf.guardian(_m(has_quote=False))["code"] == "NOQUOTE"
    assert pf.guardian(_m())["code"] == "HOLD"
    assert pf.guardian(_m(strategy="LDPB"))["code"] == "LEGACY"
    low = pf.guardian(_m(strategy="SQUEEZE", target_consensus=104.0, upside=4.0))
    assert low["badge"] == "🟡 [저탄력 순항]"
    assert "+4.0%" in low["order"]
    fat = pf.guardian(_m(strategy="SQUEEZE", target_consensus=120.0, upside=20.0))
    assert fat["badge"] == "🟢 [순항 홀딩]"
    earn = pf.guardian(_m(strategy="EARNINGS", target_consensus=104.0, upside=4.0))
    assert earn["badge"] == "🟢 [순항 홀딩]"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
