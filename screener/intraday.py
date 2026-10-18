"""실시간 진입 타이밍 (v4.3): 시가 범위 고점(ORH) 돌파 판정.

분봉 경로: FMP 5분봉 → Alpaca 5분봉(IEX) → Yahoo 5분봉. 앞 경로가 비면 다음으로 넘어간다.
판정만 한다. 주문은 보내지 않는다. Streamlit 에 의존하지 않는다.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from . import fmp_client as fc

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
OPEN_T = dt.time(9, 30)
CLOSE_T = dt.time(16, 0)
_TIMEOUT = 8
_COLS = ["t", "open", "high", "low", "close", "volume"]


@dataclass
class OrCfg:
    or_minutes: int = 15        # 시가 범위 = 개장 후 첫 15분
    entry_buf_pct: float = 0.1  # 진입 하단 = ORH +0.1%
    chase_pct: float = 1.0      # ORH 보다 1% 넘게 오르면 추격 금지
    vol_confirm: float = 0.8    # 돌파 5분봉 거래량 >= 장중 5분봉 평균 x 0.8


# ----------------------------------------------------------------------------
# 분봉 파서 (공급자별 응답 → 공통 DataFrame, ET 시각)
# ----------------------------------------------------------------------------
def _frame(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=_COLS)
    if df.empty:
        return df
    for c in _COLS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna().sort_values("t").drop_duplicates("t").reset_index(drop=True)
    return df


def parse_fmp(data, day: dt.date) -> pd.DataFrame:
    """FMP historical-chart/5min: [{date 'YYYY-MM-DD HH:MM:SS'(ET), open, high, low, close, volume}]."""
    rows = []
    for r in data if isinstance(data, list) else []:
        try:
            t = dt.datetime.fromisoformat(str(r["date"])[:19]).replace(tzinfo=ET)
        except Exception:
            continue
        if t.date() == day:
            rows.append([t, r.get("open"), r.get("high"), r.get("low"), r.get("close"), r.get("volume")])
    return _frame(rows)


def parse_alpaca(data, day: dt.date) -> pd.DataFrame:
    """Alpaca v2 bars: {'bars': [{t ISO UTC, o, h, l, c, v}]}."""
    rows = []
    bars = data.get("bars") if isinstance(data, dict) else None
    for r in bars or []:
        try:
            t = dt.datetime.fromisoformat(str(r["t"]).replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue
        if t.date() == day:
            rows.append([t, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v")])
    return _frame(rows)


def parse_yahoo(data, day: dt.date) -> pd.DataFrame:
    """Yahoo v8 chart: chart.result[0].timestamp + indicators.quote[0]."""
    try:
        res = data["chart"]["result"][0]
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
    except Exception:
        return _frame([])
    rows = []
    for i, epoch in enumerate(ts):
        t = dt.datetime.fromtimestamp(int(epoch), UTC).astimezone(ET)
        if t.date() != day:
            continue
        try:
            rows.append([t, q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]])
        except (KeyError, IndexError):
            continue
    return _frame(rows)


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return _frame([])
    times = df["t"].map(lambda t: t.time())
    return df[(times >= OPEN_T) & (times < CLOSE_T)].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 공급자 호출
# ----------------------------------------------------------------------------
def _fmp(sym: str, day: dt.date, fmp_key: str) -> pd.DataFrame:
    data = fc.fmp_get("historical-chart/5min", fmp_key,
                      {"symbol": sym, "from": day.isoformat(), "to": day.isoformat()}, retries=1)
    return parse_fmp(data, day)


def _alpaca(sym: str, day: dt.date, key: str, secret: str) -> pd.DataFrame:
    if not key or not secret:
        return _frame([])
    start = dt.datetime.combine(day, OPEN_T, ET).astimezone(UTC)
    end = dt.datetime.combine(day, CLOSE_T, ET).astimezone(UTC)
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{sym}/bars",
                     headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                     params={"timeframe": "5Min", "start": start.isoformat().replace("+00:00", "Z"),
                             "end": end.isoformat().replace("+00:00", "Z"), "feed": "iex", "limit": 100},
                     timeout=_TIMEOUT)
    r.raise_for_status()
    return parse_alpaca(r.json(), day)


def _yahoo(sym: str, day: dt.date) -> pd.DataFrame:
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"interval": "5m", "range": "5d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=_TIMEOUT)
    r.raise_for_status()
    return parse_yahoo(r.json(), day)


def fetch_5min(sym: str, day: dt.date, fmp_key: str = "", alpaca: tuple[str, str] = ("", ""),
               providers=None) -> tuple[pd.DataFrame, str, list]:
    """(정규장 5분봉, 출처, 실패 사유). 출처는 FMP / ALPACA / YAHOO / ''."""
    sym = sym.strip().upper()
    providers = providers or (
        ("FMP", lambda: _fmp(sym, day, fmp_key) if fmp_key else _frame([])),
        ("ALPACA", lambda: _alpaca(sym, day, *alpaca)),
        ("YAHOO", lambda: _yahoo(sym, day)),
    )
    errors = []
    for name, call in providers:
        try:
            df = regular_session(call())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        if len(df):
            return df, name, errors
        errors.append(f"{name}: 분봉 없음")
    return _frame([]), "", errors


def probe(sym: str, day: dt.date, fmp_key: str = "", alpaca: tuple[str, str] = ("", ""), providers=None) -> list:
    """공급자마다 따로 호출해 결과를 돌려준다 (진단용). [{name, ok, bars, first, last, error}]."""
    sym = sym.strip().upper()
    providers = providers or (
        ("FMP", lambda: _fmp(sym, day, fmp_key) if fmp_key else None),
        ("ALPACA", lambda: _alpaca(sym, day, *alpaca) if all(alpaca) else None),
        ("YAHOO", lambda: _yahoo(sym, day)),
    )
    out = []
    for name, call in providers:
        row = {"name": name, "ok": False, "bars": 0, "first": None, "last": None, "error": ""}
        try:
            raw = call()
            if raw is None:
                row["error"] = "키 없음"
            else:
                df = regular_session(raw)
                row["bars"] = len(df)
                if len(df):
                    row.update(ok=True, first=df["t"].iloc[0].strftime("%H:%M"), last=df["t"].iloc[-1].strftime("%H:%M"))
                else:
                    status = fc.LAST_HTTP_STATUS if name == "FMP" else None
                    row["error"] = "분봉 없음" + (f" (HTTP {status})" if status and status != 200 else "")
        except requests.HTTPError as exc:
            code = getattr(exc.response, "status_code", None)
            row["error"] = f"HTTP {code}" if code else type(exc).__name__
        except Exception as exc:  # noqa: BLE001
            row["error"] = type(exc).__name__
        out.append(row)
    return out


# ----------------------------------------------------------------------------
# 시가 범위 돌파 판정
# ----------------------------------------------------------------------------
def opening_range(df: pd.DataFrame, day: dt.date, cfg: OrCfg | None = None):
    """(ORH, ORL, 확정 여부). 범위를 덮는 봉이 없으면 None."""
    cfg = cfg or OrCfg()
    if df is None or df.empty:
        return None
    o = dt.datetime.combine(day, OPEN_T, ET)
    end = o + dt.timedelta(minutes=cfg.or_minutes)
    win = df[(df["t"] >= o) & (df["t"] < end)]
    if win.empty:
        return None
    # 5분봉의 시각은 시작 시각이다. 마지막 범위 봉(end-5분)이 있으면 범위가 닫힌 것으로 본다.
    closed = bool((df["t"] >= end).any()) or win["t"].max() >= end - dt.timedelta(minutes=5)
    return float(win["high"].max()), float(win["low"].min()), closed


def entry_signal(df: pd.DataFrame, price: float | None, day: dt.date, now: dt.datetime,
                 cfg: OrCfg | None = None) -> dict:
    """진입 상태 판정.

    WAIT_OR  시가 범위 형성 중        WAIT  ORH 아래, 돌파 대기
    GO       돌파 + 거래량 동반       WEAK  돌파했지만 거래량 약함
    CHASE    ORH +1% 초과, 추격 금지  FAIL  돌파 후 ORH 아래로 복귀
    BREAK    시가 범위 저점 이탈      NO_DATA 분봉 없음
    """
    cfg = cfg or OrCfg()
    now = now.astimezone(ET)
    o = dt.datetime.combine(day, OPEN_T, ET)
    or_end = o + dt.timedelta(minutes=cfg.or_minutes)
    out = {"state": "NO_DATA", "orh": None, "orl": None, "entry_lo": None, "entry_hi": None,
           "dist_pct": None, "text": "분봉 데이터 없음 — 진입 타이밍 판정 불가"}
    if now < or_end:
        mins = int((or_end - now).total_seconds() // 60) + 1
        out.update(state="WAIT_OR", text=f"⏳ 시가 범위 형성 중 — 약 {mins}분 뒤(ET {or_end:%H:%M}) 확정")
        return out
    rng = opening_range(df, day, cfg)
    if rng is None:
        return out
    orh, orl, _ = rng
    after = df[df["t"] >= or_end]
    px = float(price) if price else (float(df["close"].iloc[-1]) if len(df) else None)
    if not px:
        return out
    entry_lo = orh * (1 + cfg.entry_buf_pct / 100.0)
    entry_hi = orh * (1 + cfg.chase_pct / 100.0)
    dist = (px / orh - 1) * 100.0
    out.update(orh=orh, orl=orl, entry_lo=entry_lo, entry_hi=entry_hi, dist_pct=dist)
    broke = after[after["high"] > orh]
    if px < orl:
        out.update(state="BREAK", text=f"⛔ 시가 범위 저점({orl:.2f}) 이탈 — 오늘 진입 금지")
    elif px <= orh:
        if len(broke):
            out.update(state="FAIL", text=f"⚠️ 돌파 실패 — ORH {orh:.2f} 아래로 복귀 ({dist:+.1f}%)")
        else:
            out.update(state="WAIT", text=f"⏳ 돌파 대기 — ORH {orh:.2f}까지 {-dist:.1f}%")
    elif px > entry_hi:
        out.update(state="CHASE", text=f"🚀 추격 금지 — ORH 대비 {dist:+.1f}% 이미 상승 (눌림 대기)")
    else:
        first = broke.iloc[0] if len(broke) else None
        avg = float(df["volume"].mean()) if len(df) else 0.0
        vol_ok = first is not None and avg > 0 and float(first["volume"]) >= cfg.vol_confirm * avg
        if vol_ok:
            out.update(state="GO", text=f"✅ 진입 가능 — ORH {orh:.2f} 거래량 동반 돌파 ({dist:+.1f}%)")
        else:
            out.update(state="WEAK", text=f"🟡 돌파했지만 거래량 약함 — ORH {orh:.2f} ({dist:+.1f}%)")
    return out
