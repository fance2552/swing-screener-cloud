"""portfolio.json 관리 + 전략별 청산 헌법. No LDPB. peak_price 는 로컬만 갱신한다."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import cloud_sync
import config
from data.fmp import fetch_fmp_analyst_data
from screener import event_driven as ed
from state import SHARED

PORTFOLIO_PATH = Path(__file__).resolve().parent.parent / "portfolio.json"
CLOSED_TRADES_PATH = Path(__file__).resolve().parent.parent / Path(config.CLOSED_TRADES_LOG_PATH).name
BACKUP_PATH = PORTFOLIO_PATH.with_name("portfolio.json.bak")
_PF_LOCK = threading.RLock()
_CACHE: list[dict[str, Any]] = []
_CACHE_OK = False
_LAST_PEAK_SAVE_TS: float = 0.0
_LAST_PEAK_SAVE_SNAPSHOT: dict[str, float] = {}


def _parse_rows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("portfolio.json 은 리스트여야 합니다")
    return [row for row in raw if isinstance(row, dict) and row.get("ticker")]


def _read_path(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"{path.name} 이 비어 있습니다")
    return _parse_rows(json.loads(text))


def _read_raw() -> list[dict[str, Any]]:
    return _read_path(PORTFOLIO_PATH)


def _clone(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _fsync_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())


def _remember(rows: list[dict[str, Any]]) -> None:
    global _CACHE, _CACHE_OK
    _CACHE = _clone(rows)
    _CACHE_OK = True
    SHARED.set_portfolio_health("", False)


def load_portfolio() -> list[dict[str, Any]]:
    """Fail-closed. Never hide a corrupt file behind an empty list.

    Order: live file → in-memory last-good → portfolio.json.bak.
    UI must read SHARED.portfolio_corrupt / portfolio_error before treating [] as 'no positions'.
    """
    global _CACHE, _CACHE_OK
    with _PF_LOCK:
        cloud_err = cloud_sync.pull_portfolio_from_cloud(PORTFOLIO_PATH)
        cloud_sync.pull_closed_trades_from_cloud(CLOSED_TRADES_PATH)
        if cloud_err and not PORTFOLIO_PATH.exists():
            SHARED.set_portfolio_health(
                f"클라우드(Gist) 포트폴리오 복원 실패. 빈 목록으로 위장하지 않음. {cloud_err}",
                True,
            )
            return _clone(_CACHE) if _CACHE_OK else []
        try:
            rows = _read_raw()
            _remember(rows)
            return _clone(rows)
        except FileNotFoundError:
            _remember([])
            return []
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if _CACHE_OK:
                SHARED.set_portfolio_health(
                    f"portfolio.json 손상. 마지막 정상 캐시 {len(_CACHE)}건 유지. {detail}",
                    True,
                )
                return _clone(_CACHE)
            try:
                if not BACKUP_PATH.exists():
                    raise FileNotFoundError(f"{BACKUP_PATH.name} 없음")
                bak = _read_path(BACKUP_PATH)
            except Exception as bak_exc:  # noqa: BLE001
                SHARED.set_portfolio_health(
                    f"portfolio.json 손상, 백업도 실패. 빈 목록으로 위장하지 않음. "
                    f"{detail} / bak {type(bak_exc).__name__}: {bak_exc}",
                    True,
                )
                return []
            _CACHE = _clone(bak)
            _CACHE_OK = True
            SHARED.set_portfolio_health(
                f"portfolio.json 손상. {BACKUP_PATH.name} 에서 {len(bak)}건 복구. {detail}",
                True,
            )
            return _clone(bak)


def save_portfolio(data: list[dict[str, Any]]) -> None:
    rows = _parse_rows(data)
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    tmp = PORTFOLIO_PATH.with_name(
        f"{PORTFOLIO_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    with _PF_LOCK:
        try:
            if PORTFOLIO_PATH.exists() and PORTFOLIO_PATH.stat().st_size > 0:
                current = PORTFOLIO_PATH.read_text(encoding="utf-8")
                try:
                    _parse_rows(json.loads(current))
                except Exception:  # noqa: BLE001
                    current = ""
                if current:
                    _fsync_write(BACKUP_PATH, current)
            _fsync_write(tmp, payload)
            os.replace(tmp, PORTFOLIO_PATH)
            _remember(rows)
            cloud_sync.push_portfolio_to_cloud(PORTFOLIO_PATH)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass


def _rows_for_write() -> list[dict[str, Any]]:
    rows = load_portfolio()
    if SHARED.portfolio_corrupt and not rows and not _CACHE_OK:
        raise ValueError(
            "portfolio.json 이 손상되었고 복구 캐시도 없습니다. 빈 파일로 덮어쓰지 않습니다."
        )
    return rows


def held_tickers() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in load_portfolio():
        tk = str(row.get("ticker") or "").upper().strip()
        if tk and tk not in seen:
            seen.add(tk)
            out.append(tk)
    return out


def add_position(
    ticker: str,
    entry_price: float,
    shares: float,
    strategy: str = "EARNINGS",
    earnings_date: str = "",
) -> dict[str, Any]:
    tk = (ticker or "").upper().strip()
    entry = float(entry_price)
    qty = float(shares)
    if not tk or entry <= 0 or qty <= 0:
        raise ValueError("티커 / 평단가 / 수량 필요")
    strat = str(strategy or "EARNINGS").upper().strip()
    if strat not in ("EARNINGS", "SQUEEZE"):
        raise ValueError("전략은 EARNINGS 또는 SQUEEZE")

    edate, date_src = "", ""
    row_e: dict[str, Any] | None = None
    typed = str(earnings_date or "").strip()
    if typed:
        if ed.days_to_event(typed) is None:
            raise ValueError("실적 발표일 형식은 YYYY-MM-DD")
        edate, date_src = typed[:10], "MANUAL"
    else:
        pool = SHARED.earnings_targets if strat == "EARNINGS" else SHARED.squeeze_targets
        with SHARED._lock:
            for r in pool:
                if str(r.get("ticker") or "").upper() == tk:
                    row_e = dict(r)
                    break
        if row_e and row_e.get("earnings_date"):
            edate, date_src = str(row_e["earnings_date"])[:10], "CAL"
        elif strat == "EARNINGS":
            raise ValueError("실적 런업 종목은 실적 발표일이 필수입니다 (표에 없으면 직접 입력)")
        else:
            try:
                found = ed.next_earnings_date(tk)
            except Exception:  # noqa: BLE001
                found = ""
            if found:
                edate, date_src = found, "AUTO"

    if edate:
        left = ed.days_to_event(edate)
        if left is not None and left < 0:
            raise ValueError("이미 지난 실적 발표일입니다")

    if row_e and (row_e.get("buy_ratio") or row_e.get("target_consensus")):
        wall: dict[str, Any] = {
            "target_consensus": row_e.get("target_consensus"),
            "buy_ratio": row_e.get("buy_ratio"),
        }
    else:
        try:
            wall = fetch_fmp_analyst_data(tk) or {}
        except Exception:  # noqa: BLE001
            wall = {}

    fx = SHARED.quote("KRW=X")
    if fx <= 500:
        fx = 1360.0
    stop = round(entry * (1.0 - float(config.EXIT_SL_PCT) / 100.0), 2)

    pos: dict[str, Any] = {
        "ticker": tk,
        "strategy": strat,
        "entry_price": round(entry, 4),
        "shares": qty,
        "stop_price": stop,
        "entry_date": ed.today_et().isoformat(),
        "entry_fx": round(fx, 1),
        "target_consensus": float(wall.get("target_consensus") or 0.0),
        "buy_ratio": float(wall.get("buy_ratio") or 0.0),
    }
    if edate:
        pos["earnings_date"] = edate
        pos["date_src"] = date_src

    with _PF_LOCK:
        data = [p for p in _rows_for_write() if str(p.get("ticker") or "").upper() != tk]
        data.append(pos)
        save_portfolio(data)
    return pos


def log_closed_trade(pos: dict[str, Any], reason_code: str = "") -> None:
    """청산 1줄을 closed_trades.jsonl 에 append 하고 Gist에도 올린다."""
    try:
        m = metrics(pos)
        today = ed.today_et().isoformat()
        rec = {
            "ticker": m["ticker"],
            "strategy": m["strategy"],
            "reason": reason_code,
            "entry_price": m["entry_price"],
            "entry_date": m["entry_date"],
            "exit_date": today,
            "closed_at_et_date": today,
            "closed_ts": time.time(),
            "exit_mark": m["current_price"],
            "pct": round(float(m["pct"]), 3),
            "r_mult": round(float(m["r_mult"]), 3),
        }
        path = CLOSED_TRADES_PATH
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        cloud_sync.push_closed_trades_to_cloud(path)
    except Exception:  # noqa: BLE001
        pass


def _log_closed(pos: dict[str, Any], reason_code: str = "") -> None:
    log_closed_trade(pos, reason_code)


def remove_ticker(ticker: str) -> None:
    tk = (ticker or "").upper().strip()
    with _PF_LOCK:
        rows = _rows_for_write()
        for p in rows:
            if str(p.get("ticker") or "").upper() == tk:
                m = metrics(p)
                g = guardian(m)
                log_closed_trade(p, reason_code=str(g.get("code") or ""))
        save_portfolio([p for p in rows if str(p.get("ticker") or "").upper() != tk])
        _PEAKS.pop(tk, None)
        _LAST_PEAK_SAVE_SNAPSHOT.pop(tk, None)


def holding_mark(pos: dict[str, Any]) -> float:
    ticker = str(pos.get("ticker") or "").upper().strip()
    px = SHARED.quote(ticker)
    if px > 0:
        return px
    try:
        return float(pos.get("entry_price") or 0)
    except (TypeError, ValueError):
        return 0.0


def sync_earnings_dates(cal: dict[str, str]) -> int:
    """Refresh earnings_date of positions from the fresh calendar. Manual dates untouched."""
    n = 0
    with _PF_LOCK:
        rows = _rows_for_write()
        for p in rows:
            if str(p.get("date_src") or "").upper() == "MANUAL":
                continue
            tk = str(p.get("ticker") or "").upper()
            new = cal.get(tk)
            if new and new != str(p.get("earnings_date") or ""):
                p["earnings_date"] = new
                p["date_src"] = "CAL"
                n += 1
        if n:
            save_portfolio(rows)
    return n


def metrics(pos: dict[str, Any]) -> dict[str, Any]:
    ticker = str(pos.get("ticker") or "").upper()
    entry = float(pos.get("entry_price") or 0)
    shares = float(pos.get("shares") or 0)
    stop = float(pos.get("stop_price") or 0)
    consensus = float(pos.get("target_consensus") or 0)
    buy_ratio = float(pos.get("buy_ratio") or 0)
    px = holding_mark(pos)
    has_quote = SHARED.quote(ticker) > 0
    peak_updated = False
    if has_quote:
        peak_updated = _update_peak_price(pos, px)
    one_r = max(entry - stop, 0.01)
    r_mult = (px - entry) / one_r if one_r and px > 0 else 0.0
    pct = ((px - entry) / entry) * 100.0 if entry and px > 0 else 0.0
    pnl = (px - entry) * shares if px > 0 else 0.0
    raw_date = str(pos.get("entry_date") or date.today().isoformat())
    strategy = str(pos.get("strategy") or "EARNINGS").upper().strip()
    if strategy not in ("EARNINGS", "SQUEEZE"):
        strategy = pos.get("strategy") or "LEGACY"
    e_raw = str(pos.get("earnings_date") or "").strip()
    peak = float(pos.get("peak_price") or entry or 0.0)
    trail_armed = False
    drawdown_from_peak_pct = 0.0
    try:
        max_gain_pct = float(pos.get("max_gain_pct") or 0.0)
    except (TypeError, ValueError):
        max_gain_pct = 0.0
    max_gain_updated = False
    if strategy == "EARNINGS" and has_quote and px and entry:
        gain_from_entry_pct = (peak - entry) / entry * 100.0
        trail_armed = gain_from_entry_pct >= float(config.RUNUP_TRAILING_TRIGGER_PCT)
        if peak:
            drawdown_from_peak_pct = (peak - px) / peak * 100.0
        current_gain_pct = (px - entry) / entry * 100.0
        if current_gain_pct > max_gain_pct:
            max_gain_pct = current_gain_pct
            pos["max_gain_pct"] = max_gain_pct
            max_gain_updated = True
    return {
        "ticker": ticker, "strategy": strategy, "entry_price": entry, "shares": shares,
        "stop_price": stop, "entry_date": raw_date, "target_consensus": consensus,
        "buy_ratio": buy_ratio, "current_price": px, "peak_price": peak,
        "one_r": one_r, "r_mult": r_mult,
        "pct": pct, "pnl": pnl, "pnl_pct": pct,
        "upside": ((consensus - px) / px * 100.0) if consensus > 0 and px > 0 else 0.0,
        "has_quote": has_quote, "earnings_date": e_raw,
        "d_day": ed.days_to_event(e_raw) if e_raw else None,
        "force_exit_d3": ed.must_exit_for_earnings(e_raw) if e_raw else False,
        "hold_bdays": ed.business_days_held(raw_date),
        "trail_armed": trail_armed,
        "drawdown_from_peak_pct": drawdown_from_peak_pct,
        "peak_updated": peak_updated,
        "max_gain_pct": max_gain_pct,
        "max_gain_updated": max_gain_updated,
    }


_PEAKS: dict[str, float] = {}


def _update_peak_price(position: dict, live_price: float) -> bool:
    """메모리 고점을 올린다. 디스크 쓰기는 maybe_persist_peak 가 스로틀한다."""
    if live_price <= 0:
        return False
    tk = str(position.get("ticker") or "").upper()
    try:
        entry = float(position.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        entry = 0.0
    try:
        filed = float(position.get("peak_price") or 0.0)
    except (TypeError, ValueError):
        filed = 0.0
    old = max(_PEAKS.get(tk, 0.0), filed, entry)
    new = live_price if live_price > old else old
    changed = new > old
    if tk and new > 0:
        _PEAKS[tk] = new
    if new > 0:
        position["peak_price"] = new
    return changed


def maybe_persist_peak(m: dict, pos: dict, all_positions: list) -> bool:
    """고점이 올랐을 때만, 20초 바닥과 0.3% 변동을 둘 다 넘긴 뒤에 디스크/Gist에 쓴다.

    지시서의 interval OR delta는 1초 루프에서 Gist를 두드린다. AND로 고정한다.
    """
    if not (m.get("peak_updated") or m.get("max_gain_updated")):
        return False
    ticker = str(pos.get("ticker") or "").upper().strip()
    try:
        peak_price = float(pos.get("peak_price") or 0.0)
    except (TypeError, ValueError):
        peak_price = 0.0
    if not ticker or peak_price <= 0:
        return False

    global _LAST_PEAK_SAVE_TS
    now = time.time()
    try:
        entry = float(pos.get("entry_price") or peak_price)
    except (TypeError, ValueError):
        entry = peak_price
    prev_saved = _LAST_PEAK_SAVE_SNAPSHOT.get(ticker, entry)
    delta_pct = abs(peak_price - prev_saved) / prev_saved * 100.0 if prev_saved else 100.0
    interval_ok = (now - _LAST_PEAK_SAVE_TS) >= float(config.PEAK_SAVE_MIN_INTERVAL_SEC)
    delta_ok = delta_pct >= float(config.PEAK_SAVE_MIN_DELTA_PCT)
    if m.get("peak_updated"):
        if not (interval_ok and delta_ok):
            return False
    elif not interval_ok:
        return False

    _LAST_PEAK_SAVE_TS = now
    _LAST_PEAK_SAVE_SNAPSHOT[ticker] = peak_price
    save_portfolio(all_positions)
    return True


def save_positions_local_only(positions: list[dict[str, Any]]) -> None:
    """peak_price 틱 전용. Gist에는 올리지 않는다."""
    rows: list[dict[str, Any]] = []
    for p in positions:
        row = dict(p)
        row.pop("_peak_dirty", None)
        if row.get("ticker"):
            rows.append(row)
    payload = json.dumps(rows, ensure_ascii=False, indent=2)
    tmp = PORTFOLIO_PATH.with_name(
        f"{PORTFOLIO_PATH.name}.{os.getpid()}.{time.time_ns()}.peak.tmp"
    )
    with _PF_LOCK:
        try:
            _fsync_write(tmp, payload)
            os.replace(tmp, PORTFOLIO_PATH)
            _remember(rows)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    for p in positions:
        p.pop("_peak_dirty", None)


def recently_closed_today(now: datetime | None = None) -> list[str]:
    """오늘(ET) 청산된 티커. closed_trades.jsonl 의 날짜/티커 필드명은 후보로 찾는다."""
    path = CLOSED_TRADES_PATH
    if not path.exists():
        return []
    today_et = (now or datetime.now(ZoneInfo("America/New_York"))).date()
    date_fields = (
        "closed_at_et_date", "closed_at", "exit_date", "date", "closed_date", "timestamp",
    )
    ticker_fields = ("ticker", "symbol")
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        raw_date = next((row.get(f) for f in date_fields if row.get(f)), None)
        if not raw_date:
            continue
        try:
            d = datetime.fromisoformat(str(raw_date)[:19]).date()
        except Exception:
            try:
                d = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
            except Exception:
                continue
        if d != today_et:
            continue
        tk = next((row.get(f) for f in ticker_fields if row.get(f)), None)
        if tk:
            out.append(str(tk).upper())
    return sorted(set(out))


def get_recently_closed_today(now: datetime | None = None) -> list[str]:
    return recently_closed_today(now)


def _is_friday_risk_check_time() -> bool:
    """금요일 15:30 ET 이후. 무조건 청산 창이 아니다."""
    return ed.is_friday_flat_window()


def _pnl_pct(m: dict[str, Any]) -> float:
    if m.get("pnl_pct") is not None:
        return float(m["pnl_pct"])
    return float(m.get("pct") or 0.0)


def guardian(m: dict[str, Any]) -> dict[str, Any]:
    """청산 코드는 dict['code']. 화면이 배지·사이렌·랭크를 이 딕셔너리로 그린다."""
    strategy = str(m.get("strategy") or "")
    if strategy not in ("EARNINGS", "SQUEEZE"):
        return {
            "ticker": m.get("ticker", ""), "code": "LEGACY", "rank": 9, "alert": "warning",
            "badge": "⚠️ [레거시 전략 — 수동 검토]",
            "order": "이 포지션은 폐기된 전략으로 등록되어 자동 진단을 지원하지 않습니다. "
                     "토스 앱에서 직접 확인 후 정리하십시오.",
            "days": int(m.get("hold_bdays") or 1), "structure": "미지원 전략",
            "r_mult": float(m.get("r_mult") or 0), "pct": float(m.get("pct") or 0),
        }

    tk = m.get("ticker", "")
    pct = _pnl_pct(m)
    hold = int(m.get("hold_bdays") or 0)
    quoted = bool(m.get("has_quote"))
    friday = _is_friday_risk_check_time()
    d_day = m.get("d_day")
    entry = float(m.get("entry_price") or 0.0)
    current = float(m.get("current_price") or 0.0)
    peak = float(m.get("peak_price") or entry or 0.0)
    peak_pct = (peak / entry - 1.0) * 100.0 if entry > 0 and peak > 0 else 0.0
    drawdown = float(m.get("drawdown_from_peak_pct") or 0.0)
    if not drawdown and peak > 0 and current > 0:
        drawdown = (peak - current) / peak * 100.0
    try:
        max_gain = float(m.get("max_gain_pct") or 0.0)
    except (TypeError, ValueError):
        max_gain = 0.0
    sl = float(config.EXIT_SL_PCT)
    armed = bool(m.get("trail_armed")) if "trail_armed" in m else (
        quoted and peak_pct >= float(config.RUNUP_TRAILING_TRIGGER_PCT)
    )
    d_day_n = int(d_day) if d_day is not None else None
    hits = {
        "SL": quoted and pct <= -sl,
        "D3": bool(m.get("force_exit_d3")),
        "FRI_RISK_CUT": (
            friday
            and pct <= -float(config.OVERWEEK_LOSS_CUT_PCT)
            and pct > -sl
        ),
        "TRAIL": (
            strategy == "EARNINGS"
            and armed
            and drawdown >= float(config.RUNUP_TRAILING_DROP_PCT)
        ),
        "MOMENTUM_EXPIRE": (
            strategy == "EARNINGS"
            and d_day_n is not None
            and d_day_n <= int(config.RUNUP_MOMENTUM_DEADLINE_DDAY)
            and max_gain < float(config.RUNUP_MOMENTUM_MIN_PCT)
        ),
        "SQZ_TP": strategy == "SQUEEZE" and quoted and pct >= float(config.SQUEEZE_EXIT_TP_PCT),
        "SQZ_TIME": strategy == "SQUEEZE" and hold >= int(config.SQUEEZE_MAX_HOLD_BDAYS),
    }
    copy = {
        "SL": (
            "error",
            f"🔴 [기계적 손절 -{sl:.1f}%]",
            f"손절선(-{sl:.1f}%) 도달. 토스에서 전량 매도하십시오.",
        ),
        "D3": (
            "error",
            "🚨 [D-3 강제 청산]",
            "실적 D-3. 손익과 무관하게 토스에서 전량 매도하십시오.",
        ),
        "FRI_RISK_CUT": (
            "error",
            "🟠 [금요일 리스크 컷]",
            f"금요일 15:30 ET, 미실현 손실이 -{config.OVERWEEK_LOSS_CUT_PCT:g}% 를 넘었습니다. 전량 매도하십시오.",
        ),
        "TRAIL": (
            "success",
            f"🟡 [트레일링 익절: 고점 대비 -{config.RUNUP_TRAILING_DROP_PCT:g}%]",
            f"고점 ${peak:.2f} 대비 {drawdown:.1f}% 반락. 토스에서 전량 익절하십시오.",
        ),
        "MOMENTUM_EXPIRE": (
            "warning",
            "⏰ [런업 모멘텀 부재 만기]",
            f"D-{d_day_n} 인데 최고 수익률이 +{config.RUNUP_MOMENTUM_MIN_PCT:g}% 에 못 미쳤습니다. 전량 매도하십시오.",
        ),
        "SQZ_TP": (
            "success",
            f"🟡 [스퀴즈 폭발 익절 +{config.SQUEEZE_EXIT_TP_PCT:g}%]",
            f"+{config.SQUEEZE_EXIT_TP_PCT:g}% 도달. 토스에서 전량 익절하십시오.",
        ),
        "SQZ_TIME": (
            "warning",
            f"⏰ [스퀴즈 {config.SQUEEZE_MAX_HOLD_BDAYS}거래일 만기]",
            f"보유 {hold}거래일. 스퀴즈 한도 초과. 전량 매도하십시오.",
        ),
    }
    for rank, key in enumerate(config.EXIT_PRIORITY):
        if not hits.get(key):
            continue
        alert, badge, order = copy[key]
        if key == "TRAIL":
            structure = f"고점 ${peak:.2f}" + (f" / D-{d_day_n}" if d_day_n is not None else "")
        elif d_day_n is not None:
            structure = f"실적 D-{d_day_n}"
        else:
            structure = f"{hold}거래일 보유"
        return {
            "ticker": tk, "code": key, "rank": rank, "alert": alert,
            "badge": badge, "order": order, "days": hold,
            "structure": structure, "r_mult": float(m.get("r_mult") or 0), "pct": pct,
        }
    if not quoted:
        return {
            "ticker": tk, "code": "NOQUOTE", "rank": 9, "alert": "error",
            "badge": "⛔ [시세 끊김]",
            "order": "시세 수신이 없습니다. 토스 앱에서 직접 확인하십시오.",
            "days": hold, "structure": "시세 없음", "r_mult": 0.0, "pct": 0.0,
        }
    base = {
        "ticker": tk, "rank": 9, "alert": "info", "days": hold,
        "r_mult": float(m.get("r_mult") or 0), "pct": pct,
    }
    if (
        strategy == "EARNINGS"
        and friday
        and d_day_n is not None
        and d_day_n >= int(config.OVERWEEK_MIN_DDAY)
    ):
        return {
            **base, "code": "OVERWEEK",
            "badge": f"🟢 [오버위크 순항: D-{d_day_n}]",
            "order": f"D-{d_day_n}. 금요일 손실 한도 안쪽. 주말을 넘기고 완주하십시오.",
            "structure": f"실적 D-{d_day_n}",
        }
    if strategy == "EARNINGS" and armed:
        return {
            **base, "code": "HOLD",
            "badge": f"🟢 [트레일링 순항: 고점 대비 -{drawdown:.1f}%]",
            "order": f"무장 상태. 고점 대비 -{config.RUNUP_TRAILING_DROP_PCT:g}% 전까지 보유.",
            "structure": f"고점 ${peak:.2f}" + (f" / D-{d_day_n}" if d_day_n is not None else ""),
        }
    d_txt = f"D-{d_day_n} " if d_day_n is not None else ""
    return {
        **base, "code": "HOLD",
        "badge": "🟢 [순항]",
        "order": f"{d_txt}보유 중 (P&L {pct:+.1f}%). 헌법 위반 없음.",
        "structure": f"{hold}거래일 보유",
    }

def lead_guardian(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for row in rows:
        item = guardian(row)
        if best is None or int(item["rank"]) < int(best["rank"]):
            best = item
    return best
