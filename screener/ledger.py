"""추천 원장: 장중 추천 티커를 기록하고 그 세션 종가로 당일 적중 여부를 채점한다.

- 기록: 장중(LIVE) 스캔의 Top 후보만. (세션, 티커, 셋업)마다 처음 추천된 가격 하나.
- 채점: 추천 세션이 끝난 뒤 그 세션 일봉으로. 종가 > 추천가 이면 당일 적중.
- 눌림 반등(setup PB, v4.5): 신호일 종가가 추천가. 다음 세션 시가 진입 → 5일선 위 종가 또는 5거래일째 종가 청산으로
  채점한다 (수수료 0.2% 차감 후 수익 > 0 이면 적중). screener.pullback.grade 와 같은 규칙.
- Streamlit 에 의존하지 않는다. Gist 동기화는 app.py 가 cloud_sync 로 한다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

from . import fmp_client as fc
from . import pullback as pb

ET = ZoneInfo("America/New_York")
GIST_NAME = "rec_ledger.json"
DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache", GIST_NAME)
MAX_ROWS = 600
GIVE_UP_DAYS = 7  # 이 기간이 지나도 일봉이 없으면 채점 불가로 닫는다
PB_GIVE_UP_DAYS = 16  # 눌림 반등은 최대 5거래일 보유라 더 기다린다


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


def record_pullback(rows: list, pool: dict) -> int:
    """눌림 반등 풀의 추천(top)을 원장에 추가한다. 신호는 완결 일봉 기준이라 장중이 아니어도 기록한다."""
    if not pool or not pool.get("session"):
        return 0
    seen = {(r.get("session"), r.get("symbol"), r.get("setup")) for r in rows}
    added = 0
    for c in pool.get("top") or []:
        key = (pool["session"], c.get("symbol"), pb.SETUP)
        if None in key or key in seen:
            continue
        try:
            price = float(c.get("close"))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        rows.append({
            "session": key[0], "symbol": key[1], "setup": key[2], "rec_price": price,
            "rec_kst": pool.get("scanned_at_kst"), "entry_session": pool.get("entry_session"),
            "score": round(float(c.get("r60") or 0), 1), "rsi2": c.get("rsi2"), "r60": c.get("r60"),
            "outcome": None,
        })
        seen.add(key)
        added += 1
    return added


def recent_pullbacks(rows: list) -> dict:
    """{심볼: 마지막 눌림 반등 추천 신호일} (재추천 금지용)."""
    out: dict = {}
    for r in rows:
        if r.get("setup") == pb.SETUP and r.get("symbol") and r.get("session"):
            out[r["symbol"]] = max(out.get(r["symbol"], ""), r["session"])
    return out


def _session_over(session: str, now: dt.datetime) -> bool:
    now = now.astimezone(ET)
    d = dt.date.fromisoformat(session)
    return now.date() > d or (now.date() == d and now.hour >= 16)


def grade(rows: list, api_key: str, now: dt.datetime | None = None, fetch=None) -> int:
    """끝난 세션의 미채점 행을 채점한다. 채점한 개수를 돌려준다."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    fetch = fetch or (lambda sym: fc.fetch_eod(api_key, sym, 40))
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
        if r.get("setup") == pb.SETUP:
            o = pb.grade(bars, r["session"])
            if o is not None:
                r["outcome"] = o
                graded += 1
            elif (now.date() - dt.date.fromisoformat(r["session"])).days > PB_GIVE_UP_DAYS:
                r["outcome"] = {"missing": True}
                graded += 1
            continue
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
    """셋업별 적중률. 키: ALL(EP A+B) / A / B / PB(눌림 반등, 보유 기간 기준)."""
    out = {}

    def match(r, key):
        return r.get("setup") in ("A", "B") if key == "ALL" else r.get("setup") == key

    for key in ("ALL", "A", "B", pb.SETUP):
        done = [r["outcome"] for r in rows
                if isinstance(r.get("outcome"), dict) and "win" in r["outcome"] and match(r, key)]
        n = len(done)
        out[key] = {
            "n": n,
            "pending": sum(1 for r in rows if r.get("outcome") is None and match(r, key)),
            "win_rate": (sum(1 for o in done if o["win"]) / n * 100) if n else None,
            "avg_close": (sum(o["ret_close_pct"] for o in done) / n) if n else None,
            "avg_up": (sum(o["max_up_pct"] for o in done) / n) if n else None,
        }
    return out
