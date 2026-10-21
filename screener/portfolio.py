"""수호 데스크 엔진: 포지션 규칙(2단계 분할 익절) + cloud_sync(Gist) 어댑터.

cloud_sync.py 는 한 글자도 수정하지 않는다. 이 모듈이 공개 함수를 탐지해서 호출한다.
함수명이 다르면 아래 _LOAD_NAMES / _SAVE_NAMES 튜플만 고치면 된다 (cloud_sync.py 수정 금지).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

FEE_PCT = 0.2          # 수수료 0.2% 선반영
TP1_PCT = 5.0          # +5% 도달 시 50% 분할 익절
STOP_MIN, STOP_MAX = 3.0, 3.5
NEAR_STOP_PCT = 0.7    # 손절선까지 0.7% 이내면 근접 경고
PB = "PB"              # 눌림 반등 포지션 (v4.5): 손절 없음, 5일선 위 종가 또는 5거래일째 청산
PB_MAX_HOLD = 5

PORTFOLIO_PATH = Path("portfolio.json")
_LOAD_NAMES = (
    "pull_portfolio_from_cloud",
    "load_portfolio", "pull_portfolio", "get_portfolio", "read_portfolio", "load_positions", "load",
)
_SAVE_NAMES = (
    "push_portfolio_to_cloud",
    "save_portfolio", "push_portfolio", "put_portfolio", "write_portfolio", "save_positions", "save",
)


# ----------------------------------------------------------------------------
# 포지션 스키마
# ----------------------------------------------------------------------------
def _num(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def normalize(p: dict) -> dict | None:
    """구형 스키마(symbol/shares/avg_price 등)도 허용. 원본 키는 보존한다."""
    if not isinstance(p, dict):
        return None
    t = p.get("ticker") or p.get("symbol")
    qty = _num(p.get("qty", p.get("shares", p.get("quantity"))))
    avg = _num(p.get("avg", p.get("avg_price", p.get("entry_price", p.get("avg_cost")))))
    if not t or qty is None or avg is None or qty <= 0 or avg <= 0:
        return None
    out = dict(p)
    out["ticker"] = str(t).strip().upper()
    out["qty"] = qty
    out["avg"] = avg
    sp = _num(p.get("stop_pct"), STOP_MAX)
    out["stop_pct"] = min(max(sp, STOP_MIN), STOP_MAX)
    out["tp1_done"] = bool(p.get("tp1_done", False))
    out["opened"] = str(p.get("opened") or p.get("entry_date") or dt.date.today().isoformat())
    out["note"] = str(p.get("note", ""))
    out["strategy"] = PB if str(p.get("strategy") or "").upper() == PB else "EP"
    return out


def new_position(ticker: str, qty: float, avg: float, stop_pct: float = STOP_MAX, note: str = "",
                 strategy: str = "EP") -> dict:
    return normalize({"ticker": ticker, "qty": qty, "avg": avg, "stop_pct": stop_pct, "note": note,
                      "tp1_done": False, "opened": dt.date.today().isoformat(), "strategy": strategy})


# ----------------------------------------------------------------------------
# 규칙 엔진
# ----------------------------------------------------------------------------
def business_days_held(opened, today: dt.date | None = None) -> int:
    """진입일부터 today까지 NYSE 세션 수. 진입 당일이 1이다."""
    try:
        start = dt.date.fromisoformat(str(opened)[:10])
    except ValueError:
        return 1
    if today is None:
        from zoneinfo import ZoneInfo
        today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    if start > today:
        return 0
    try:
        from . import radar
        days = radar._nyse().valid_days(start.isoformat(), today.isoformat())
        return int(len(days))
    except Exception:
        n = 0
        cur = start
        while cur <= today:
            if cur.weekday() < 5:
                n += 1
            cur += dt.timedelta(days=1)
        return n


def guardian(pos: dict, price: float | None = None, sma10: float | None = None, today: dt.date | None = None,
             sma5: float | None = None) -> dict:
    """수호 판정. 화면은 evaluate와 같은 dict를 쓴다."""
    return evaluate(pos, price, sma10, today, sma5)


def pb_last_day(opened, max_hold: int = PB_MAX_HOLD) -> dt.date | None:
    """눌림 반등 마지막 매도일: 진입일을 1일째로 센 max_hold 번째 NYSE 세션 (v4.5.5)."""
    try:
        day = dt.date.fromisoformat(str(opened)[:10])
    except ValueError:
        return None
    from . import radar
    n = 0
    for _ in range(max_hold * 3 + 10):
        if radar.is_session_day(day):
            n += 1
            if n == max_hold:
                return day
        day += dt.timedelta(days=1)
    return None


def pb_target(dates, closes, now_et: dt.datetime) -> float | None:
    """눌림 반등 목표 매도가 (v4.5.8). '종가 > 5일선'은 오늘 종가가 직전 4거래일 종가 평균보다 높다는 뜻이다.
    오늘 장 마감(16:00 ET) 전에는 오늘 봉을 빼고, 마감 후에는 넣어 다음 거래일 목표가를 낸다."""
    today = now_et.date()
    done = now_et.time() >= dt.time(16, 0)
    last = [float(c) for d, c in zip(dates, closes) if (d.date() if hasattr(d, "date") else d) < today or done]
    return sum(last[-4:]) / 4.0 if len(last) >= 4 else None


def _pnl(price, avg, qty):
    gross = (price / avg - 1.0) * 100.0
    pnl = (price - avg) * qty - price * qty * FEE_PCT / 100.0
    return gross, pnl / (avg * qty) * 100.0, pnl


def evaluate_pullback(pos: dict, price: float | None, sma5: float | None, today: dt.date | None = None,
                      target: float | None = None) -> dict:
    """눌림 반등 포지션: 손절선 없이 반등(종가 > 5일선) 또는 보유 5거래일째 종가에 전량 청산.
    target(목표 매도가, pb_target)이 있으면 그 가격과 비교한다. 없으면 예전처럼 5일선과 비교."""
    line = target or sma5
    avg, qty = pos["avg"], pos["qty"]
    held = business_days_held(pos.get("opened"), today)
    base = {"ticker": pos["ticker"], "qty": qty, "avg": avg, "stop": None, "stop_pct": None, "tp1": sma5,
            "sma10": None, "sma5": sma5, "target": target, "tp1_done": False, "breakeven": avg * (1 + FEE_PCT / 100.0),
            "price": price, "cost": avg * qty, "held_sessions": held, "strategy": PB,
            "gross_pct": None, "net_pct": None, "pnl_usd": None, "value": None, "near_stop": False}
    if price and price > 0:
        gross, net, pnl = _pnl(price, avg, qty)
        base.update(gross_pct=gross, net_pct=net, pnl_usd=pnl, value=price * qty)
    if held > PB_MAX_HOLD:
        base.update(signal="TIME_EXIT", reason=f"보유 {PB_MAX_HOLD}거래일을 넘겼다. 즉시 전량 청산.",
                    alert="🚨 시간 청산 사이렌")
    elif not price or price <= 0:
        base.update(signal="NO_DATA", reason="시세 조회 실패", alert="")
    elif line and price > line:
        base.update(signal="EXIT", reason="목표 매도가(5일선) 위 — 반등 완료. 오늘 종가 무렵 전량 매도.",
                    alert="🎯 반등 청산 구간")
    elif held >= PB_MAX_HOLD:
        base.update(signal="TIME_EXIT", reason=f"보유 {PB_MAX_HOLD}거래일째 — 오늘 종가 무렵 전량 청산.",
                    alert="🚨 시간 청산 사이렌")
    else:
        base.update(signal="HOLD", reason=f"반등 대기 ({held}/{PB_MAX_HOLD}거래일) — 손절 없이 5일선 회복까지 보유",
                    alert="")
    return base


def evaluate(pos: dict, price: float | None, sma10: float | None = None, today: dt.date | None = None,
             sma5: float | None = None, pb_target: float | None = None) -> dict:
    if pos.get("strategy") == PB:
        return evaluate_pullback(pos, price, sma5, today, pb_target)
    avg, qty = pos["avg"], pos["qty"]
    tp1_done = pos["tp1_done"]
    breakeven = avg * (1 + FEE_PCT / 100.0)          # 수수료 포함 본전
    stop = breakeven if tp1_done else avg * (1 - pos["stop_pct"] / 100.0)
    tp1 = avg * (1 + TP1_PCT / 100.0)
    base = {"ticker": pos["ticker"], "qty": qty, "avg": avg, "stop": stop, "stop_pct": pos["stop_pct"],
            "tp1": tp1, "sma10": sma10, "tp1_done": tp1_done, "breakeven": breakeven,
            "price": price, "cost": avg * qty}
    held_sessions = business_days_held(pos.get("opened"), today)
    base["held_sessions"] = held_sessions
    if held_sessions > 3:
        base.update(signal="TIME_EXIT", reason="보유 3거래일을 넘겼다. 수익률과 관계없이 전량 청산.",
                    alert="🚨 시간 손절 사이렌", gross_pct=None, net_pct=None, pnl_usd=None, value=None,
                    near_stop=False)
        if price and price > 0:
            gross = (price / avg - 1.0) * 100.0
            fee = price * qty * FEE_PCT / 100.0
            pnl = (price - avg) * qty - fee
            net = pnl / (avg * qty) * 100.0
            base.update(gross_pct=gross, net_pct=net, pnl_usd=pnl, value=price * qty)
        return base
    if not price or price <= 0:
        base.update(signal="NO_DATA", reason="시세 조회 실패", alert="", gross_pct=None, net_pct=None,
                    pnl_usd=None, value=None, near_stop=False)
        return base
    gross = (price / avg - 1.0) * 100.0
    fee = price * qty * FEE_PCT / 100.0
    pnl = (price - avg) * qty - fee
    net = pnl / (avg * qty) * 100.0
    near = (not (price <= stop)) and (price - stop) / price * 100.0 <= NEAR_STOP_PCT
    signal, reason, alert = "HOLD", "규칙 이탈 없음 — 홀딩", ""
    if price <= stop:
        signal = "EXIT"
        if tp1_done:
            reason, alert = "본전 스탑 도달 — 잔량 청산", "🔴 본전 스탑 발동"
        else:
            reason, alert = f"손절선 -{pos['stop_pct']:.1f}% 도달 — 즉시 청산", "🔴 칼손절 경보"
    elif tp1_done and sma10 and price < sma10:
        signal, reason, alert = "EXIT", "10일선 이탈 — 잔량 청산", "🔴 10일선 이탈"
    elif (not tp1_done) and gross >= TP1_PCT:
        signal, reason, alert = "TRIM", "+5% 도달 — 50% 분할 익절 후 손절선을 본전으로", "🟡 1차 익절 구간"
    elif near:
        alert = "⚠️ 손절선 근접"
    base.update(signal=signal, reason=reason, alert=alert, gross_pct=gross, net_pct=net,
                pnl_usd=pnl, value=price * qty, near_stop=near)
    return base


def summarize(rows: list) -> dict:
    ok = [r for r in rows if r.get("pnl_usd") is not None]
    cost = sum(r["cost"] for r in ok)
    pnl = sum(r["pnl_usd"] for r in ok)
    value = sum(r["value"] for r in ok)
    return {"cost": cost, "value": value, "pnl": pnl, "pnl_pct": (pnl / cost * 100.0) if cost else 0.0, "n": len(ok)}


def apply_tp1(pos: dict) -> dict:
    """1차 익절 체결 처리: 수량 50% 감소 + 손절선 본전 전환."""
    out = dict(pos)
    out["qty"] = round(pos["qty"] * 0.5, 6)
    out["tp1_done"] = True
    return out


# ----------------------------------------------------------------------------
# cloud_sync(Gist) 어댑터
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 에이전트(swing-agent) 보유 목록 따라 읽기 (v4.5.7)
# ----------------------------------------------------------------------------
AGENT = "agent"         # source 표시: 에이전트 목록에서 온 포지션
AGENT_REPO = "fance2552/swing-agent"
AGENT_PATH = "data/holdings.json"
_AGENT_KEYS = ("qty", "avg", "opened", "strategy", "tp1_done")


def fetch_agent_holdings(token: str, repo: str = AGENT_REPO, path: str = AGENT_PATH) -> list | None:
    """에이전트 저장소의 보유 목록. 실패하면 None (읽기 전용)."""
    import requests
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", timeout=15,
                         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw+json"})
        if not r.ok:
            return None
        rows = r.json()
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def merge_agent(positions: list, agent_rows: list) -> tuple[list, list, list, bool]:
    """(합친 목록, 추가 티커, 제거 티커, 바뀜 여부). 에이전트에 있는 종목은 에이전트 값이 기준이다.
    에이전트에서 온 종목이 에이전트 목록에서 빠지면 지운다. 봇에서만 넣은 종목은 건드리지 않는다."""
    agent = {}
    for h in agent_rows or []:
        p = normalize(h)
        if p:
            agent[p["ticker"]] = p
    out, removed, changed = [], [], False
    for p in positions:
        a = agent.get(p["ticker"])
        if a is None:
            if p.get("source") == AGENT:
                removed.append(p["ticker"])
                changed = True
                continue
            out.append(p)
            continue
        new = dict(p, source=AGENT, **{k: a[k] for k in _AGENT_KEYS})
        changed = changed or new != p
        out.append(new)
    have = {p["ticker"] for p in out}
    added = [dict(a, source=AGENT) for t, a in agent.items() if t not in have]
    for a in added:
        a.pop("saved", None)
        a.pop("symbol", None)
    return out + added, [a["ticker"] for a in added], removed, changed or bool(added)


def _cloud():
    try:
        import cloud_sync  # type: ignore
        return cloud_sync
    except Exception:
        pass
    try:
        from screener import cloud_sync  # type: ignore
        return cloud_sync
    except Exception:
        return None


def _extract_list(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, dict):
        for k in ("positions", "portfolio", "holdings"):
            if isinstance(raw.get(k), list):
                return raw[k]
        if raw and all(isinstance(v, dict) for v in raw.values()):
            return [dict(v, ticker=v.get("ticker") or k) for k, v in raw.items()]
        return []
    return list(raw) if isinstance(raw, (list, tuple)) else []


def _read_local() -> list:
    try:
        raw = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [p for p in (normalize(x) for x in _extract_list(raw)) if p]


def load_positions():
    """(positions, message). 실제 cloud_sync 는 경로로 portfolio.json 을 당긴다."""
    cs = _cloud()
    if cs is None:
        return _read_local(), "cloud_sync 모듈을 찾지 못했습니다 (세션 메모리만 사용)"
    pull = getattr(cs, "pull_portfolio_from_cloud", None)
    if callable(pull):
        try:
            err = pull(PORTFOLIO_PATH) or ""
        except Exception as e:
            return _read_local(), f"Gist 로드 실패: {type(e).__name__}"
        pos = _read_local()
        if err and not pos:
            return [], f"Gist 로드 실패: {err}"
        note = "Gist 동기화 로드 완료 (pull_portfolio_from_cloud)"
        if err:
            note = f"Gist 동기화 로드 경고: {err}"
        return pos, note
    for name in _LOAD_NAMES:
        fn = getattr(cs, name, None)
        if not callable(fn) or name == "pull_portfolio_from_cloud":
            continue
        try:
            raw = fn()
            pos = [p for p in (normalize(x) for x in _extract_list(raw)) if p]
            return pos, f"Gist 동기화 로드 완료 ({name})"
        except Exception as e:
            return [], f"Gist 로드 실패: {type(e).__name__}"
    return [], "cloud_sync 에서 로드 함수를 찾지 못했습니다"


def save_positions(positions: list):
    """(ok, message). 로컬 portfolio.json 을 쓴 뒤 Gist 파일로 올린다."""
    try:
        PORTFOLIO_PATH.write_text(
            json.dumps(list(positions), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        return False, f"로컬 저장 실패: {type(e).__name__}"
    cs = _cloud()
    if cs is None:
        return False, "cloud_sync 없음 — 세션 메모리에만 저장됨"
    push = getattr(cs, "push_portfolio_to_cloud", None)
    if callable(push):
        try:
            push(PORTFOLIO_PATH)
            return True, "Gist 저장 완료 (push_portfolio_to_cloud)"
        except Exception as e:
            return False, f"Gist 저장 실패: {type(e).__name__}"
    for name in _SAVE_NAMES:
        fn = getattr(cs, name, None)
        if not callable(fn) or name == "push_portfolio_to_cloud":
            continue
        try:
            fn(positions)
            return True, f"Gist 저장 완료 ({name})"
        except TypeError:
            try:
                fn({"positions": positions})
                return True, f"Gist 저장 완료 ({name}, dict)"
            except Exception as e:
                return False, f"Gist 저장 실패: {type(e).__name__}"
        except Exception as e:
            return False, f"Gist 저장 실패: {type(e).__name__}"
    return False, "cloud_sync 에서 저장 함수를 찾지 못했습니다"
