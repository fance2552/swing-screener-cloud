"""Qullamaggie 2대 셋업 단일 엔진 (v4.2: 당일 상승 확률 중심 진입 판정).

  셋업 A  당일 Episodic Pivot (EP)
  셋업 B  EP 후 2~4일 얕은 눌림목 (Flag Pullback)

기존 pead.py / rsi2.py / 런업 / 숏스퀴즈 / 위원회 코드를 전부 대체한다.
Streamlit 에 의존하지 않는다 (스레드풀 안전).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from . import fmp_client as fc

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

SETUP_A = "A"
SETUP_B = "B"
SETUP_LABEL = {SETUP_A: "🔥 당일 Episodic Pivot", SETUP_B: "🎯 EP 눌림목(Flag Pullback)"}

DEFAULT_POOL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache", "scan_pool.json")
DEFAULT_HISTORY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".cache", "ep_history.json")


@dataclass
class Cfg:
    # --- 셋업 A ---
    gap_min_pct: float = 8.0
    vol_mult_min: float = 3.0
    price_min: float = 10.0
    mcap_min: float = 1_000_000_000.0
    avg_vol_min: float = 300_000.0          # 유동성 하한 (v4 추가 안전장치)
    eps_beat_min_pct: float = 5.0
    prefilter_chg_pct: float = 4.0          # 후보 풀 1차 컷 (API 호출 절감)
    # --- 셋업 B ---
    pb_min_days_since: int = 3              # EP 발생 후 경과 세션 (3~5)
    pb_max_days_since: int = 5
    pb_dry_mean_max: float = 0.50           # 눌림 구간 평균 거래량 <= 50%
    pb_dry_max_max: float = 0.65            # 눌림 구간 최대 거래량 <= 65%
    pb_max_depth_pct: float = 10.0          # EP 후 고점 대비 되돌림 한도
    pb_max_stop_pct: float = 3.5
    # --- 주문 계획 ---
    stop_min_pct: float = 2.5
    stop_max_pct: float = 3.5
    t1_min_pct: float = 5.0
    t1_max_pct: float = 7.0
    min_rr: float = 2.0
    chase_cap_pct: float = 0.5              # 진입 상단 = 현재가 +0.5%
    live_min_minutes: int = 30              # 장중 거래량 추정 최소 경과 분 (10분은 과대 추정)
    live_raw_vol_min: float = 1.0           # 장중 원 거래량 하한 (50일 평균 대비 배수). 원전: 개장 15~20분에 일평균 소화
    hold_range_min: float = 0.5             # 셋업 A: 현재가가 당일 고저 범위 상위 50% 안 (갭 버팀)
    run_lookback: int = 60                  # 셋업 A: 과열 판정용 직전 세션 수
    run_full_pct: float = 15.0              # 직전 상승률이 이 이하면 베이스 점수 만점
    run_zero_pct: float = 60.0              # 이 이상이면 베이스 점수 0 + 과열 경고
    prev_close_tol_pct: float = 1.0         # quote 전일종가 vs 일봉 전일종가 허용 오차
    min_score: float = 50.0
    top_n: int = 3


# ----------------------------------------------------------------------------
# 시간 / 세션
# ----------------------------------------------------------------------------
def market_state(now: dt.datetime | None = None):
    """('LIVE', 개장후 분) | ('PRE', None) | ('CLOSED', None). 주말과 NYSE 휴장은 CLOSED."""
    from . import radar
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    if not radar.is_session_day(now.date()):
        return "CLOSED", None
    o = now.replace(hour=9, minute=30, second=0, microsecond=0)
    c = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < o:
        return "PRE", None
    if radar.is_market_open(now):
        return "LIVE", int((now - o).total_seconds() // 60)
    return "CLOSED", None


def session_date(now: dt.datetime | None = None) -> dt.date:
    """스캔 기준 세션일: 거래일 개장(ET 09:30) 이후면 오늘, 아니면 직전 거래일."""
    from . import radar
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    d = now.date()
    o = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if radar.is_session_day(d) and now >= o:
        return d
    d -= dt.timedelta(days=1)
    while not radar.is_session_day(d):
        d -= dt.timedelta(days=1)
    return d


def prev_session(d: dt.date) -> dt.date:
    """d 직전 NYSE 거래일."""
    from . import radar
    d -= dt.timedelta(days=1)
    while not radar.is_session_day(d):
        d -= dt.timedelta(days=1)
    return d


def recommended_entry_kst(now: dt.datetime | None = None) -> dt.datetime:
    """ET 09:45 (개장 15분 후) 를 KST 로 환산 (서머타임 자동 반영)."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    t = dt.datetime.combine(now.date(), dt.time(9, 45), ET)
    return t.astimezone(KST)


# 갭 당일은 거래량이 장 초반에 몰린다. 평일 U자보다 앞쪽을 두껍게 잡아 추정 배수를 보수적으로 만든다.
_PROFILE = [(0, 0.0), (15, 0.15), (30, 0.25), (60, 0.36), (120, 0.52), (180, 0.63),
            (240, 0.72), (300, 0.81), (360, 0.91), (390, 1.0)]


def expected_fraction(minutes: int) -> float:
    """개장 후 minutes 분 시점의 '일중 누적 거래량 비율' 근사 (U자 프로파일)."""
    m = max(0, min(390, int(minutes)))
    for (m0, f0), (m1, f1) in zip(_PROFILE, _PROFILE[1:]):
        if m0 <= m <= m1:
            return f0 + (f1 - f0) * (m - m0) / (m1 - m0)
    return 1.0


# ----------------------------------------------------------------------------
# 바 데이터 유틸
# ----------------------------------------------------------------------------
def attach_live_bar(bars: pd.DataFrame, quote: dict | None, sess: dt.date, state: str) -> pd.DataFrame:
    """quote 로 '세션일 바'를 보강한다 (장중이거나 EOD 가 아직 갱신 전일 때)."""
    if bars is None or quote is None:
        return bars
    keys = ("open", "high", "low", "price", "volume")
    if any(quote.get(k) in (None, 0) for k in keys):
        return bars
    last_date = bars["date"].iloc[-1].date()
    if last_date < sess and last_date != prev_session(sess):
        return bars  # 전일 일봉 결측: 붙이면 이틀 전 종가 대비 갭이 된다 → 세션일 바 없음으로 탈락
    row = {"date": pd.Timestamp(sess), "open": quote["open"], "high": quote["high"],
           "low": quote["low"], "close": quote["price"], "volume": quote["volume"]}
    if last_date < sess:
        return pd.concat([bars, pd.DataFrame([row])], ignore_index=True)
    if last_date == sess and state == "LIVE":
        out = bars.copy()
        for k, v in row.items():
            out.iloc[-1, out.columns.get_loc(k)] = v
        return out
    return bars


def gap_vol(bars: pd.DataFrame, idx: int):
    """idx 번째 바의 (갭%, 거래량배수, 직전50일 평균거래량). 부족하면 None."""
    if idx < 30 or idx >= len(bars):
        return None
    prev_close = float(bars["close"].iloc[idx - 1])
    op = float(bars["open"].iloc[idx])
    if prev_close <= 0:
        return None
    base = float(bars["volume"].iloc[max(0, idx - 50):idx].mean())
    if base <= 0:
        return None
    gap = (op / prev_close - 1.0) * 100.0
    volx = float(bars["volume"].iloc[idx]) / base
    return gap, volx, base


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _parse_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 촉매 (실적 서프라이즈 / 팩트 뉴스)
# ----------------------------------------------------------------------------
# 주가 움직임을 사후에 설명하는 반응 기사·시세 요약은 촉매가 아니다.
_NEWS_REACTION = (
    "shares are", "stock is", "stocks moving", "why ", "soar", "jump", "surge", "rall", "spike",
    "plunge", "trading higher", "trading lower", "moving higher", "moving lower", "top gainers",
    "top losers", "premarket movers", "pre-market movers", "mid-day movers", "midday movers",
    "big movers", "unusual options", "gap up", "gaps up", "shares up", "shares down", "stock up",
    "stock down", "52-week high", "new high", "hits high", "price target", "upgrade", "downgrade",
    "reiterate", "maintains", "initiates coverage",
)


def _parse_dt(s):
    """'YYYY-MM-DD HH:MM:SS' → (date, time|None). FMP 뉴스 시각은 ET 기준."""
    d = _parse_date(s)
    t = None
    try:
        txt = str(s)
        if len(txt) >= 16:
            t = dt.time.fromisoformat(txt[11:16])
    except Exception:
        t = None
    return d, t


def news_is_catalyst(title: str) -> bool:
    low = str(title or "").lower()
    return bool(low.strip()) and not any(k in low for k in _NEWS_REACTION)


# 실적 발표 보도자료 제목 (실적 엔드포인트가 막혔을 때 대체 확인용).
_EARNINGS_PR = ("results", "earnings")
_EARNINGS_PR_PERIOD = ("quarter", "fiscal", "q1", "q2", "q3", "q4", "first", "second", "third", "fourth",
                       "full year", "full-year", "annual")


def is_earnings_release(title: str) -> bool:
    low = str(title or "").lower()
    return any(k in low for k in _EARNINGS_PR) and any(k in low for k in _EARNINGS_PR_PERIOD)


def _pre_open_window(d, t, ref: dt.date, prev: dt.date) -> bool:
    """직전 거래일 16:00 ET 이후 ~ ref 09:30 ET 이전 게시인가. 시각이 없으면 날짜만 본다."""
    if not d or not (prev <= d <= ref):
        return False
    if d == ref and t is not None and t >= dt.time(9, 30):
        return False  # 개장 이후 기사 = 갭에 대한 반응
    if d == prev and t is not None and t < dt.time(16, 0):
        return False  # 전일 장중 기사는 전일 가격에 이미 반영
    return True


def catalyst_check(api_key: str, sym: str, ref: dt.date, cfg: Cfg, fails: dict | None = None):
    """(촉매|None, 조회 실패 여부). ref 세션일 갭의 원인을 아래 순서로 찾는다.

    1. 실적: ref 당일(장전) 또는 직전 거래일(장후) 발표, EPS 비트 ≥ 기준 그리고 매출 ≥ 컨센서스.
    2. 실적 보도자료: 실적 엔드포인트가 막혔을 때만. 비트 폭은 확인 못 하므로 뉴스급으로 본다.
    3. 회사 보도자료 / 종목 뉴스: 직전 거래일 16:00 ET ~ ref 09:30 ET 게시, 반응 기사 제외.
    4. 애널리스트 등급 상향: ref 당일 발표.
    조회 실패 여부는 네 출처가 전부 실패했을 때만 True (촉매가 없는 것과 구분)."""
    prev = prev_session(ref)
    failed = []

    def get(path, params, name):
        data = fc.fmp_get(path, api_key, params)
        if not isinstance(data, list):
            failed.append(name)
            if fails is not None:
                fails[name] = fails.get(name, 0) + 1
            return []
        return data

    rows = get("earnings", {"symbol": sym, "limit": 6}, "earnings")
    earnings_ok = "earnings" not in failed
    for r in rows:
        d = _parse_date(r.get("date"))
        if not d or d not in (ref, prev):
            continue
        ea, ee = fc._f(r.get("epsActual")), fc._f(r.get("epsEstimated"))
        ra, re_ = fc._f(r.get("revenueActual")), fc._f(r.get("revenueEstimated"))
        if ea is None or ee is None:
            continue
        beat = (100.0 if ea > 0 else 0.0) if ee == 0 else (ea - ee) / abs(ee) * 100.0
        rev_ok = ra is not None and re_ is not None and ra >= re_
        if beat >= cfg.eps_beat_min_pct and rev_ok:
            return {"type": "EARNINGS", "eps_beat_pct": round(beat, 1),
                    "rev_beat_pct": round((ra / re_ - 1) * 100, 1) if re_ else None,
                    "date": d.isoformat(),
                    "text": f"실적 서프라이즈 EPS +{beat:.1f}% / 매출 컨센서스 상회 ({d.isoformat()})"}, False

    press = get("news/press-releases", {"symbols": sym, "limit": 12}, "press")
    for n in press:
        d, t = _parse_dt(n.get("publishedDate") or n.get("date"))
        title = str(n.get("title") or "")
        if not _pre_open_window(d, t, ref, prev) or not news_is_catalyst(title):
            continue
        if is_earnings_release(title):
            if earnings_ok:
                continue  # 실적 수치를 확인했는데 기준 미달이면 보도자료로 우회하지 않는다
            return {"type": "EARNINGS_PR", "date": d.isoformat(), "title": title[:140],
                    "text": f"실적 발표(보도자료, 비트 폭 미확인): {title[:100]} ({d.isoformat()})"}, False
        return {"type": "NEWS", "source": "PRESS", "date": d.isoformat(), "title": title[:140],
                "text": f"회사 발표: {title[:110]} ({d.isoformat()})"}, False

    news = get("news/stock", {"symbols": sym, "limit": 12}, "news")
    for n in news:
        d, t = _parse_dt(n.get("publishedDate"))
        if not _pre_open_window(d, t, ref, prev) or not news_is_catalyst(n.get("title")):
            continue
        if earnings_ok and is_earnings_release(n.get("title")):
            continue  # 실적 기사는 실적 수치로 판정한다
        return {"type": "NEWS", "date": d.isoformat(), "title": str(n.get("title", ""))[:140],
                "text": f"뉴스 촉매: {str(n.get('title', ''))[:110]} ({d.isoformat()})"}, False

    grades = get("grades", {"symbol": sym, "limit": 10}, "grades")
    for g in grades:
        d = _parse_date(g.get("date"))
        if d != ref or str(g.get("action") or "").lower() != "upgrade":
            continue
        firm = str(g.get("gradingCompany") or "애널리스트")
        old, new = str(g.get("previousGrade") or "?"), str(g.get("newGrade") or "?")
        return {"type": "UPGRADE", "date": d.isoformat(), "firm": firm,
                "text": f"등급 상향: {firm[:40]} {old[:20]} → {new[:20]} ({d.isoformat()})"}, False
    return None, len(failed) == 4


def catalyst(api_key: str, sym: str, ref: dt.date, cfg: Cfg):
    """ref 세션일 갭의 원인 (catalyst_check 의 촉매만)."""
    return catalyst_check(api_key, sym, ref, cfg)[0]


# ----------------------------------------------------------------------------
# 주문 계획 (가격은 전부 파이썬이 계산 — LLM 은 숫자를 만들지 않는다)
# ----------------------------------------------------------------------------
def make_plan(price: float, struct_low: float, sma10: float | None, cfg: Cfg) -> dict:
    entry_lo = price
    entry_hi = price * (1 + cfg.chase_cap_pct / 100.0)
    if struct_low and struct_low >= entry_lo:
        struct_low = None  # 구조 저점이 현재가 위: 구조 손절 무효 (호출부에서 이미 탈락시켜야 함)
    raw = (entry_hi - struct_low) / entry_hi * 100.0 if struct_low and struct_low > 0 else cfg.stop_max_pct
    stop_pct = _clamp(raw + 0.3, cfg.stop_min_pct, cfg.stop_max_pct)
    t1_pct = _clamp(2.0 * stop_pct, cfg.t1_min_pct, cfg.t1_max_pct)
    stop = entry_hi * (1 - stop_pct / 100.0)
    t1 = entry_hi * (1 + t1_pct / 100.0)
    rr = t1_pct / stop_pct
    rr_net = (t1_pct - 0.2) / (stop_pct + 0.2)
    return {"entry_lo": entry_lo, "entry_hi": entry_hi, "stop": stop, "stop_pct": stop_pct,
            "t1": t1, "t1_pct": t1_pct, "t2_ref": sma10, "rr": rr, "rr_net": rr_net,
            "struct_stop_dist_pct": raw, "lod_cap_exceeded": raw > cfg.stop_max_pct,
            "rr_ok": rr + 1e-9 >= cfg.min_rr}


# ----------------------------------------------------------------------------
# 셋업 A 평가
# ----------------------------------------------------------------------------
def evaluate_a(sym, quote, bars, cfg: Cfg, sess: dt.date, state: str, minutes):
    if bars is None or len(bars) < 35:
        return None, "일봉 데이터 부족"
    if bars["date"].iloc[-1].date() != sess:
        return None, "세션일 바 없음"
    gv = gap_vol(bars, len(bars) - 1)
    if gv is None:
        return None, "갭/거래량 계산 불가"
    gap, volx_raw, base = gv
    volx, projected = volx_raw, False
    if state == "LIVE":
        if minutes is None or minutes < cfg.live_min_minutes:
            return None, f"개장 {cfg.live_min_minutes}분 미만(거래량 추정 불가)"
        if volx_raw < cfg.live_raw_vol_min:
            return None, f"거래량 {volx_raw:.1f}배(원) < {cfg.live_raw_vol_min}배"
        volx, projected = volx_raw / max(expected_fraction(minutes), 0.05), True
    price = float(bars["close"].iloc[-1])
    prev_close = float(bars["close"].iloc[-2])
    q_prev = quote.get("prev_close") if quote else None
    if q_prev and prev_close > 0 and abs(q_prev / prev_close - 1.0) * 100.0 > cfg.prev_close_tol_pct:
        return None, "전일 종가 불일치(일봉 결측·분할 의심)"
    mcap = quote.get("market_cap") if quote else None
    if gap < cfg.gap_min_pct:
        return None, f"갭 {gap:.1f}% < {cfg.gap_min_pct:.0f}%"
    if volx < cfg.vol_mult_min:
        return None, f"거래량 {volx:.1f}배 < {cfg.vol_mult_min:.0f}배"
    if price < cfg.price_min:
        return None, "주가 하한 미달"
    if not mcap or mcap < cfg.mcap_min:
        return None, "시총 하한 미달"
    if base < cfg.avg_vol_min:
        return None, "평균거래량 부족"
    if price <= prev_close:
        return None, "갭 메워짐(전일 종가 이하)"
    # 갭 당일은 약 3분의 2가 음봉으로 끝난다. 시가 위에서, 당일 범위 위쪽에서 버티는 갭만 남긴다.
    if price < float(bars["open"].iloc[-1]):
        return None, "시가 이탈(갭 이후 매도 우위)"
    day_low = float(bars["low"].iloc[-1])
    day_high = float(bars["high"].iloc[-1])
    hold = range_position(price, day_low, day_high)
    if hold < cfg.hold_range_min:
        return None, f"당일 범위 하단({hold * 100:.0f}%)"
    warnings = []
    if projected:
        warnings.append(f"장중 거래량은 개장 {minutes}분 기준 추정치입니다 (원 거래량 {volx_raw:.1f}배).")
    run = prior_run_pct(bars, len(bars) - 1, cfg.run_lookback)
    if run is not None and run >= cfg.run_zero_pct:
        warnings.append(f"갭 직전 {cfg.run_lookback}세션 동안 이미 {run:+.0f}% 올랐습니다 (과열 감점).")
    sma10 = fc.sma(bars["close"], 10)
    return {"price": price, "gap": gap, "volx": volx, "volx_raw": volx_raw, "projected": projected,
            "mcap": mcap, "day_low": day_low, "sma10": sma10, "warnings": warnings,
            "hold": hold, "prior_run": run, "ep_date": sess.isoformat()}, "OK"


def range_position(price: float, low: float, high: float) -> float:
    """당일 고저 범위 안 위치 (0=저가, 1=고가). 범위가 없으면 1."""
    if high <= low:
        return 1.0
    return _clamp((price - low) / (high - low), 0.0, 1.0)


def prior_run_pct(bars: pd.DataFrame, idx: int, lookback: int) -> float | None:
    """idx 바 직전 종가가 lookback 세션 전 종가 대비 몇 % 올랐나. 데이터 부족이면 None."""
    if idx - 1 - lookback < 0:
        return None
    base = float(bars["close"].iloc[idx - 1 - lookback])
    if base <= 0:
        return None
    return (float(bars["close"].iloc[idx - 1]) / base - 1.0) * 100.0


def score_a(r: dict, cat: dict, cfg: Cfg | None = None) -> float:
    """100점 = 거래량 35 + 갭 25 + 갭 버팀 15 + 베이스(비과열) 10 + 촉매 15.
    대형주에서는 EPS 비트 폭의 예측력이 약하므로 촉매 비중을 낮추고 거래량·버팀 비중을 높였다."""
    cfg = cfg or Cfg()
    vol = 35.0 * min(r["volx"], 8.0) / 8.0
    gap = 25.0 * min(r["gap"], 20.0) / 20.0
    hold = 15.0 * _clamp((r.get("hold", 1.0) - cfg.hold_range_min) / (1.0 - cfg.hold_range_min), 0.0, 1.0)
    run = r.get("prior_run")
    if run is None:
        base = 5.0
    else:
        base = 10.0 * _clamp((cfg.run_zero_pct - run) / (cfg.run_zero_pct - cfg.run_full_pct), 0.0, 1.0)
    c = 15.0 if cat["type"] == "EARNINGS" else 8.0
    return vol + gap + hold + base + c


# ----------------------------------------------------------------------------
# 셋업 B 평가
# ----------------------------------------------------------------------------
def evaluate_b(sym, quote, bars_done: pd.DataFrame, cat, cfg: Cfg, live: bool = False):
    """bars_done: 완결된 일봉만 (진행 중인 오늘 바 제외)."""
    if bars_done is None or len(bars_done) < 40:
        return None, "일봉 데이터 부족"
    if cat is None:
        return None, "촉매 미확인"
    n = len(bars_done)
    ep_idx = None
    for k in range(cfg.pb_max_days_since, cfg.pb_min_days_since - 1, -1):
        idx = n - 1 - k
        gv = gap_vol(bars_done, idx)
        if gv and gv[0] >= cfg.gap_min_pct and gv[1] >= cfg.vol_mult_min:
            ep_idx, ep_gv = idx, gv
            break
    if ep_idx is None:
        return None, "최근 3~5세션 내 EP 없음"
    gap, volx, base = ep_gv
    post = bars_done.iloc[ep_idx + 1:]
    dry = post["volume"].iloc[1:]
    if len(dry) < 2:
        return None, "눌림 구간 부족"
    ratio = dry / base
    if ratio.mean() > cfg.pb_dry_mean_max or ratio.max() > cfg.pb_dry_max_max:
        return None, f"거래량 미감소 (평균 {ratio.mean() * 100:.0f}%)"
    last = bars_done.iloc[-1]
    ep_prev_close = float(bars_done["close"].iloc[ep_idx - 1])
    if float(last["close"]) <= ep_prev_close:
        return None, "갭 메워짐"
    post_high = float(post["high"].max())
    depth = (post_high - float(last["close"])) / post_high * 100.0
    if depth > cfg.pb_max_depth_pct:
        return None, f"되돌림 {depth:.1f}% 과다"
    sma10, sma20 = fc.sma(bars_done["close"], 10), fc.sma(bars_done["close"], 20)
    support = None
    for name, v in (("10일선", sma10), ("20일선", sma20)):
        if v and float(last["low"]) <= v * 1.015 and float(last["close"]) >= v * 0.99:
            support = (name, v)
            break
    if support is None:
        return None, "10/20일선 지지 아님"
    if live and not (quote and quote.get("price")):
        return None, "실시간 시세 없음"
    price = float(quote["price"]) if quote and quote.get("price") else float(last["close"])
    if price <= ep_prev_close:
        return None, "갭 메워짐(현재가)"
    if price < support[1] * 0.99:
        return None, f"{support[0]} 이탈(현재가)"
    struct_low = float(bars_done["low"].iloc[-3:].min()) * 0.995
    raw = (price - struct_low) / price * 100.0
    if raw <= 0:
        return None, "구조 저점 이탈"
    if raw > cfg.pb_max_stop_pct:
        return None, f"손절폭 {raw:.1f}% > {cfg.pb_max_stop_pct}%"
    last_high = float(last["high"])
    return {"price": price, "gap": gap, "volx": volx, "volx_raw": volx, "projected": False,
            "mcap": quote.get("market_cap") if quote else None, "day_low": struct_low,
            "sma10": sma10, "warnings": [], "ep_date": bars_done["date"].iloc[ep_idx].date().isoformat(),
            "dry_mean": float(ratio.mean()), "depth": depth, "support": support[0],
            "trigger": last_high, "days_since": n - 1 - ep_idx}, "OK"


def score_b(r: dict, plan: dict, cat: dict) -> float:
    s = 45.0 + 20.0 * (1 - min(r["dry_mean"] / 0.5, 1.0))
    s += 15.0 if r["support"] == "10일선" else 10.0
    s += 15.0 * (1 - min(plan["stop_pct"] / 3.5, 1.0))
    s += 5.0 if cat["type"] == "EARNINGS" else 0.0
    return s


# ----------------------------------------------------------------------------
# 당일 시장 방향 (SPY·QQQ)
# ----------------------------------------------------------------------------
def market_day(api_key: str, state: str, quotes: dict | None = None) -> dict:
    """약세 신호 수: 장중 SPY 시가 아래, 장중 QQQ 시가 아래, SPY 10일선 < 20일선.
    0개면 정상, 1~2개면 후보 1개, 3개 모두면 후보 0개. 조회 실패한 신호는 세지 않는다."""
    weak, unknown = [], []
    if state == "LIVE":
        for sym in ("SPY", "QQQ"):
            q = (quotes or {}).get(sym)
            if not q:
                try:
                    q = fc.fetch_quote(api_key, sym)
                except Exception:
                    q = None
            if not q or not q.get("price") or not q.get("open"):
                unknown.append(f"{sym} 시세")
            elif q["price"] < q["open"]:
                weak.append(f"{sym} 시가 아래 ({(q['price'] / q['open'] - 1) * 100:+.1f}%)")
    try:
        bars = fc.fetch_eod(api_key, "SPY", 40)
    except Exception:
        bars = None
    s10 = fc.sma(bars["close"], 10) if bars is not None else None
    s20 = fc.sma(bars["close"], 20) if bars is not None else None
    if s10 is None or s20 is None:
        unknown.append("SPY 이평")
    elif s10 < s20:
        weak.append("SPY 10일선 < 20일선")
    n = len(weak)
    limit = None if n == 0 else (1 if n < 3 else 0)
    if n == 0:
        detail = "정상" + (f" (판정 불가: {', '.join(unknown)})" if unknown else "")
    elif limit == 1:
        detail = "약세 신호 " + ", ".join(weak) + " → 최상위 후보 1개만"
    else:
        detail = "약세 신호 " + ", ".join(weak) + " → 약세장 주의, 추천 후보 0개 (본장 흐름 보고 재검색)"
    return {"weak": weak, "unknown": unknown, "limit": limit, "detail": detail}


# ----------------------------------------------------------------------------
# 히스토리 / 풀 저장
# ----------------------------------------------------------------------------
def _dump(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, default=lambda o: float(o) if hasattr(o, "__float__") else str(o))


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_pool(path: str = DEFAULT_POOL_PATH):
    return _load(path)


def pool_is_current(pool: dict | None, now: dt.datetime | None = None) -> bool:
    return bool(pool) and pool.get("session") == session_date(now).isoformat()


def load_history(path: str = DEFAULT_HISTORY_PATH) -> list:
    d = _load(path)
    return d.get("events", []) if isinstance(d, dict) else []


def save_history(events: list, sess: dt.date, path: str = DEFAULT_HISTORY_PATH):
    cutoff = (sess - dt.timedelta(days=14)).isoformat()
    seen, out = set(), []
    for e in events:
        key = (e["symbol"], e["date"])
        if key in seen or e["date"] < cutoff:
            continue
        seen.add(key)
        out.append(e)
    _dump({"events": out}, path)


# ----------------------------------------------------------------------------
# 전체 스캔
# ----------------------------------------------------------------------------
def _universe_today(api_key: str, cfg: Cfg) -> list:
    syms = {}
    for ep_ in ("biggest-gainers", "most-actives"):
        rows = fc.fmp_get(ep_, api_key)
        if not isinstance(rows, list):
            continue
        for r in rows:
            s = r.get("symbol")
            px = fc._f(r.get("price"))
            chg = fc._f(r.get("changesPercentage", r.get("changePercentage")))
            if not s or "." in s or px is None or px < cfg.price_min:
                continue
            if chg is not None and chg < cfg.prefilter_chg_pct:
                continue
            syms[s] = r.get("name")
    return list(syms.keys())


def _universe_backfill(api_key: str, cfg: Cfg, max_universe: int) -> list:
    rows = fc.fmp_get("company-screener", api_key, {
        "marketCapMoreThan": int(cfg.mcap_min), "priceMoreThan": cfg.price_min,
        "volumeMoreThan": int(cfg.avg_vol_min), "isEtf": "false", "isFund": "false",
        "isActivelyTrading": "true", "exchange": "NASDAQ,NYSE", "limit": 1000})
    if not isinstance(rows, list):
        return []
    rows = [r for r in rows if r.get("symbol") and "." not in r["symbol"]]
    rows.sort(key=lambda r: fc._f(r.get("volume")) or 0, reverse=True)
    return [r["symbol"] for r in rows[:max_universe]]


# FMP profile 섹터명 → SPDR 섹터 ETF.
SECTOR_ETF = {
    "Technology": "XLK", "Healthcare": "XLV", "Financial Services": "XLF", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Energy": "XLE", "Industrials": "XLI", "Basic Materials": "XLB",
    "Real Estate": "XLRE", "Utilities": "XLU", "Communication Services": "XLC",
}


def float_turnover_pct(volume, float_shares) -> float | None:
    """당일 거래량 ÷ 유통주식 수 (%)."""
    v, f = fc._f(volume), fc._f(float_shares)
    if not v or not f or f <= 0:
        return None
    return v / f * 100.0


def sector_context(sym: str, prof: dict | None, movers: dict, etf_quotes: dict, state: str) -> dict | None:
    """섹터·업종 동조. movers: 오늘 갭 기준 이상 오른 종목 {심볼: 업종}.
    etf_vs_open_pct 는 장중에만 (섹터 ETF 현재가 / 시가 - 1)."""
    if not prof or not (prof.get("sector") or prof.get("industry")):
        return None
    sector, industry = prof.get("sector") or "", prof.get("industry") or ""
    etf = SECTOR_ETF.get(sector)
    vs_open = None
    q = etf_quotes.get(etf) if etf else None
    if state == "LIVE" and q and q.get("price") and q.get("open"):
        vs_open = (q["price"] / q["open"] - 1.0) * 100.0
    peers = sorted(s for s, ind in movers.items() if s != sym and industry and ind == industry)
    return {"sector": sector, "industry": industry, "etf": etf, "etf_vs_open_pct": vs_open, "peers": peers}


# 종목이 셋업에 안 맞아서가 아니라 데이터를 못 받아서 빠진 경우.
_DATA_FAIL = frozenset({"시세 없음", "일봉 없음", "처리 오류", "세션일 바 없음", "실시간 시세 없음",
                        "전일 종가 불일치(일봉 결측·분할 의심)", "갭/거래량 계산 불가", "일봉 데이터 부족",
                        "촉매 조회 실패"})


def _pack(sym, name, setup, score, r, plan, cat):
    return {"symbol": sym, "name": name or "", "setup": setup, "setup_label": SETUP_LABEL[setup],
            "score": round(float(score), 1), "price": r["price"], "gap": r["gap"], "volx": r["volx"],
            "volx_raw": r["volx_raw"], "projected": r["projected"], "mcap": r["mcap"],
            "ep_date": r["ep_date"], "sma10": r["sma10"], "catalyst": cat, "warnings": list(r["warnings"]),
            "hold": r.get("hold"), "prior_run": r.get("prior_run"),
            "float_turn": r.get("float_turn"), "float_shares": r.get("float_shares"), "sector": None,
            "plan": plan, "extra": {k: r[k] for k in ("dry_mean", "depth", "support", "trigger", "days_since") if k in r}}


def scan_all(api_key: str, cfg: Cfg | None = None, backfill: bool = False, max_universe: int = 250,
             progress=None, now: dt.datetime | None = None, pool_path: str = DEFAULT_POOL_PATH,
             history_path: str = DEFAULT_HISTORY_PATH, workers: int = 8) -> dict:
    cfg = cfg or Cfg()
    now_et = (now or dt.datetime.now(ET)).astimezone(ET)
    sess = session_date(now_et)
    state, minutes = market_state(now_et)
    rejects: dict[str, int] = {}
    cands_a, cands_b, new_events = [], [], []
    hist = load_history(history_path)

    def bump(reason):
        key = reason.split("(")[0].split(" ")[0] if reason.startswith("갭 ") or reason.startswith("거래량 ") else reason
        rejects[key] = rejects.get(key, 0) + 1

    # ---- 유니버스 구성
    uni_a = _universe_today(api_key, cfg)
    names = {}
    hist_syms = sorted({e["symbol"] for e in hist if e["date"] < sess.isoformat()
                        and e["date"] >= (sess - dt.timedelta(days=9)).isoformat()})
    bf_syms = _universe_backfill(api_key, cfg, max_universe) if backfill else []
    total = len(uni_a) + len(hist_syms) + len(bf_syms)
    done = 0
    # 시세는 batch-quote 로 한 번에 받고, 빠진 종목만 개별 조회한다.
    quotes = fc.fetch_quotes(api_key, list(dict.fromkeys(uni_a + hist_syms + bf_syms)))
    movers: dict = {}       # 오늘 갭 기준 이상 오른 종목 {심볼: 업종} (업종 동반 상승 판정용)
    cat_fails: dict = {}    # 촉매 출처별 조회 실패 수

    def quote_of(sym):
        return quotes.get(sym) or fc.fetch_quote(api_key, sym)

    def w_a(sym):
        q = quote_of(sym)
        if not q:
            return sym, None, "시세 없음", None
        if (q.get("market_cap") or 0) < cfg.mcap_min:
            return sym, None, "시총 하한 미달", q
        op, pc = q.get("open"), q.get("prev_close")
        if op and pc and (op / pc - 1.0) * 100.0 >= cfg.gap_min_pct:
            prof = fc.fetch_profile(api_key, sym)
            if prof and prof.get("industry"):
                movers[sym] = prof["industry"]
        bars = fc.fetch_eod(api_key, sym, 120)
        if bars is None:
            return sym, None, "일봉 없음", q
        bars = attach_live_bar(bars, q, sess, state)
        r, why = evaluate_a(sym, q, bars, cfg, sess, state, minutes)
        if not r:
            return sym, None, why, q
        cat, failed = catalyst_check(api_key, sym, sess, cfg, cat_fails)
        if not cat:
            return sym, None, "촉매 조회 실패" if failed else "촉매 없음", q
        fl = fc.fetch_float(api_key, sym)
        r["float_shares"] = fl
        turn = float_turnover_pct(q.get("volume"), fl)
        r["float_turn"] = round(turn, 1) if turn is not None else None
        return sym, (r, cat), "OK", q

    def w_b(sym, cat_in):
        q = quote_of(sym)
        bars = fc.fetch_eod(api_key, sym, 120)
        if bars is None:
            return sym, None, "일봉 없음", q
        bars_done = bars[bars["date"].dt.date < sess] if state == "LIVE" else bars
        if state != "LIVE":
            bars_done = bars[bars["date"].dt.date <= sess]
        cat = cat_in
        if cat is None:
            r0, _ = evaluate_b(sym, q, bars_done, {"type": "NEWS"}, cfg, state == "LIVE")
            if not r0:
                return sym, None, "EP 없음/눌림 불충족", q
            cat = catalyst_check(api_key, sym, dt.date.fromisoformat(r0["ep_date"]), cfg, cat_fails)[0]
        r, why = evaluate_b(sym, q, bars_done, cat, cfg, state == "LIVE")
        return sym, ((r, cat) if r else None), why, q

    jobs = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for s in uni_a:
            jobs.append(("A", ex.submit(w_a, s)))
        cat_by_hist = {}
        for e in sorted(hist, key=lambda e: e.get("date", "")):
            if e.get("catalyst"):
                cat_by_hist[e["symbol"]] = e["catalyst"]  # 같은 종목은 가장 최근 EP 의 촉매
        for s in hist_syms:
            jobs.append(("B", ex.submit(w_b, s, cat_by_hist.get(s))))
        for s in bf_syms:
            if s not in hist_syms and s not in uni_a:
                jobs.append(("B", ex.submit(w_b, s, None)))
        total = len(jobs)
        for fut in as_completed([j[1] for j in jobs]):
            kind = next(k for k, f in jobs if f is fut)
            done += 1
            try:
                sym, res, why, q = fut.result()
            except Exception:
                bump("처리 오류")
                continue
            if progress:
                progress(done, total, sym)
            if q and q.get("name"):
                names[sym] = q["name"]
            if not res:
                bump(why)
                continue
            r, cat = res
            if kind == "A":
                plan = make_plan(r["price"], r["day_low"], r["sma10"], cfg)
                sc = score_a(r, cat, cfg)
                cands_a.append(_pack(sym, names.get(sym), SETUP_A, sc, r, plan, cat))
                new_events.append({"symbol": sym, "date": sess.isoformat(), "gap": r["gap"], "volx": r["volx"],
                                   "catalyst": cat})
            else:
                ref_price = r["price"]
                plan = make_plan(ref_price, r["day_low"], r["sma10"], cfg)
                cands_b.append(_pack(sym, names.get(sym), SETUP_B, score_b(r, plan, cat), r, plan, cat))
                new_events.append({"symbol": sym, "date": r["ep_date"], "gap": r["gap"], "volx": r["volx"],
                                   "catalyst": cat})

    n_fail = sum(v for k, v in rejects.items() if k in _DATA_FAIL)
    save_history(hist + new_events, sess, history_path)
    from . import event_driven as _priced
    allc = [c for c in cands_a + cands_b if c["score"] >= cfg.min_score]
    allc.sort(key=lambda c: c["score"], reverse=True)
    profs = {c["symbol"]: fc.fetch_profile(api_key, c["symbol"]) for c in allc[:10]}
    etfs = sorted({SECTOR_ETF.get((p or {}).get("sector")) for p in profs.values()} - {None})
    idx_quotes = fc.fetch_quotes(api_key, ["SPY", "QQQ"] + etfs) if (state == "LIVE" or etfs) else {}
    for c in allc[:10]:
        ctx = sector_context(c["symbol"], profs.get(c["symbol"]), movers, idx_quotes, state)
        c["sector"] = ctx
        if ctx and ctx["etf_vs_open_pct"] is not None and ctx["etf_vs_open_pct"] < 0:
            c["warnings"].append(f"업종 역풍: 섹터 ETF {ctx['etf']}가 시가 아래입니다 ({ctx['etf_vs_open_pct']:+.1f}%).")
    mday = market_day(api_key, state, idx_quotes)
    top_n = cfg.top_n if mday["limit"] is None else min(cfg.top_n, mday["limit"])
    top = _priced.priced_top(allc, top_n) if top_n > 0 else []
    pool = {"session": sess.isoformat(), "scanned_at_kst": now_et.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
            "state": state, "minutes": minutes, "scanned": total, "backfill": bool(backfill),
            "top": top, "rejects": dict(sorted(rejects.items(), key=lambda kv: -kv[1])[:12]),
            "n_a": len(cands_a), "n_b": len(cands_b),
            "data_fail": n_fail, "fail_rate": round(n_fail / total, 3) if total else 0.0,
            "degraded": bool(total) and n_fail / total > 0.2,
            "market_day": mday, "n_passed": len(allc), "catalyst_fails": cat_fails,
            "quote_batch": len(quotes)}
    _dump(pool, pool_path)
    return pool
