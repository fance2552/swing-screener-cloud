"""portfolio.json 관리 + 4대 청산 헌법 (FRIDAY FLAT / TP / SL / D-3). No LDPB."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import cloud_sync
import config
from data.fmp import fetch_fmp_analyst_data
from screener import event_driven as ed
from state import SHARED

PORTFOLIO_PATH = Path(__file__).resolve().parent.parent / "portfolio.json"
BACKUP_PATH = PORTFOLIO_PATH.with_name("portfolio.json.bak")
_PF_LOCK = threading.RLock()
_CACHE: list[dict[str, Any]] = []
_CACHE_OK = False


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


def _log_closed(pos: dict[str, Any]) -> None:
    try:
        m = metrics(pos)
        rec = {
            "ticker": m["ticker"], "strategy": m["strategy"],
            "entry_price": m["entry_price"], "entry_date": m["entry_date"],
            "exit_date": ed.today_et().isoformat(), "exit_mark": m["current_price"],
            "pct": round(float(m["pct"]), 3), "r_mult": round(float(m["r_mult"]), 3),
        }
        path = PORTFOLIO_PATH.with_name("closed_trades.jsonl")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:  # noqa: BLE001
        pass


def remove_ticker(ticker: str) -> None:
    tk = (ticker or "").upper().strip()
    with _PF_LOCK:
        rows = _rows_for_write()
        for p in rows:
            if str(p.get("ticker") or "").upper() == tk:
                _log_closed(p)
        save_portfolio([p for p in rows if str(p.get("ticker") or "").upper() != tk])


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
    one_r = max(entry - stop, 0.01)
    r_mult = (px - entry) / one_r if one_r and px > 0 else 0.0
    pct = ((px - entry) / entry) * 100.0 if entry and px > 0 else 0.0
    pnl = (px - entry) * shares if px > 0 else 0.0
    raw_date = str(pos.get("entry_date") or date.today().isoformat())
    strategy = str(pos.get("strategy") or "EARNINGS").upper().strip()
    if strategy not in ("EARNINGS", "SQUEEZE"):
        strategy = pos.get("strategy") or "LEGACY"
    e_raw = str(pos.get("earnings_date") or "").strip()
    return {
        "ticker": ticker, "strategy": strategy, "entry_price": entry, "shares": shares,
        "stop_price": stop, "entry_date": raw_date, "target_consensus": consensus,
        "buy_ratio": buy_ratio, "current_price": px, "one_r": one_r, "r_mult": r_mult,
        "pct": pct, "pnl": pnl,
        "upside": ((consensus - px) / px * 100.0) if consensus > 0 and px > 0 else 0.0,
        "has_quote": SHARED.quote(ticker) > 0, "earnings_date": e_raw,
        "d_day": ed.days_to_event(e_raw) if e_raw else None,
        "force_exit_d3": ed.must_exit_for_earnings(e_raw) if e_raw else False,
        "hold_bdays": ed.business_days_held(raw_date),
    }


def guardian(m: dict[str, Any]) -> dict[str, Any]:
    """4대 청산 헌법: FRIDAY FLAT / TP / SL / D-3, 순서는 config.EXIT_PRIORITY."""
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

    tk = str(m.get("ticker") or "")
    pct = float(m.get("pct") or 0)
    hold = int(m.get("hold_bdays") or 1)
    d_day = m.get("d_day")
    tp, sl = float(config.EXIT_TP_PCT), float(config.EXIT_SL_PCT)
    quoted = bool(m.get("has_quote"))
    friday = ed.is_friday_flat_window()
    force_d3 = bool(m.get("force_exit_d3"))

    rules: dict[str, dict[str, Any]] = {
        "FRIDAY": {
            "hit": friday,
            "code": "FRIDAY", "alert": "warning",
            "badge": "🟠 [FRIDAY FLAT]",
            "order": "금요일 마감이 가까워집니다. 주말 갭다운 리스크를 피하기 위해 지금 전량 시장가 매도하십시오.",
        },
        "TP": {
            "hit": quoted and pct >= tp,
            "code": "TP", "alert": "success",
            "badge": f"🟡 [목표 달성 익절 +{tp:.1f}%]",
            "order": f"목표 수익(+{tp:.1f}%) 달성! 욕심부리지 말고 지금 전량 시장가 익절하십시오.",
        },
        "SL": {
            "hit": quoted and pct <= -sl,
            "code": "SL", "alert": "error",
            "badge": f"🔴 [기계적 손절 -{sl:.1f}%]",
            "order": f"손절선(-{sl:.1f}%) 도달! 즉시 손절하여 손실을 최소화하십시오.",
        },
        "D3": {
            "hit": force_d3,
            "code": "D3", "alert": "error",
            "badge": "🚨 [D-3 강제 청산]",
            "order": "실적 발표 3일 전입니다! 어닝 갭 리스크를 피하기 위해 손익 무관 지금 전량 시장가 매도하십시오.",
        },
    }

    # rank는 config.EXIT_PRIORITY 안에서의 위치로 정한다(0이 가장 급함).
    # 서로 다른 종목에서 SL/D3/FRIDAY가 동시에 뜰 때 lead_guardian()이
    # 임의 순서(rows 순서)로 고르던 동순위 문제를 없앤다.
    for idx, key in enumerate(config.EXIT_PRIORITY):
        rule = rules.get(key)
        if rule and rule["hit"]:
            structure = f"실적 D-{int(d_day)}" if d_day is not None else f"{hold}거래일 보유"
            return {
                "ticker": tk, "code": rule["code"], "rank": idx, "alert": rule["alert"],
                "badge": rule["badge"], "order": rule["order"], "days": hold,
                "structure": structure, "r_mult": float(m.get("r_mult") or 0), "pct": pct,
            }

    if not quoted:
        return {
            "ticker": tk, "code": "NOQUOTE", "rank": 0, "alert": "error",
            "badge": "⛔ [시세 끊김]",
            "order": "시세 수신이 없습니다. 토스 앱에서 직접 확인하십시오.",
            "days": hold, "structure": "시세 없음", "r_mult": 0.0, "pct": 0.0,
        }

    when = f"D-{int(d_day)}일 전" if d_day is not None else "실적일 미상"
    order = f"현재 안전 구간입니다({when}). 목표(+{tp:.1f}%)/손절(-{sl:.1f}%)선 도달 전까지 편안히 홀딩하십시오."
    low_upside = (
        strategy == "SQUEEZE"
        and float(m.get("target_consensus") or 0) > 0
        and float(m.get("upside") or 0) < float(config.SQUEEZE_UPSIDE_MIN_PCT)
    )
    if low_upside:
        order += (
            f" ⚠️ 단, 월가 목표가 기준 상승여력이 {float(m.get('upside') or 0):+.1f}%로 "
            f"{config.SQUEEZE_UPSIDE_MIN_PCT:.0f}% 미만인 저탄력 구간입니다 — 수급상 숏커버링이 "
            "임박했더라도 스캘핑 관점으로만 짧게 보십시오."
        )
    return {
        "ticker": tk, "code": "HOLD", "rank": 6, "alert": "success",
        "badge": "🟢 [순항 홀딩]" if not low_upside else "🟡 [저탄력 순항]",
        "order": order,
        "days": hold, "structure": f"실적 {when}" if d_day is not None else f"{hold}거래일 보유",
        "r_mult": float(m.get("r_mult") or 0), "pct": pct,
    }


def lead_guardian(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for row in rows:
        item = guardian(row)
        if best is None or int(item["rank"]) < int(best["rank"]):
            best = item
    return best
