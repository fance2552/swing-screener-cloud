"""P0 session clock: ET + DST. Run from repo root:

    .venv/bin/python tests/test_session_clock.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "screener" / "radar.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util

_radar_path = ROOT / "screener" / "radar.py"
_spec = importlib.util.spec_from_file_location("p0_radar", _radar_path)
radar = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(radar)

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


def _kst(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=KST)


CASES = [
    (_kst(2026, 10, 4, 17, 30), "CLOSED", "Sun 17:30 KST = Sun 04:30 ET"),
    (_kst(2026, 10, 4, 22, 31), "CLOSED", "Sun 22:31 KST = Sun 09:31 ET"),
    (_kst(2026, 10, 5, 2, 0), "CLOSED", "Mon 02:00 KST = Sun 13:00 ET"),
    (_kst(2026, 10, 5, 17, 30), "PRE", "Mon 17:30 KST = Mon 04:30 ET pre"),
    (_kst(2026, 10, 5, 21, 0), "PRE", "Mon 21:00 KST = Mon 08:00 ET pre"),
    (_kst(2026, 10, 5, 22, 29, 59), "PRE", "Mon 22:29:59 KST still PRE"),
    (_kst(2026, 10, 5, 22, 30, 0), "RTH", "Mon 22:30:00 KST = 09:30 ET open"),
    (_kst(2026, 10, 5, 22, 44, 59), "RTH", "Mon 22:44:59 KST RTH but fire locked"),
    (_kst(2026, 10, 5, 22, 45, 0), "RTH", "Mon 22:45:00 KST fire unlock"),
    (_kst(2026, 10, 6, 4, 59), "RTH", "Tue 04:59 KST = Mon 15:59 ET"),
    (_kst(2026, 10, 6, 5, 0), "AH", "Tue 05:00 KST = Mon 16:00 ET"),
    (_kst(2026, 10, 6, 8, 59), "AH", "Tue 08:59 KST = Mon 19:59 ET"),
    (_kst(2026, 10, 6, 9, 0), "CLOSED", "Tue 09:00 KST = Mon 20:00 ET"),
    (_kst(2026, 10, 10, 1, 0), "RTH", "Sat 01:00 KST = Fri 12:00 ET RTH"),
    (_kst(2026, 11, 2, 22, 45), "PRE", "DST end: Mon 22:45 KST = Mon 08:45 ET"),
    (_kst(2026, 11, 3, 5, 30), "RTH", "DST end: Tue 05:30 KST = Mon 15:30 ET"),
    (_kst(2026, 11, 26, 23, 0), "CLOSED", "Thanksgiving 2026"),
]


def main() -> int:
    failed = 0
    for ts, expect, label in CASES:
        got = radar.us_session(ts)["key"]
        ok = got == expect
        fire = radar.session_allows_fire_alert(ts)
        if ts == _kst(2026, 10, 5, 22, 44, 59) and fire:
            print(f"FAIL fire lock {label}: fire={fire}")
            failed += 1
        if ts == _kst(2026, 10, 5, 22, 45, 0) and not fire:
            print(f"FAIL fire unlock {label}: fire={fire}")
            failed += 1
        if not ok:
            et = ts.astimezone(ET)
            print(f"FAIL {label}: got {got} expected {expect} (ET {et:%a %H:%M})")
            failed += 1
        else:
            print(f"OK   {label}: {got} fire={fire}")
    # DST open chime must use 09:30 ET, not 22:30 KST
    dst_open = datetime(2026, 11, 2, 22, 30, 1, tzinfo=KST)
    if radar.is_rth_open_chime_window(dst_open):
        print("FAIL DST open chime at 22:30 KST after fall-back")
        failed += 1
    else:
        print("OK   DST 22:30 KST is not the open chime")
    real_open = datetime(2026, 11, 2, 23, 30, 1, tzinfo=KST)  # 09:30 ET after DST
    if not radar.is_rth_open_chime_window(real_open):
        print(f"FAIL real open chime {real_open.astimezone(ET)}")
        failed += 1
    else:
        print("OK   DST 23:30 KST = 09:30 ET open chime")
    print(f"failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
