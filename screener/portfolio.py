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
    strat = str(strategy or "RUNUP").upper().strip()
    if strat not in ("PEAD", "RUNUP", "RSI2"):
        raise ValueError("전략은 PEAD, RUNUP, RSI2")

    edate, date_src = "", ""
    row_e: dict[str, Any] | None = None
    typed = str(earnings_date or "").strip()
    if typed:
        if ed.days_to_event(typed) is None:
            raise ValueError("실적 발표일 형식은 YYYY-MM-DD")
        edate, date_src = typed[:10], "MANUAL"
    else:
        pool = SHARED.earnings_targets if strat in ("RUNUP", "PEAD") else []
        with SHARED._lock:
            for r in pool:
                if str(r.get("ticker") or "").upper() == tk:
                    row_e = dict(r)
                    break
        if row_e and row_e.get("earnings_date"):
            edate, date_src = str(row_e["earnings_date"])[:10], "CAL"
        elif strat == "RUNUP":
            raise ValueError("런업 종목은 실적 발표일이 필수입니다 (표에 없으면 직접 입력)")
        else:
            try:
                found = ed.next_earnings_date(tk)
            except Exception:  # noqa: BLE001
                found = ""
            if found:
                edate, date_src = found, "AUTO"

    if edate and strat == "RUNUP":
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
    sl_pct = {"PEAD": config.PEAD_SL_PCT, "RUNUP": config.RUNUP_SL_PCT, "RSI2": config.RSI2_SL_PCT}[strat]
    stop = round(entry * (1.0 - float(sl_pct) / 100.0), 2)

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
    strategy = str(pos.get("strategy") or "").upper().strip() or "LEGACY"
    e_raw = str(pos.get("earnings_date") or "").strip()
    d_day = ed.days_to_event(e_raw) if e_raw else None
    sma5 = None
    sma5_recaptured = False
    if strategy == "RSI2" and has_quote and px:
        sma5 = _cached_sma5(ticker)
        sma5_recaptured = bool(sma5 and px >= sma5)
    return {
        "ticker": ticker, "strategy": strategy, "entry_price": entry, "shares": shares,
        "stop_price": stop, "entry_date": raw_date, "target_consensus": consensus,
        "buy_ratio": buy_ratio, "current_price": px,
        "one_r": one_r, "r_mult": r_mult,
        "pct": pct, "pnl": pnl, "pnl_pct": pct,
        "upside": ((consensus - px) / px * 100.0) if consensus > 0 and px > 0 else 0.0,
        "has_quote": has_quote, "earnings_date": e_raw,
        "d_day": d_day,
        "force_exit_d3": ed.must_exit_for_earnings(e_raw) if e_raw else False,
        "force_exit_runup": bool(strategy == "RUNUP" and d_day is not None and d_day <= int(config.RUNUP_FORCE_EXIT_DDAY)),
        "hold_bdays": ed.business_days_held(raw_date),
        "sma5": sma5,
        "sma5_recaptured": sma5_recaptured,
        "peak_updated": peak_updated,
    }


_PEAKS: dict[str, float] = {}
_SMA5_CACHE: dict[str, tuple[float, float | None]] = {}


def _cached_sma5(ticker: str) -> float | None:
    """5일 평균. 1초 루프마다 FMP를 두드리지 않도록 1시간 캐시."""
    now = time.time()
    hit = _SMA5_CACHE.get(ticker)
    if hit and now - hit[0] < 3600:
        return hit[1]
    sma: float | None = None
    try:
        from data.fmp import historical_daily
        df = historical_daily(ticker)
        if df is not None and not getattr(df, "empty", True) and "Close" in df.columns and len(df) >= 5:
            sma = float(df["Close"].tail(5).mean())
    except Exception:  # noqa: BLE001
        sma = None
    _SMA5_CACHE[ticker] = (now, sma)
    return sma


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


def _guardian_code(m: dict[str, Any]) -> str:
    """PEAD / RUNUP / RSI2 판정. 그 외는 LEGACY."""
    strategy = str(m.get("strategy") or "")
    if strategy == "PEAD":
        return _guardian_pead(m)
    if strategy == "RUNUP":
        return _guardian_runup(m)
    if strategy == "RSI2":
        return _guardian_rsi2(m)
    return "LEGACY"


def _guardian_pead(m: dict[str, Any]) -> str:
    pnl_pct = _pnl_pct(m)
    if pnl_pct <= -float(config.PEAD_SL_PCT):
        return "SL"
    if int(m.get("hold_bdays") or 0) >= int(config.PEAD_MAX_HOLD_BDAYS):
        return "TIME_EXIT"
    if not m.get("has_quote"):
        return "NO_QUOTE"
    return "HOLD"


def _guardian_runup(m: dict[str, Any]) -> str:
    pnl_pct = _pnl_pct(m)
    if pnl_pct <= -float(config.RUNUP_SL_PCT):
        return "SL"
    if m.get("force_exit_runup"):
        return "D2_EXIT"
    if not m.get("has_quote"):
        return "NO_QUOTE"
    return "HOLD"


def _guardian_rsi2(m: dict[str, Any]) -> str:
    pnl_pct = _pnl_pct(m)
    if pnl_pct <= -float(config.RSI2_SL_PCT):
        return "SL"
    if m.get("sma5_recaptured"):
        return "SMA5_EXIT"
    if int(m.get("hold_bdays") or 0) >= int(config.RSI2_MAX_HOLD_BDAYS):
        return "TIME_EXIT"
    if not m.get("has_quote"):
        return "NO_QUOTE"
    return "HOLD"


_GUARD_RANK = {
    "SL": 0, "D2_EXIT": 1, "SMA5_EXIT": 1, "TIME_EXIT": 2, "NO_QUOTE": 3,
    "HOLD": 9, "LEGACY": 9,
}
_GUARD_COPY = {
    "SL": ("error", "🔴 [손절선 터치]", "손절선 터치. 토스에서 전량 매도하십시오."),
    "TIME_EXIT": ("warning", "⏰ [보유기한 만기]", "보유 기한이 찼다. 토스에서 전량 매도하십시오."),
    "D2_EXIT": ("error", "🚨 [런업 D-2 강제청산]", "실적 D-2 이내다. 발표 전에 전량 매도하십시오."),
    "SMA5_EXIT": ("success", "🟡 [5일선 재돌파]", "RSI2 5일선을 되찾았다. 토스에서 익절하십시오."),
    "NO_QUOTE": ("error", "⛔ [시세 끊김]", "시세가 없다. 토스에서 직접 확인하십시오."),
    "HOLD": ("info", "🟢 [순항]", "순항. 매도 타이밍은 AI 감리가 판단한다."),
    "LEGACY": ("warning", "🔍 [LEGACY]", "폐기된 전략이다. 자동판정 없음. 수동으로 정리하십시오."),
}


def guardian(m: dict[str, Any]) -> dict[str, Any]:
    """화면은 dict를 그린다. code 값은 PEAD/RUNUP/RSI2 헌법의 문자열이다."""
    code = _guardian_code(m)
    alert, badge, order = _GUARD_COPY.get(code, _GUARD_COPY["LEGACY"])
    hold = int(m.get("hold_bdays") or 0)
    d_day = m.get("d_day")
    structure = f"실적 D-{int(d_day)}" if d_day is not None else f"{hold}거래일 보유"
    if code == "SMA5_EXIT" and m.get("sma5"):
        structure = f"SMA5 ${float(m['sma5']):.2f}"
    return {
        "ticker": m.get("ticker", ""),
        "code": code,
        "rank": _GUARD_RANK.get(code, 9),
        "alert": alert,
        "badge": badge,
        "order": order,
        "days": hold,
        "structure": structure,
        "r_mult": float(m.get("r_mult") or 0),
        "pct": _pnl_pct(m),
    }


def lead_guardian(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for row in rows:
        item = guardian(row)
        if best is None or int(item["rank"]) < int(best["rank"]):
            best = item
    return best
