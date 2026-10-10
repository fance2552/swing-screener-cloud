"""Event-driven dual hunt: earnings run-up + short squeeze. No broker send. No LDPB."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

import config
import env_settings
from slog import slog

_ET = ZoneInfo("America/New_York")
_KST = ZoneInfo("Asia/Seoul")

EARN_FIRE = "🎯 런업 사격! (D-Day)"
EARN_WAIT = "⏳ 진입 대기 (D-12 대기)"
EARN_LOW = "⏳ 점수 미달 (관망)"
EARN_CLOSED = "⏳ 윈도우 종료 (관망)"
EARN_BAN = "🚫 진입 금지 (실적 임박)"

SQZ_FIRE = "🎯 스퀴즈 사격!"
SQZ_WAIT = "⏳ 스퀴즈 관망"
SQZ_CHASE = "🚫 추격 금지 (과열)"

_SHORT_SEED_PATH = Path(__file__).resolve().parent.parent / "data" / "seed" / "short_float_seed.json"
_SHORT_SEED: dict[str, Any] | None = None
SHORT_SEED_NOTE = ""


# ---------------------------------------------------------------- dates / sessions

def today_et() -> date:
    return datetime.now(_ET).date()


def _parse(raw: Any) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def days_to_event(target_date: Any, today: date | None = None) -> int | None:
    d = _parse(target_date)
    if d is None:
        return None
    return (d - (today or today_et())).days


def exit_deadline(target_date: Any, before_days: int | None = None) -> date | None:
    """target_date - before_days; if that lands on a weekend, the prior Friday."""
    d = _parse(target_date)
    if d is None:
        return None
    n = int(before_days if before_days is not None else config.EXIT_D3_DAYS)
    x = d - timedelta(days=n)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def must_exit_for_earnings(target_date: Any, today: date | None = None) -> bool:
    d = _parse(target_date)
    if d is None:
        return False
    today = today or today_et()
    if (d - today).days <= int(config.EXIT_D3_DAYS):
        return True
    dl = exit_deadline(target_date)
    return dl is not None and today >= dl


def business_days_held(entry_date: Any, today: date | None = None) -> int:
    d = _parse(entry_date)
    if d is None:
        return 1
    today = today or today_et()
    n = int(np.busday_count(d, today + timedelta(days=1)))
    return max(n, 1)


def is_friday_flat_window(now_et: datetime | None = None) -> bool:
    """금요일 15:30 ET 이후. 무조건 청산이 아니라 리스크 컷 시각이다."""
    ts = now_et or datetime.now(_ET)
    if ts.weekday() != 4:
        return False
    threshold = ts.replace(
        hour=15,
        minute=30,
        second=0,
        microsecond=0,
    )
    return ts >= threshold


# ---------------------------------------------------------------- shared HTTP helpers

def _fmp_get(path: str, params: dict[str, Any], timeout: int = 20) -> Any:
    key = env_settings.fmp_key()
    if not key:
        return None
    base = str(env_settings.fmp_base() or "https://financialmodelingprep.com").rstrip("/")
    q = dict(params)
    q["apikey"] = key
    try:
        resp = requests.get(f"{base}{path}", params=q, timeout=timeout)
    except requests.RequestException as exc:
        slog(f"FMP {path} 요청 실패 {type(exc).__name__}")
        return None
    if resp.status_code != 200:
        slog(f"FMP {path} HTTP {resp.status_code}")
        return None
    try:
        return resp.json()
    except ValueError:
        slog(f"FMP {path} JSON 파싱 실패")
        return None


def next_earnings_date(symbol: str) -> str:
    """Best-effort single-symbol lookup via /stable/earnings. Empty string on any failure."""
    data = _fmp_get("/stable/earnings", {"symbol": symbol, "limit": 12})
    if not isinstance(data, list):
        return ""
    today = today_et()
    best = ""
    for item in data:
        if not isinstance(item, dict):
            continue
        d = _parse(item.get("date"))
        if d is None or d < today:
            continue
        iso = d.isoformat()
        if not best or iso < best:
            best = iso
    return best


# ---------------------------------------------------------------- calendar (earnings)

def fetch_calendar(today: date | None = None, horizon: int | None = None) -> dict[str, str]:
    """symbol -> earliest earnings date (ISO) within [today, today+horizon]. One call per scan."""
    today = today or today_et()
    horizon = int(horizon or config.EARN_HORIZON_DAYS)
    params = {"from": today.isoformat(), "to": (today + timedelta(days=horizon)).isoformat()}
    data: list[Any] = []
    for path in ("/stable/earnings-calendar", "/api/v3/earning_calendar"):
        payload = _fmp_get(path, params)
        if isinstance(payload, list) and payload:
            data = payload
            break
    out: dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "").upper().strip()
        d = _parse(item.get("date"))
        if not sym or d is None or d < today:
            continue
        iso = d.isoformat()
        prev = out.get(sym)
        if prev is None or iso < prev:
            out[sym] = iso
    slog(f"실적 캘린더 {len(out)}종 ({params['from']} ~ {params['to']})")
    return out


# ---------------------------------------------------------------- shared universe

def fetch_universe() -> list[dict[str, Any]]:
    """One FMP screener pull wide enough for both scanners. Falls back to [] on failure
    (engine.py must fall back to the Wikipedia universe on empty, same as before)."""
    from data import fmp

    rows = fmp.company_screener()
    slog(
        f"공용 유니버스 {len(rows)}개 확보 "
        f"(price>={config.PRICE_MIN} cap {config.MARKET_CAP_MIN:,}~{config.MARKET_CAP_MAX:,})"
    )
    return rows


# ---------------------------------------------------------------- shared bar stats

def _hist_stats(df: pd.DataFrame | None) -> dict[str, float] | None:
    """price / sma10 / sma50 / adv20 / today_vol / ret_5d, or None if too short."""
    if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
        return None
    close = df["Close"].dropna()
    if len(close) < 50:
        return None
    price = float(close.iloc[-1])
    sma10 = float(close.rolling(10).mean().iloc[-1]) if len(close) >= 10 else 0.0
    sma50 = float(close.rolling(50).mean().iloc[-1])
    adv20 = 0.0
    today_vol = 0.0
    if "Volume" in df.columns:
        vol = df["Volume"].dropna()
        if len(vol) >= 20:
            adv20 = float(vol.tail(20).mean())
        if len(vol) >= 1:
            today_vol = float(vol.iloc[-1])
    ret_5d = 0.0
    if len(close) >= 6 and close.iloc[-6] > 0:
        ret_5d = (float(close.iloc[-1]) / float(close.iloc[-6]) - 1.0) * 100.0
    return {
        "price": price, "sma10": sma10, "sma50": sma50,
        "adv20": adv20, "today_vol": today_vol, "ret_5d": ret_5d,
    }


def fmt_cap(value: Any) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "—"
    if x <= 0:
        return "—"
    return f"${x / 1e9:.2f}B" if x >= 1e9 else f"${x / 1e6:.0f}M"


# =================================================================
# EARNINGS RUN-UP (Tab 1 left)
# =================================================================

def _earn_dday_points(d: int) -> float:
    if 7 <= d <= 12:
        return 40.0
    if 13 <= d <= 14:
        return 25.0
    if 5 <= d <= 6:
        return 10.0
    return 0.0


def _earn_rating_points(buy_ratio: float) -> float:
    full = float(config.EARN_RATING_FULL_PCT)
    return max(0.0, min(30.0, 30.0 * float(buy_ratio) / full)) if full > 0 else 0.0


def _earn_upside_points(up: float) -> float:
    full = float(config.EARN_UPSIDE_FULL_PCT)
    return max(0.0, min(15.0, 15.0 * float(up) / full)) if full > 0 else 0.0


def _earn_sma_points(price: float, sma50: float) -> float:
    return 15.0 if price > 0 and sma50 > 0 and price > sma50 else 0.0


def _earn_upside_pct(price: float, target: float) -> float:
    return (target - price) / price * 100.0 if price > 0 and target > 0 else 0.0


def earn_score_row(row: dict[str, Any], price: float, today: date | None = None) -> dict[str, Any]:
    today = today or today_et()
    d = days_to_event(row.get("earnings_date"), today)
    d = -999 if d is None else int(d)
    buy_ratio = float(row.get("buy_ratio") or 0.0)
    target = float(row.get("target_consensus") or 0.0)
    sma50 = float(row.get("sma50") or 0.0)
    up = _earn_upside_pct(price, target)
    s_dday = _earn_dday_points(d)
    s_rating = _earn_rating_points(buy_ratio)
    s_sma = _earn_sma_points(price, sma50)
    s_up = _earn_upside_points(up)
    return {
        "d_day": d, "upside": up,
        "s_dday": s_dday, "s_rating": s_rating, "s_sma": s_sma, "s_upside": s_up,
        "score": round(s_dday + s_rating + s_sma + s_up, 2),
    }


def earn_fire_signal(d: int, score: float) -> tuple[str, int]:
    if d <= 4:
        return EARN_BAN, 3
    if 7 <= d <= 12:
        return (EARN_FIRE, 1) if score >= float(config.EARN_FIRE_MIN_SCORE) else (EARN_LOW, 2)
    if d >= 13:
        return EARN_WAIT, 2
    return EARN_CLOSED, 2


def earn_rank_live(
    rows: list[dict[str, Any]],
    quote_fn: Callable[[str], float] | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    today = today or today_et()
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        px = 0.0
        if quote_fn is not None:
            try:
                px = float(quote_fn(str(row.get("ticker") or "")) or 0.0)
            except (TypeError, ValueError):
                px = 0.0
        if px <= 0:
            px = float(row.get("price") or 0.0)
        row["price"] = px
        parts = earn_score_row(row, px, today)
        row.update(parts)
        sig, prio = earn_fire_signal(int(parts["d_day"]), float(parts["score"]))
        row["signal"], row["_priority"] = sig, prio
        out.append(row)
    out.sort(key=lambda r: (int(r["_priority"]), -float(r["score"]), str(r.get("ticker") or "")))
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


def _analyst_one(sym: str) -> tuple[str, dict[str, Any]]:
    from data.fmp import fetch_fmp_analyst_data

    try:
        wall = fetch_fmp_analyst_data(sym) or {}
    except Exception:  # noqa: BLE001
        wall = {}
    return sym, wall if isinstance(wall, dict) else {}


def _attach_analyst(pool: list[dict[str, Any]]) -> int:
    if not pool:
        return 0
    syms = [c["ticker"] for c in pool]
    got: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for sym, wall in ex.map(_analyst_one, syms):
            got[sym] = wall
    missing = 0
    for c in pool:
        wall = got.get(c["ticker"]) or {}
        try:
            c["buy_ratio"] = float(wall.get("buy_ratio") or 0.0)
        except (TypeError, ValueError):
            c["buy_ratio"] = 0.0
        try:
            c["target_consensus"] = float(wall.get("target_consensus") or 0.0)
        except (TypeError, ValueError):
            c["target_consensus"] = 0.0
        if c["buy_ratio"] <= 0 and c["target_consensus"] <= 0:
            missing += 1
    return missing


def scan_earnings(
    universe: list[dict[str, Any]],
    hist: dict[str, pd.DataFrame],
    today: date | None = None,
) -> tuple[list[dict[str, Any]], str, dict[str, str]]:
    today = today or today_et()
    cal = fetch_calendar(today)
    if not cal:
        return [], "실적 캘린더 0건 (FMP 키/플랜/응답 확인)", {}

    meta = {str(r.get("symbol") or "").upper(): r for r in universe if r.get("symbol")}
    skip = {"universe": 0, "hist": 0, "cap": 0, "price": 0, "vol": 0}
    cands: list[dict[str, Any]] = []
    for sym, edate in cal.items():
        r = meta.get(sym)
        if r is None:
            skip["universe"] += 1
            continue
        stat = _hist_stats(hist.get(sym))
        if stat is None:
            skip["hist"] += 1
            continue
        try:
            cap = float(r.get("marketCap") or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        if not (config.EARN_CAP_MIN <= cap <= config.EARN_CAP_MAX):
            skip["cap"] += 1
            continue
        if stat["price"] <= config.EARN_PRICE_MIN:
            skip["price"] += 1
            continue
        if stat["adv20"] <= config.EARN_ADV_MIN:
            skip["vol"] += 1
            continue
        d = days_to_event(edate, today) or 0
        cands.append({
            "ticker": sym, "name": str(r.get("companyName") or sym), "market_cap": cap,
            "earnings_date": edate, "price": stat["price"], "sma50": stat["sma50"],
            "adv": stat["adv20"], "buy_ratio": 0.0, "target_consensus": 0.0,
            "_pre": _earn_dday_points(d) + _earn_sma_points(stat["price"], stat["sma50"]),
        })

    cands.sort(key=lambda c: (-float(c["_pre"]), -float(c["adv"]) * float(c["price"])))
    pool = cands[: int(config.EARN_MAX_ANALYST_CALLS)]
    missing = _attach_analyst(pool)

    scored = []
    for c in pool:
        row = dict(c)
        row.update(earn_score_row(c, float(c["price"]), today))
        row.pop("_pre", None)
        scored.append(row)
    scored.sort(key=lambda r: (-float(r["score"]), str(r["ticker"])))
    top = scored[: int(config.EARN_TOP_N)]

    note = (
        f"캘린더 {len(cal)} → 필터 통과 {len(cands)} → 채점 {len(pool)} → Top {len(top)}"
        f" · 애널리스트 미수신 {missing}"
        f" · 제외 {skip['universe']}/{skip['hist']}/{skip['cap']}/{skip['price']}/{skip['vol']}"
        " (유니버스밖/일봉/시총/가격/거래량)"
    )
    slog("실적 런업 " + note)
    return top, note, cal


def earnings_guide(row: dict[str, Any], fx: float, budget_krw: float) -> dict[str, str]:
    tk = str(row.get("ticker") or "")
    px = float(row.get("price") or 0.0)
    d = row.get("d_day")
    d_txt = f"D-{int(d)}" if d is not None else "D-?"
    edate = str(row.get("earnings_date") or "")
    score = float(row.get("score") or 0.0)
    sig = str(row.get("signal") or "")
    tp_pct, sl_pct = float(config.EXIT_TP_PCT), float(config.EXIT_SL_PCT)
    tp, sl = px * (1 + tp_pct / 100.0), px * (1 - sl_pct / 100.0)
    fx_eff = fx if fx and fx > 500 else 1360.0
    qty = int(budget_krw // (px * fx_eff * 1.001)) if px > 0 else 0
    deadline = exit_deadline(edate)
    dl_txt = deadline.strftime("%m-%d") if deadline else "미상"
    won = f"{int(budget_krw) // 10000}만 원"

    if sig.startswith("🚫"):
        kind, title = "chase", f"🚫 [{tk} 진입 금지: 실적 임박 {d_txt}]"
        line1 = f"• 시세: 현재가 ${px:.2f} | 실적 {edate} ({d_txt}) | 점수 {score:.0f}"
        line2 = "• 행동: 신규 진입 금지. 보유 중이면 D-3 강제 탈출 규칙에 따라 정리."
    elif sig.startswith("🎯"):
        kind, title = "go", f"🎯 [{tk} 런업 사격: {d_txt} · {score:.0f}점]"
        line1 = f"• 시세: 현재가 ${px:.2f} | 실적 {edate} ({d_txt}) | {won} ≈ {qty}주 (환율 {fx_eff:,.0f})"
        line2 = (
            f"• 행동: 토스 [지정가 매수 ${px:.2f}] ➔ 체결 즉시 [조건주문: "
            f"+{tp_pct:g}% ${tp:.2f} 익절 / -{sl_pct:g}% ${sl:.2f} 손절] · {dl_txt} 정규장 전량 탈출"
        )
    else:
        kind, title = "wait", f"⏳ [{tk} 런업 관망: {d_txt} · {score:.0f}점]"
        line1 = f"• 시세: 현재가 ${px:.2f} | 실적 {edate} ({d_txt}) | {sig}"
        line2 = "• 행동: 진입 윈도우(D-12 ~ D-7, 75점 이상) 대기."

    html = (
        f'<div class="guide-title">{title}</div>'
        f'<div class="guide-line">{line1}</div><div class="guide-line">{line2}</div>'
    )
    return {"kind": kind, "html": html, "md": f"**{title}**\n\n{line1}\n\n{line2}"}


# =================================================================
# SHORT SQUEEZE (Tab 1 right) — yfinance shortPercentOfFloat. No FINRA.
# =================================================================

def _pct_from_yf_info(info: dict[str, Any]) -> tuple[float, float]:
    """shortPercentOfFloat is a fraction (0.15 = 15%). Fallback: sharesShort / floatShares.
    Second value is days-to-cover (shortRatio), display only."""
    pct = 0.0
    raw = info.get("shortPercentOfFloat")
    try:
        if raw is not None and raw != "":
            pct = float(raw) * 100.0
    except (TypeError, ValueError):
        pct = 0.0
    if pct <= 0:
        try:
            shares = float(info.get("sharesShort") or 0.0)
            float_shares = float(info.get("floatShares") or 0.0)
        except (TypeError, ValueError):
            shares, float_shares = 0.0, 0.0
        if shares > 0 and float_shares > 0:
            pct = shares / float_shares * 100.0
    dtc = 0.0
    try:
        dtc = float(info.get("shortRatio") or 0.0)
    except (TypeError, ValueError):
        dtc = 0.0
    if pct < 0:
        pct = 0.0
    if dtc < 0:
        dtc = 0.0
    return pct, dtc


def _get_short_float(sym: str) -> tuple[str, float, float]:
    """One Yahoo info pull. Returns (symbol, short float %, days-to-cover). 0 on any failure."""
    try:
        import yfinance as yf

        info = yf.Ticker(sym).info or {}
    except Exception:  # noqa: BLE001
        return sym, 0.0, 0.0
    if not isinstance(info, dict):
        return sym, 0.0, 0.0
    pct, dtc = _pct_from_yf_info(info)
    return sym, pct, dtc


def _fetch_short_float_live(tickers: list[str]) -> dict[str, dict[str, float]]:
    if not tickers:
        return {}
    workers = max(1, min(int(config.SQUEEZE_YF_WORKERS), len(tickers)))
    out: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for sym, pct, dtc in ex.map(_get_short_float, tickers):
            if pct > 0:
                out[sym] = {"short_float_pct": pct, "days_to_cover": dtc}
    return out


def _short_seed() -> dict[str, Any]:
    """Yahoo shortPercentOfFloat snapshot. Cloud datacenter IPs usually get nothing live."""
    global _SHORT_SEED
    if _SHORT_SEED is not None:
        return _SHORT_SEED
    _SHORT_SEED = {}
    if not _SHORT_SEED_PATH.exists():
        return _SHORT_SEED
    try:
        blob = json.loads(_SHORT_SEED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _SHORT_SEED
    if isinstance(blob, dict):
        _SHORT_SEED = blob
    return _SHORT_SEED


def _fill_short_gaps(tickers: list[str], live: dict[str, dict[str, float]]) -> str:
    """Fill names Yahoo missed. Returns the seed date when at least one row was used."""
    seed = _short_seed()
    symbols = seed.get("symbols") if isinstance(seed, dict) else None
    if not isinstance(symbols, dict):
        return ""
    took = 0
    for sym in tickers:
        if sym in live:
            continue
        raw = symbols.get(sym)
        if not isinstance(raw, (list, tuple)) or not raw:
            continue
        try:
            pct = float(raw[0])
            dtc = float(raw[1]) if len(raw) > 1 else 0.0
        except (TypeError, ValueError):
            continue
        if pct <= 0:
            continue
        live[sym] = {"short_float_pct": pct, "days_to_cover": dtc}
        took += 1
    if not took:
        return ""
    return str(seed.get("asof") or "")


def fetch_short_float_yf(tickers: list[str]) -> dict[str, dict[str, float]]:
    """Live Yahoo first. A dead probe uses the bundled seed instead of wiping the board."""
    global SHORT_SEED_NOTE
    SHORT_SEED_NOTE = ""
    if not tickers:
        return {}
    probe_n = min(8, len(tickers))
    live = _fetch_short_float_live(tickers[:probe_n])
    if live:
        if probe_n < len(tickers):
            live.update(_fetch_short_float_live(tickers[probe_n:]))
    elif probe_n < len(tickers):
        slog("Yahoo 공매도 probe 0건. 시드로 채운다.")
    asof = _fill_short_gaps(tickers, live)
    if asof:
        SHORT_SEED_NOTE = f"공매도 시드 {asof}"
    return live


def sqz_score_row(short_float_pct: float, rvol: float, upside_pct: float) -> dict[str, float]:
    """v3.2: 공매도40 + 거래량40 + 상승여력20. 상승여력 하드필터는 scan_squeeze() 에서 먼저 걸린다."""
    s_sf = max(0.0, min(40.0, 40.0 * short_float_pct / float(config.SQUEEZE_SHORTFLOAT_FULL)))
    s_rv = max(0.0, min(40.0, 40.0 * rvol / float(config.SQUEEZE_RVOL_FULL)))
    s_up = max(0.0, min(20.0, 20.0 * upside_pct / float(config.SQUEEZE_UPSIDE_FULL_PCT)))
    return {"s_shortfloat": s_sf, "s_rvol": s_rv, "s_upside": s_up,
            "score": round(s_sf + s_rv + s_up, 2)}


def sqz_fire_signal(score: float, rvol: float, ret_5d: float) -> str:
    if ret_5d >= float(config.SQUEEZE_CHASE_5D_PCT):
        return SQZ_CHASE
    if score >= float(config.SQUEEZE_FIRE_MIN_SCORE) and rvol >= 1.5:
        return SQZ_FIRE
    return SQZ_WAIT


def sqz_rank_live(
    rows: list[dict[str, Any]],
    quote_fn: Callable[[str], float] | None = None,
) -> list[dict[str, Any]]:
    """Overlay live price only; score/short-float/DTC are as of the last scan (not per-tick)."""
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        px = 0.0
        if quote_fn is not None:
            try:
                px = float(quote_fn(str(row.get("ticker") or "")) or 0.0)
            except (TypeError, ValueError):
                px = 0.0
        if px > 0:
            row["price"] = px
        out.append(row)
    out.sort(key=lambda r: (
        0 if r.get("signal") == SQZ_FIRE else (1 if r.get("signal") == SQZ_WAIT else 2),
        -float(r.get("score") or 0),
    ))
    for i, row in enumerate(out, start=1):
        row["rank"] = i
    return out


def _drop_partial_last_bar(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """Drop a stub last bar. FMP sometimes stores an intraday print as the completed
    session (AAPL 2026-09-28 cache 9.0M shares vs the real 32.7M close). A last
    volume under 40% of the prior 20-session median is that stub, not a quiet day
    we would trade as a squeeze anyway."""
    if df is None or getattr(df, "empty", True) or "Volume" not in df.columns:
        return df
    vol = df["Volume"].dropna()
    if len(vol) < 22 or len(df) < 22:
        return df
    last = float(vol.iloc[-1])
    base = float(vol.iloc[-21:-1].median())
    if base > 0 and last < 0.40 * base:
        return df.iloc[:-1]
    return df


def _vol50_ratio(df: pd.DataFrame | None) -> float:
    """Last session volume / mean of the prior 50 sessions. 0 when the series is too short."""
    if df is None or getattr(df, "empty", True) or "Volume" not in df.columns:
        return 0.0
    vol = df["Volume"].dropna()
    if len(vol) < 51:
        return 0.0
    base = float(vol.iloc[-51:-1].mean())
    if base <= 0:
        return 0.0
    return float(vol.iloc[-1]) / base


def scan_squeeze(
    universe: list[dict[str, Any]],
    hist: dict[str, pd.DataFrame],
) -> tuple[list[dict[str, Any]], str]:
    """1차: 주가·시총·50일 거래량 1.5배. 2차: 상위 100종 yfinance 공매도.
    3차: 숏비율 15%+ 에만 FMP 컨센서스. 상승여력 5% 미만은 후보에서 제외.
    점수 = 공매도 40 + 거래량 40 + 상승여력 20. 숏커버일수는 표시만."""
    meta = {str(r.get("symbol") or "").upper(): r for r in universe if r.get("symbol")}
    skip = {"cap": 0, "price": 0, "vol": 0, "hist": 0}
    spikes: list[dict[str, Any]] = []
    rvol_min = float(config.SQUEEZE_RVOL_MIN)
    for sym, r in meta.items():
        try:
            cap = float(r.get("marketCap") or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap < float(config.SQUEEZE_CAP_MIN):
            skip["cap"] += 1
            continue
        df = _drop_partial_last_bar(hist.get(sym))
        stat = _hist_stats(df)
        if stat is None:
            skip["hist"] += 1
            continue
        if stat["price"] < float(config.SQUEEZE_PRICE_MIN):
            skip["price"] += 1
            continue
        rvol = _vol50_ratio(df)
        if rvol < rvol_min:
            skip["vol"] += 1
            continue
        spikes.append({
            "ticker": sym,
            "name": str(r.get("companyName") or sym),
            "market_cap": cap,
            "price": stat["price"],
            "sma10": stat["sma10"],
            "sma50": stat["sma50"],
            "adv": stat["adv20"],
            "ret_5d": stat["ret_5d"],
            "rvol": rvol,
        })

    spikes.sort(key=lambda c: -float(c["rvol"]))
    pool = spikes[: int(config.SQUEEZE_SI_MAX_CALLS)]
    quotes = fetch_short_float_yf([c["ticker"] for c in pool])

    floor = float(config.SQUEEZE_SHORT_FLOAT_MIN)
    upside_min = float(config.SQUEEZE_UPSIDE_MIN_PCT)
    passed_shortfloat: list[dict[str, Any]] = []
    for c in pool:
        got = quotes.get(c["ticker"])
        if not got:
            continue
        short_float_pct = float(got["short_float_pct"])
        if short_float_pct < floor:
            continue
        row = dict(c)
        row["short_float_pct"] = round(short_float_pct, 1)
        row["rvol"] = round(float(c["rvol"]), 2)
        row["days_to_cover"] = round(float(got.get("days_to_cover") or 0.0), 1)
        passed_shortfloat.append(row)

    analyst_pool = passed_shortfloat[: int(config.SQUEEZE_MAX_ANALYST_CALLS)]
    _attach_analyst(analyst_pool)

    scored: list[dict[str, Any]] = []
    skip_upside = 0
    for row in analyst_pool:
        price = float(row.get("price") or 0.0)
        target = float(row.get("target_consensus") or 0.0)
        upside = ((target - price) / price * 100.0) if price > 0 and target > 0 else 0.0
        if upside < upside_min:
            skip_upside += 1
            continue
        parts = sqz_score_row(float(row["short_float_pct"]), float(row["rvol"]), upside)
        row.update(parts)
        row["upside"] = round(upside, 1)
        row["target_consensus"] = target
        row["signal"] = sqz_fire_signal(parts["score"], float(row["rvol"]), float(row.get("ret_5d") or 0.0))
        scored.append(row)

    scored.sort(key=lambda r: -float(r["score"]))
    top = scored[: int(config.SQUEEZE_TOP_N)]
    for i, row in enumerate(top, start=1):
        row["rank"] = i

    seed_bit = f" · {SHORT_SEED_NOTE}" if SHORT_SEED_NOTE else ""
    if not quotes:
        note = (
            f"yfinance 공매도 0건 수신. 거래량 폭증 후보 {len(spikes)}종 중 {len(pool)}종 조회. "
            "비율이 없으면 목록을 비운다."
            + seed_bit
        )
    else:
        note = (
            f"1차 폭증 {len(spikes)} → 조회 {len(pool)} (공매도 수신 {len(quotes)}) → "
            f"숏비율≥{floor:.0f}% 통과 {len(passed_shortfloat)} → "
            f"상승여력≥{upside_min:.0f}% 통과 {len(scored)} → Top {len(top)}"
            f" · 제외 {skip['cap']}/{skip['price']}/{skip['vol']}/{skip['hist']}/{skip_upside}"
            " (시총/가격/거래량/일봉/상승여력부족)"
            + seed_bit
        )
    slog("숏스퀴즈 " + note)
    return top, note


def squeeze_guide(row: dict[str, Any], fx: float, budget_krw: float) -> dict[str, str]:
    tk = str(row.get("ticker") or "")
    px = float(row.get("price") or 0.0)
    sf = float(row.get("short_float_pct") or 0.0)
    rvol = float(row.get("rvol") or 0.0)
    dtc = float(row.get("days_to_cover") or 0.0)
    score = float(row.get("score") or 0.0)
    sig = str(row.get("signal") or "")
    tp_pct, sl_pct = float(config.EXIT_TP_PCT), float(config.EXIT_SL_PCT)
    tp, sl = px * (1 + tp_pct / 100.0), px * (1 - sl_pct / 100.0)
    fx_eff = fx if fx and fx > 500 else 1360.0
    qty = int(budget_krw // (px * fx_eff * 1.001)) if px > 0 else 0
    won = f"{int(budget_krw) // 10000}만 원"

    if sig.startswith("🚫"):
        kind, title = "chase", f"🚫 [{tk} 추격 금지: 이미 급등]"
        line1 = f"• 시세: 현재가 ${px:.2f} | 최근5일 {float(row.get('ret_5d') or 0):+.1f}% | 숏비율 {sf:.1f}%"
        line2 = "• 행동: 신규 진입 금지. 추격 매수는 스퀴즈 마지막 국면일 위험이 크다."
    elif sig.startswith("🎯"):
        kind, title = "go", f"🎯 [{tk} 스퀴즈 사격: {score:.0f}점]"
        line1 = f"• 지표: 숏비율 {sf:.1f}% | 거래량 {rvol:.1f}배 | 숏커버일수 {dtc:.1f}일 | 상승여력 {float(row.get('upside') or 0):+.1f}%"
        line2 = (
            f"• 행동: {won} ≈ {qty}주 토스 [지정가 매수 ${px:.2f}] ➔ 체결 즉시 "
            f"[조건주문: +{tp_pct:g}% ${tp:.2f} 익절 / -{sl_pct:g}% ${sl:.2f} 손절]"
        )
    else:
        kind, title = "wait", f"⏳ [{tk} 스퀴즈 관망: {score:.0f}점]"
        line1 = f"• 지표: 숏비율 {sf:.1f}% | 거래량 {rvol:.1f}배 | 숏커버일수 {dtc:.1f}일 | 상승여력 {float(row.get('upside') or 0):+.1f}%"
        line2 = "• 행동: 점수 70 미만. 숏비율이나 거래량 배수가 더 붙기 전에는 관망."

    html = (
        f'<div class="guide-title">{title}</div>'
        f'<div class="guide-line">{line1}</div><div class="guide-line">{line2}</div>'
    )
    return {"kind": kind, "html": html, "md": f"**{title}**\n\n{line1}\n\n{line2}"}
