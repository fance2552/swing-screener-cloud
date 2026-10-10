"""PEAD / 런업 압축 / RSI2 스캔 + 실시간 레이더. No broker send. No LDPB."""
from __future__ import annotations

import threading
import time
from typing import Any

import pandas as pd

import config
import env_settings
from data import alpaca_feed, fmp
from data.yf_download import download_daily, spy as load_spy, wikipedia_universe
from screener import event_driven, portfolio, regime
from slog import slog
from state import SHARED

_SCAN_THREAD: threading.Thread | None = None
_SCAN_THREAD_LOCK = threading.Lock()


def healthcheck() -> None:
    SHARED.fmp_ok = fmp.ping()
    SHARED.alpaca_ok = alpaca_feed.ping()
    slog(f"ping FMP={SHARED.fmp_ok} ALPACA={SHARED.alpaca_ok}")


def _universe() -> list[dict[str, Any]]:
    SHARED.set_phase("UNIVERSE")
    SHARED.set_banner("FMP 공용 유니버스 확장 중…")
    try:
        rows = event_driven.fetch_universe()
        if not rows:
            raise RuntimeError("빈 유니버스")
        SHARED.fmp_ok = True
        return rows
    except Exception as exc:  # noqa: BLE001
        SHARED.scan_error = f"FMP screener 폴백: {exc}"
        SHARED.fmp_ok = False
        rows = wikipedia_universe()
        slog(f"FMP 실패 → 위키폴백 {len(rows)}개 ({exc})")
        return rows


def get_active_watchlist() -> list[str]:
    """PEAD ∪ 런업 ∪ RSI2 ∪ 보유. 매 폴링마다 재계산."""
    snap = SHARED.snapshot()
    seen: set[str] = set()
    out: list[str] = []
    pool = snap.get("hunting_pool") if isinstance(snap.get("hunting_pool"), dict) else {}
    pools = [pool.get("pead"), pool.get("runup"), pool.get("rsi2"), snap.get("earnings_targets")]
    for rows in pools:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            tk = str(row.get("ticker") or "").upper().strip()
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)
    try:
        held = [str(p.get("ticker") or "").upper().strip() for p in portfolio.load_portfolio()]
    except Exception:  # noqa: BLE001
        held = []
    for tk in held:
        if tk and tk not in seen:
            seen.add(tk)
            out.append(tk)
    if "KRW=X" not in seen:
        out.append("KRW=X")
    return out


def ensure_realtime() -> None:
    if not isinstance(SHARED.live_quotes, dict):
        SHARED.live_quotes = {}
    from data_feed import start_dual_realtime_engine

    start_dual_realtime_engine(get_active_watchlist, SHARED.live_quotes, SHARED._lock)


def start_heartbeat() -> None:
    SHARED.is_scanning = True
    ensure_realtime()


def ensure_heartbeat() -> None:
    ensure_realtime()


def _claim_scan() -> bool:
    with SHARED._lock:
        if SHARED.scan_running:
            return False
        SHARED.scan_running = True
        return True


def run_scan() -> None:
    """하위 호환. 헤비 스캔과 같다."""
    build_hunting_pool()


def build_hunting_pool() -> None:
    """PEAD / 런업 / RSI2를 따로 스캔한다. 하나 실패해도 나머지는 풀에 남긴다."""
    from screener import pead, pricing, rsi2

    if not _claim_scan():
        slog("스캔 이미 진행 중. 중복 실행 거부.")
        SHARED.heavy_scan_running = bool(SHARED.scan_running)
        return
    if not env_settings.api_keys_ready():
        SHARED.scan_error = "FMP_API_KEY / ALPACA 키 없음."
        SHARED.set_phase("NO_KEYS")
        SHARED.set_banner("키가 없다. 사이드바에 입력.")
        SHARED.scan_running = False
        SHARED.heavy_scan_running = False
        return

    SHARED.is_scanning = False
    SHARED.heavy_scan_running = True
    SHARED.scan_error = ""
    SHARED.scan_errors = []
    SHARED.set_phase("START")
    SHARED.set_scan_progress(0.05, "사냥터 구축")
    SHARED.heavy_scan_progress = 0.05
    SHARED.set_banner("사냥터 구축…")
    pool: dict[str, list] = {"pead": [], "runup": [], "rsi2": []}
    scan_errors: list[str] = []
    try:
        slog("헤비 스캔 시작")
        try:
            healthcheck()
        except Exception as exc:  # noqa: BLE001
            scan_errors.append(f"연결 점검 실패: {exc}")
            slog(f"연결 점검 실패 {exc}")

        try:
            SHARED.set_phase("PEAD")
            SHARED.set_scan_progress(0.15, "PEAD 스캔")
            SHARED.heavy_scan_progress = 0.15
            raw_pead = list(pead.scan_pead(limit=7) or [])
            pool["pead"] = pricing.enrich_candidates(raw_pead, "PEAD")
            slog(f"PEAD {len(pool['pead'])}")
        except Exception as exc:  # noqa: BLE001
            scan_errors.append(f"PEAD 스캔 실패: {exc}")
            slog(f"PEAD 스캔 실패 {exc}")

        try:
            SHARED.set_phase("RUNUP")
            SHARED.set_scan_progress(0.40, "런업 일봉")
            SHARED.heavy_scan_progress = 0.40
            rows = _universe()
            SHARED.universe_n = len(rows)
            meta = {str(r.get("symbol") or "").upper(): r for r in rows if r.get("symbol")}
            tickers = [t for t in meta if t]
            if "SPY" not in tickers:
                tickers.append("SPY")
            SHARED.scan_total = len(tickers)
            hist = download_daily(tickers)
            slog(f"일봉 확보 {len(hist)}")
            spy_df = hist.get("SPY")
            if spy_df is None or getattr(spy_df, "empty", True):
                spy_df = load_spy()
            SHARED.regime = regime.get_market_regime(spy_df)
            earn_top, earn_note, cal = event_driven.scan_earnings(rows, hist)
            raw_runup = list(earn_top or [])[:7]
            pool["runup"] = pricing.enrich_candidates(raw_runup, "RUNUP")
            SHARED.set_earnings_targets(pool["runup"], earn_note)
            if cal:
                portfolio.sync_earnings_dates(cal)
            slog(f"런업 압축 {len(pool['runup'])}")
        except Exception as exc:  # noqa: BLE001
            scan_errors.append(f"RUNUP 스캔 실패: {exc}")
            slog(f"RUNUP 스캔 실패 {exc}")

        try:
            SHARED.set_phase("RSI2")
            SHARED.set_scan_progress(0.75, "RSI2 스캔")
            SHARED.heavy_scan_progress = 0.75
            raw_rsi2 = list(rsi2.scan_rsi2(limit=7) or [])
            pool["rsi2"] = pricing.enrich_candidates(raw_rsi2, "RSI2")
            slog(f"RSI2 {len(pool['rsi2'])}")
        except Exception as exc:  # noqa: BLE001
            scan_errors.append(f"RSI2 스캔 실패: {exc}")
            slog(f"RSI2 스캔 실패 {exc}")

        SHARED.heavy_scan_done_ts = time.time()
        ensure_realtime()
        SHARED.set_phase("LIVE")
        SHARED.set_scan_progress(1.0, "스캔 완료")
        SHARED.heavy_scan_progress = 1.0
        fail_txt = f" · 실패 {len(scan_errors)}" if scan_errors else ""
        SHARED.scan_error = " | ".join(scan_errors)
        SHARED.set_banner(
            f"🟢 PEAD {len(pool['pead'])} · 런업 {len(pool['runup'])} · RSI2 {len(pool['rsi2'])}{fail_txt}"
        )
    finally:
        SHARED.hunting_pool = pool
        SHARED.hunting_pool_updated_ts = time.time()
        SHARED.scan_errors = list(scan_errors)
        SHARED.scan_running = False
        SHARED.heavy_scan_running = False
        SHARED.set_scan_progress(0.0, "")
        SHARED.heavy_scan_progress = 0.0
        ensure_realtime()
        SHARED.is_scanning = bool(pool.get("pead") or pool.get("runup") or pool.get("rsi2"))


def refresh_pool_snapshot() -> None:
    """런업만 라이브 시세로 다시 줄 세운다. PEAD/RSI2 는 일봉 점수 그대로."""
    raw = SHARED.hunting_pool if isinstance(SHARED.hunting_pool, dict) else {}
    pool = {
        "pead": list(raw.get("pead") or []),
        "runup": list(raw.get("runup") or []),
        "rsi2": list(raw.get("rsi2") or []),
    }
    if pool["runup"]:
        from screener import pricing

        lo = int(config.RUNUP_ENTRY_DDAY_MIN)
        hi = int(config.RUNUP_ENTRY_DDAY_MAX)
        ranked = event_driven.earn_rank_live(pool["runup"], SHARED.quote)
        kept = [
            row for row in ranked
            if lo <= int(row.get("d_day") if row.get("d_day") is not None else -999) <= hi
        ][:7]
        pool["runup"] = pricing.enrich_candidates(kept, "RUNUP")
        SHARED.set_earnings_targets(pool["runup"], SHARED.earnings_note)
    SHARED.hunting_pool = pool
    SHARED.hunting_pool_updated_ts = time.time()
    SHARED.last_quick_scan_ts = time.time()
    slog(f"퀵 스캔 런업 {len(pool['runup'])}")


def start_scan() -> None:
    run_scan()


def start_scan_async() -> bool:
    """UI 스레드를 막지 않는다. True = 스레드 기동. False = 이미 돌거나 키 없음."""
    global _SCAN_THREAD
    if not env_settings.api_keys_ready():
        SHARED.scan_error = "FMP_API_KEY / ALPACA 키 없음."
        SHARED.set_phase("NO_KEYS")
        SHARED.set_banner("키가 없다. 사이드바에 입력.")
        return False
    with _SCAN_THREAD_LOCK:
        if SHARED.scan_running:
            return False
        alive = _SCAN_THREAD is not None and _SCAN_THREAD.is_alive()
        if alive:
            return False
        SHARED.heavy_scan_running = True
        _SCAN_THREAD = threading.Thread(target=build_hunting_pool, daemon=True, name="event-scan")
        _SCAN_THREAD.start()
    return True


def stop_scan() -> None:
    SHARED.is_scanning = False
    SHARED.set_phase("STOPPED")
    SHARED.set_banner("레이더 중지 요청. 시세 피드는 유지. 진행 중 스캔은 이번 주기 끝까지 간다.")
    slog("레이더 중지")
