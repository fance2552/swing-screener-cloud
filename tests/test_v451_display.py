import datetime as dt
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from screener import episodic_pivot as ep  # noqa: E402
from ui import verdict as vd  # noqa: E402

KST = vd.KST


def test_live_bar_with_int_volume_and_fractional_quote():
    # FMP 일봉 거래량은 int64, 시세 거래량은 소수 → 예전엔 TypeError('처리 오류')로 후보가 빠졌다.
    sess = dt.date(2026, 10, 19)
    dates = pd.bdate_range(end=pd.Timestamp(sess), periods=40)
    bars = pd.DataFrame({"date": dates, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0,
                         "volume": [1_000_000] * 40})
    q = {"open": 12.0, "high": 13.0, "low": 11.5, "price": 12.5, "volume": 2979489.26516}
    out = ep.attach_live_bar(bars, q, sess, "LIVE")
    assert out["volume"].iloc[-1] == 2979489.26516 and out["close"].iloc[-1] == 12.5


def test_kst_follows_dst():
    summer = vd.kst_at(dt.date(2026, 10, 20), dt.time(9, 30))
    winter = vd.kst_at(dt.date(2026, 12, 1), dt.time(9, 30))
    assert summer.strftime("%H:%M") == "22:30" and winter.strftime("%H:%M") == "23:30"
    now = dt.datetime(2026, 10, 20, 12, 0, tzinfo=KST)
    assert vd.fmt_kst(summer, now) == "오늘 밤 22:30"
    assert vd.pb_when(dt.date(2026, 10, 21), now).startswith("내일 밤 22:30 본장 시가")
    late = dt.datetime(2026, 10, 20, 23, 30, tzinfo=KST)
    assert vd.pb_entry_passed(dt.date(2026, 10, 20), late)
    assert vd.pb_when(dt.date(2026, 10, 20), late).startswith("진입 시각 지남: 오늘 밤 22:30")


def test_ep_order_uses_orh_entry_with_plan_percent():
    plan = {"entry_lo": 74.77, "entry_hi": 75.14, "stop_pct": 3.5, "t1_pct": 7.0}
    scan = vd.ep_order(plan)
    assert scan["basis"] == "SCAN" and round(scan["stop"], 2) == 72.51
    live = vd.ep_order(plan, {"entry_lo": 76.57, "entry_hi": 77.26})
    assert live["basis"] == "ORH" and round(live["stop"], 2) == round(77.26 * 0.965, 2)
    assert round(live["t1"], 2) == round(77.26 * 1.07, 2)


def test_headlines():
    assert vd.ep_headline("IRON", "LIVE", {"state": "GO"}).startswith("✅ 매수 추천: IRON")
    assert vd.ep_headline("IRON", "LIVE", {"state": "BREAK"}).startswith("⛔")
    assert vd.ep_none_line("LIVE") == "⛔ 지금은 추천 없음"
    assert vd.ep_none_line("CLOSED") == "⛔ 오늘은 추천 없음"
    assert vd.guard_headline("XP", {"signal": "EXIT", "net_pct": 1.234}) == "🔴 지금 매도하세요: XP (순손익 +1.23%)"


def test_lod_note_explains_instead_of_contradicting():
    assert vd.lod_note({"lod_cap_exceeded": False}) is None
    note = vd.lod_note({"lod_cap_exceeded": True, "struct_stop_dist_pct": 6.9, "stop_pct": 3.5})
    assert "6.9%" in note and "-3.5%" in note and "조였" not in note


def _probe(path, ok, status, label=None):
    return {"label": label or path, "path": path, "ok": ok, "rows": 1 if ok else 0, "status": status}


def test_probe_plan_limit_with_fallback_is_yellow():
    rows = [_probe("quote", True, 200), _probe("batch-quote", False, 402),
            _probe("news/stock", True, 200), _probe("news/press-releases", False, 402)]
    assert vd.probe_status(rows[1], rows)[0] == "🟡"
    assert vd.probe_status(rows[3], rows)[0] == "🟡"
    assert vd.probe_summary(rows)[0] == "ok"


def test_probe_real_failures_are_red():
    rows = [_probe("quote", False, 500), _probe("batch-quote", False, 402), _probe("earnings", False, 402)]
    assert vd.probe_status(rows[0], rows)[0] == "🔴"
    assert vd.probe_status(rows[1], rows)[0] == "🟠"   # 대체 경로(quote)도 실패
    assert vd.probe_status(rows[2], rows)[0] == "🟠"
    assert vd.probe_status(_probe("quote", False, None), rows)[0] == "🔴"
    assert vd.probe_summary(rows)[0] == "bad"


def test_pb_glance_lines():
    # v4.5.5: 수호 데스크 눌림 반등 카드 — 5일선 위·아래와 보유 일수 카운트
    from screener import portfolio as pf
    c, line = vd.pb_sma5_line(19.35, 20.41)
    assert c == "blue" and "5일선 아래" in line and "5.2%" in line
    c, line = vd.pb_sma5_line(20.8, 20.41)
    assert c == "green" and "오늘 종가에 전량 매도" in line
    assert vd.pb_sma5_line(None, 20.41)[1].endswith("데이터 없음")
    last = pf.pb_last_day("2026-10-20")
    assert last == dt.date(2026, 10, 26)
    assert vd.pb_count_line(1, 5, last) == "📅 보유 1/5일째 ●○○○○ · 마지막 매도일 10-26(월) 종가"


def test_merge_agent_holdings():
    # v4.5.7: 에이전트 보유 목록이 기준. 같은 티커는 에이전트 값으로, 에이전트에서 온 종목이 빠지면 매도 반영.
    from screener import portfolio as pf
    bot = [pf.normalize({"ticker": "QMCO", "qty": 14, "avg": 17.68, "opened": "2026-10-20", "strategy": "PB"}),
           pf.normalize({"ticker": "AAPL", "qty": 1, "avg": 200, "opened": "2026-10-20"})]
    agent = [{"symbol": "QMCO", "qty": 14, "avg": 17.73, "opened": "2026-10-20", "strategy": "PB", "saved": True},
             {"symbol": "NVDA", "qty": 3, "avg": 150.0, "opened": "2026-10-21", "strategy": "EP"}]
    out, added, removed, changed = pf.merge_agent(bot, agent)
    by = {p["ticker"]: p for p in out}
    assert changed and added == ["NVDA"] and removed == []
    assert by["QMCO"]["avg"] == 17.73 and by["QMCO"]["source"] == pf.AGENT
    assert by["AAPL"].get("source") is None and "symbol" not in by["NVDA"]
    assert pf.merge_agent(out, agent)[3] is False          # 두 번째는 바뀐 것 없음
    out2, _, removed2, _ = pf.merge_agent(out, agent[:1])  # NVDA 매도
    assert removed2 == ["NVDA"] and {p["ticker"] for p in out2} == {"QMCO", "AAPL"}
