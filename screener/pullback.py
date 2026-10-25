"""눌림 반등 엔진 (v4.5): 상승 추세 종목의 2일 과매도 눌림을 하루 1개 골라 1~5일 보유한다.

백테스트(2024-07 ~ 2026-10, 2,500종목, backtest/backtest_pullback.py): 최근 1년 검증 구간에서
하루 1개 기준 적중률 62~63%, 수수료 0.2% 뺀 평균 +0.7~0.9%, 평균 보유 3.3일. 하위 5% 거래는 -12% 이하.

  신호   t일 종가 기준: 주가 ≥ 10, 20일 평균 거래대금 ≥ 2천만 달러, 종가 > 200일선, 50일선 > 200일선, RSI(2) < 10
  선별   같은 날 후보 중 60일 상승률 1위 (7일 안에 추천한 종목은 건너뜀)
  진입   t+1 본장 시가
  청산   종가가 5일선 위로 마감한 날 종가, 아니면 보유 5거래일째 종가. 손절 없음 → 소액으로 위험 관리.

EP 엔진(episodic_pivot)과 독립이다. Streamlit 에 의존하지 않는다.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import pickle
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import fmp_client as fc
from .episodic_pivot import ET, KST, _dump, _load, market_state, prev_session

SETUP = "PB"
LABEL = "🔁 눌림 반등"
_ROOT = os.path.dirname(os.path.dirname(__file__))
DEFAULT_POOL_PATH = os.path.join(_ROOT, ".cache", "pullback_pool.json")
SEED_PATH = os.path.join(_ROOT, "data", "seed", "eod_seed.pkl.gz")


@dataclass
class PbCfg:
    price_min: float = 10.0
    dollar_vol_min: float = 20_000_000.0   # 20일 평균 거래대금
    rsi_max: float = 10.0                   # RSI(2)
    top_n: int = 1                          # 하루 1개가 2개보다 검증 성적이 좋았다
    max_hold: int = 5                       # 최대 보유 거래일 (진입일 포함)
    no_repeat_days: int = 7                 # 같은 종목 재추천 금지 (달력일)
    fee_pct: float = 0.2                    # 왕복 수수료
    tail_loss_pct: float = 12.0             # 백테스트 하위 5% 손실. 권장 금액 = 1회 리스크 ÷ 이 값
    seed_trend_tol: float = 0.97            # 시드 1차 컷: 종가 > 200일 평균 × 0.97


def rsi2(close: pd.Series) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def evaluate(sym: str, bars: pd.DataFrame, cfg: PbCfg | None = None):
    """완결 일봉 마지막 날 기준 신호. (후보 dict | None, 사유)."""
    cfg = cfg or PbCfg()
    if bars is None or len(bars) < 201:
        return None, "일봉 부족(200일)"
    c = bars["close"].astype(float)
    last = float(c.iloc[-1])
    if last < cfg.price_min:
        return None, "주가 하한 미달"
    dv = float((c * bars["volume"].astype(float)).tail(20).mean())
    if dv < cfg.dollar_vol_min:
        return None, "거래대금 부족"
    s50, s200 = float(c.tail(50).mean()), float(c.tail(200).mean())
    if not (last > s200 and s50 > s200):
        return None, "상승 추세 아님(200일선)"
    r = float(rsi2(c).iloc[-1])
    if not r < cfg.rsi_max:
        return None, "과매도 아님(RSI2)"
    r60 = (last / float(c.iloc[-61]) - 1.0) * 100.0 if len(c) > 61 else None
    if r60 is None:
        return None, "일봉 부족(60일)"
    return {"symbol": sym, "setup": SETUP, "setup_label": LABEL,
            "signal_date": bars["date"].iloc[-1].date().isoformat(), "close": last, "price": last,
            "rsi2": r, "r60": r60, "sma5": float(c.tail(5).mean()), "sma50": s50, "sma200": s200,
            "dollar_vol": dv, "drop_2d": (last / float(c.iloc[-3]) - 1.0) * 100.0}, "OK"


def pick(cands: list, cfg: PbCfg | None = None, recent: dict | None = None, held: set | None = None) -> list:
    """60일 상승률 순으로 top_n. recent: {심볼: 마지막 추천 신호일 iso} (재추천 금지). held: 보유 종목 (건너뜀, v4.6.1)."""
    cfg = cfg or PbCfg()
    recent = recent or {}
    out = []
    for c in sorted(cands, key=lambda x: -x["r60"]):
        if c["symbol"] in (held or ()):
            continue
        last = recent.get(c["symbol"])
        if last:
            gap = (dt.date.fromisoformat(c["signal_date"]) - dt.date.fromisoformat(last)).days
            if 0 < gap < cfg.no_repeat_days:
                continue
        out.append(c)
        if len(out) >= cfg.top_n:
            break
    return out


def passed_list(pool: dict, held: set | None = None, recent: dict | None = None,
                cfg: PbCfg | None = None) -> list:
    """합격 종목 전부 60일 상승률 순 + 표시 (v4.6.1 사령관 허가: 직접 고를 수 있게). 추천은 ⭐."""
    cfg = cfg or PbCfg()
    held, recent = held or set(), recent or {}
    top = {c["symbol"] for c in pool.get("top") or []}
    out = []
    for i, c in enumerate(pool.get("others") or pool.get("top") or [], 1):
        last = recent.get(c["symbol"])
        if c["symbol"] in top:
            tag = "⭐ 추천"
        elif c["symbol"] in held:
            tag = "🛡️ 보유 중"
        elif last and 0 < (dt.date.fromisoformat(c["signal_date"]) - dt.date.fromisoformat(last)).days < cfg.no_repeat_days:
            tag = "최근 추천"
        else:
            tag = ""
        out.append(dict(c, rank=i, tag=tag))
    return out


def seed_universe(cfg: PbCfg | None = None, path: str = SEED_PATH) -> list:
    """시드 일봉(종가·거래량)으로 1차 컷: 거래대금·주가·장기 추세. 정확한 판정은 최신 일봉으로 다시 한다."""
    cfg = cfg or PbCfg()
    try:
        seed = pickle.load(gzip.open(path))
    except Exception:
        return []
    out = []
    for sym, row in (seed.get("symbols") or {}).items():
        try:
            _dates, closes, vols = row
            c, v = np.asarray(closes, float), np.asarray(vols, float)
        except Exception:
            continue
        if not sym or "." in sym or len(c) < 200:
            continue
        if c[-1] < cfg.price_min or (c[-20:] * v[-20:]).mean() < cfg.dollar_vol_min:
            continue
        if c[-1] < c[-200:].mean() * cfg.seed_trend_tol:
            continue
        out.append(sym)
    return sorted(out)


EOD_READY_HOUR = 18  # FMP 일봉이 장 마감 후 갱신되기까지 여유 (ET)


def signal_session(now: dt.datetime | None = None) -> dt.date:
    """신호를 낼 완결 세션: 세션일 18:00 ET(일봉 갱신 여유) 이후면 그날, 아니면 직전 거래일."""
    from . import radar
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    if radar.is_session_day(now.date()) and now.hour >= EOD_READY_HOUR:
        return now.date()
    return prev_session(now.date())


def entry_session(sig: dt.date) -> dt.date:
    from . import radar
    d = sig + dt.timedelta(days=1)
    while not radar.is_session_day(d):
        d += dt.timedelta(days=1)
    return d


def scan(api_key: str, cfg: PbCfg | None = None, symbols: list | None = None, recent: dict | None = None,
         progress=None, now: dt.datetime | None = None, pool_path: str = DEFAULT_POOL_PATH,
         workers: int = 8, fetch=None, held: set | None = None) -> dict:
    cfg = cfg or PbCfg()
    now_et = (now or dt.datetime.now(ET)).astimezone(ET)
    sig = signal_session(now_et)
    uni = symbols if symbols is not None else seed_universe(cfg)
    fetch = fetch or (lambda s: fc.fetch_eod(api_key, s, 260))
    cands, rejects, fails, done = [], {}, 0, 0

    def one(sym):
        try:
            b = fetch(sym)
        except Exception:
            b = None
        if b is None or not len(b):
            return sym, None, "일봉 없음"
        b = b[b["date"].dt.date <= sig].reset_index(drop=True)
        if not len(b) or b["date"].iloc[-1].date() != sig:
            return sym, None, "신호일 일봉 없음"
        return (sym, *evaluate(sym, b, cfg))

    with ThreadPoolExecutor(workers) as ex:
        for sym, c, why in ex.map(one, uni):
            done += 1
            if progress and done % 25 == 0:
                progress(done, len(uni), sym)
            if c:
                cands.append(c)
            else:
                rejects[why] = rejects.get(why, 0) + 1
                fails += why in ("일봉 없음", "신호일 일봉 없음")
    top = pick(cands, cfg, recent, held)
    ent = entry_session(sig)
    state, _ = market_state(now_et)
    pool = {"session": sig.isoformat(), "entry_session": ent.isoformat(), "scanned": len(uni),
            "scanned_at_kst": now_et.astimezone(KST).strftime("%Y-%m-%d %H:%M"), "state": state,
            "n_cands": len(cands), "top": top,
            "others": sorted(cands, key=lambda x: -x["r60"]),   # v4.6.1: 합격 종목 전부 "rejects": rejects,
            "data_fail": fails, "fail_rate": fails / max(len(uni), 1)}
    try:
        _dump(pool, pool_path)
    except Exception:
        pass
    return pool


def load_pool(path: str = DEFAULT_POOL_PATH):
    return _load(path)


def pool_is_current(pool: dict | None, now: dt.datetime | None = None) -> bool:
    return bool(pool) and pool.get("session") == signal_session(now).isoformat()


def position_usd(equity: float, risk_pct: float, cfg: PbCfg | None = None) -> float:
    """손절이 없으므로 '하위 5% 손실(-12%)이 나도 1회 리스크 이내'가 되는 금액."""
    cfg = cfg or PbCfg()
    return equity * risk_pct / 100.0 / (cfg.tail_loss_pct / 100.0)


def grade(bars: pd.DataFrame, signal_date: str, cfg: PbCfg | None = None):
    """신호일 이후 일봉으로 실제 거래 결과. 아직 끝나지 않았으면 None.
    {entry, exit, exit_date, hold, ret_close_pct(수수료 후), max_up_pct, max_dn_pct, win}."""
    cfg = cfg or PbCfg()
    if bars is None or not len(bars):
        return None
    b = bars.reset_index(drop=True)
    sd = dt.date.fromisoformat(signal_date)
    idx = b.index[b["date"].dt.date == sd]
    if not len(idx):
        return None
    i = int(idx[0])
    s5 = b["close"].rolling(5).mean()
    if i + 1 >= len(b):
        return None
    entry = float(b["open"].iloc[i + 1])
    if entry <= 0:
        return None
    for k in range(cfg.max_hold):
        j = i + 1 + k
        if j >= len(b):
            return None
        cl = float(b["close"].iloc[j])
        if cl > float(s5.iloc[j]) or k == cfg.max_hold - 1:
            seg = b.iloc[i + 1:j + 1]
            ret = (cl / entry - 1.0) * 100.0 - cfg.fee_pct
            return {"entry": entry, "exit": cl, "exit_date": b["date"].iloc[j].date().isoformat(),
                    "hold": k + 1, "ret_close_pct": round(ret, 2),
                    "max_up_pct": round((float(seg["high"].max()) / entry - 1) * 100, 2),
                    "max_dn_pct": round((float(seg["low"].min()) / entry - 1) * 100, 2),
                    "win": ret > 0}
    return None
