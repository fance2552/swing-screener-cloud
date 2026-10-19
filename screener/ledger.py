"""추천 원장: 장중 추천 티커를 기록하고 그 세션 종가로 당일 적중 여부를 채점한다.

- 기록: 장중(LIVE) 스캔의 Top 후보만. (세션, 티커, 셋업)마다 처음 추천된 가격 하나.
- 채점: 추천 세션이 끝난 뒤 그 세션 일봉으로. 종가 > 추천가 이면 당일 적중.
- Streamlit 에 의존하지 않는다. Gist 동기화는 app.py 가 cloud_sync 로 한다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

from . import fmp_client as fc

ET = ZoneInfo("America/New_York")
GIST_NAME = "rec_ledger.json"
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache", GIST_NAME)
MAX_ROWS = 600
GIVE_UP_DAYS = 7  # 이 기간이 지나도 일봉이 없으면 채점 불가로 닫는다


def load(path: str = DEFAULT_PATH) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def save(rows: list, path: str = DEFAULT_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows[-MAX_ROWS:], f, ensure_ascii=False)
    os.replace(tmp, path)


def record(rows: list, pool: dict) -> int:
    """장중 스캔 풀의 Top 후보를 원장에 추가한다. 새로 추가한 개수를 돌려준다."""
    if not pool or pool.get("state") != "LIVE":
        return 0
    seen = {(r.get("session"), r.get("symbol"), r.get("setup")) for r in rows}
    added = 0
    for c in pool.get("top") or []:
        key = (pool.get("session"), c.get("symbol"), c.get("setup"))
        if None in key or key in seen:
            continue
        try:
            price = float(c.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        rows.append({
            "session": key[0], "symbol": key[1], "setup": key[2],
            "rec_price": price, "rec_kst": pool.get("scanned_at_kst"), "minutes": pool.get("minutes"),
            "score": c.get("score"), "gap": c.get("gap"), "volx": c.get("volx"),
            "catalyst": (c.get("catalyst") or {}).get("type"), "hold": c.get("hold"),
            # 아직 점수에 넣지 않은 신호. 적중률이 쌓이면 이 값들로 가중치를 정한다.
            "float_turn": c.get("float_turn"),
            "sector_etf_pct": (c.get("sector") or {}).get("etf_vs_open_pct"),
            "peers": len((c.get("sector") or {}).get("peers") or []),
            "outcome": None,
        })
        seen.add(key)
        added += 1
    return added


def _session_over(session: str, now: dt.datetime) -> bool:
    now = now.astimezone(ET)
    d = dt.date.fromisoformat(session)
    return now.date() > d or (now.date() == d and now.hour >= 16)


def grade(rows: list, api_key: str, now: dt.datetime | None = None, fetch=None) -> int:
    """끝난 세션의 미채점 행을 채점한다. 채점한 개수를 돌려준다."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    fetch = fetch or (lambda sym: fc.fetch_eod(api_key, sym, 15))
    graded = 0
    cache: dict = {}
    for r in rows:
        if r.get("outcome") is not None or not r.get("session"):
            continue
        if not _session_over(r["session"], now):
            continue
        sym = r["symbol"]
        if sym not in cache:
            try:
                cache[sym] = fetch(sym)
            except Exception:
                cache[sym] = None
        bars = cache[sym]
        bar = None
        if bars is not None and len(bars):
            hit = bars[bars["date"].dt.date == dt.date.fromisoformat(r["session"])]
            if len(hit):
                bar = hit.iloc[-1]
        if bar is None:
            if (now.date() - dt.date.fromisoformat(r["session"])).days > GIVE_UP_DAYS:
                r["outcome"] = {"missing": True}
                graded += 1
            continue
        px = float(r["rec_price"])
        close, high, low = float(bar["close"]), float(bar["high"]), float(bar["low"])
        r["outcome"] = {
            "close": close, "high": high, "low": low,
            "ret_close_pct": round((close / px - 1) * 100, 2),
            "max_up_pct": round((high / px - 1) * 100, 2),
            "max_dn_pct": round((low / px - 1) * 100, 2),
            "win": close > px,
        }
        graded += 1
    return graded


def stats(rows: list) -> dict:
    """셋업별 당일 적중률. 키: ALL / A / B."""
    out = {}
    for key in ("ALL", "A", "B"):
        done = [r["outcome"] for r in rows
                if isinstance(r.get("outcome"), dict) and "win" in r["outcome"]
                and (key == "ALL" or r.get("setup") == key)]
        n = len(done)
        out[key] = {
            "n": n,
            "pending": sum(1 for r in rows if r.get("outcome") is None and (key == "ALL" or r.get("setup") == key)),
            "win_rate": (sum(1 for o in done if o["win"]) / n * 100) if n else None,
            "avg_close": (sum(o["ret_close_pct"] for o in done) / n) if n else None,
            "avg_up": (sum(o["max_up_pct"] for o in done) / n) if n else None,
        }
    return out
