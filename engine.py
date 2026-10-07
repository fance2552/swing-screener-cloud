"""이벤트 드리븐 스캔(실적런업 + 숏스퀴즈) + 실시간 레이더 하트비트. No broker send. No LDPB."""
from __future__ import annotations

import threading
from typing import Any

import pandas as pd

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
    """실적런업 Top10 ∪ 숏스퀴즈 Top10 ∪ portfolio.json. 매 폴링마다 재계산."""
    snap = SHARED.snapshot()
    seen: set[str] = set()
    out: list[str] = []
    for pool_key in ("earnings_targets", "squeeze_targets"):
        for row in snap.get(pool_key) or []:
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
    """유니버스 → 일봉 → 실적런업 + 숏스퀴즈 동시 스캔 → 레이더 하트비트 유지.

    UI 스레드에서 호출하지 말 것. start_scan_async() 가 백그라운드로 돌린다.
    """
    if not _claim_scan():
        slog("스캔 이미 진행 중. 중복 실행 거부.")
        return
    if not env_settings.api_keys_ready():
        SHARED.scan_error = "FMP_API_KEY / ALPACA 키 없음."
        SHARED.set_phase("NO_KEYS")
        SHARED.set_banner("키가 없다. 사이드바에 입력.")
        SHARED.scan_running = False
        return

    SHARED.is_scanning = False
    SHARED.scan_error = ""
    SHARED.set_phase("START")
    SHARED.set_scan_progress(0.0, "스캔 시작")
    SHARED.set_banner("스캔 시작…")
    try:
        slog("스캔 시작")
        healthcheck()
        rows = _universe()
        SHARED.universe_n = len(rows)
        SHARED.set_scan_progress(0.25, "[1/4] 유니버스 필터링 완료")
        meta = {str(r.get("symbol") or "").upper(): r for r in rows if r.get("symbol")}
        tickers = [t for t in meta if t]
        if "SPY" not in tickers:
            tickers.append("SPY")
        SHARED.scan_total = len(tickers)
        SHARED.scan_done = 0
        SHARED.set_phase(f"EOD 0/{len(tickers)}")
        SHARED.set_scan_progress(0.50, "[2/4] EOD 일봉 데이터 로딩 및 백필")
        SHARED.set_banner(f"일봉 수신 {len(tickers)}종…")
        hist = download_daily(tickers)
        slog(f"일봉 확보 {len(hist)}")

        spy_df = hist.get("SPY")
        if spy_df is None or getattr(spy_df, "empty", True):
            spy_df = load_spy()
        SHARED.regime = regime.get_market_regime(spy_df)
        slog(
            f"국면 {SHARED.regime.get('state')} "
            f"분산 {SHARED.regime.get('distribution_days')} {SHARED.regime.get('detail')}"
        )

        SHARED.set_phase("EARNINGS")
        SHARED.set_scan_progress(0.75, "[3/4] 월가 컨센서스 및 숏 비율 조회")
        SHARED.set_banner("실적 런업 채점 중…")
        earn_top, earn_note, cal = event_driven.scan_earnings(rows, hist)
        SHARED.set_earnings_targets(earn_top, earn_note)
        changed = portfolio.sync_earnings_dates(cal) if cal else 0
        slog(f"실적 런업 Top {len(earn_top)} / 보유 실적일 갱신 {changed}")

        SHARED.set_phase("SQUEEZE")
        SHARED.set_scan_progress(0.90, "[4/4] 런업/스퀴즈 스코어링 및 Top 10 산출")
        SHARED.set_banner("숏스퀴즈 채점 중…")
        sqz_top, sqz_note = event_driven.scan_squeeze(rows, hist)
        SHARED.set_squeeze_targets(sqz_top, sqz_note)
        slog(f"숏스퀴즈 Top {len(sqz_top)}")

        watched = get_active_watchlist()
        slog(f"실시간 감시 {len(watched)}종: {', '.join(watched)}")
        ensure_realtime()
        SHARED.set_phase("LIVE")
        SHARED.set_scan_progress(1.0, "스캔 완료 및 레이더 가동")
        if not earn_top and not sqz_top:
            SHARED.scan_error = "실적런업/숏스퀴즈 모두 0건. 캘린더/공매도 데이터 확인."
            SHARED.set_banner("분석 완료. 표시 종목 0.")
        else:
            SHARED.scan_error = ""
            SHARED.set_banner(f"🟢 레이더 가동. 실적런업 {len(earn_top)} · 숏스퀴즈 {len(sqz_top)}.")
    except Exception as exc:  # noqa: BLE001
        slog(f"스캔 실패 {exc}")
        SHARED.scan_error = str(exc)
        SHARED.set_phase("ERROR")
        SHARED.set_banner(f"스캔 실패: {exc}")
    finally:
        SHARED.scan_running = False
        SHARED.set_scan_progress(0.0, "")
        ensure_realtime()
        SHARED.is_scanning = bool(SHARED.earnings_targets or SHARED.squeeze_targets)
        if not SHARED.is_scanning:
            slog("스캔 완료. 표시 종목 0.")


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
        _SCAN_THREAD = threading.Thread(target=run_scan, daemon=True, name="event-scan")
        _SCAN_THREAD.start()
    return True


def stop_scan() -> None:
    SHARED.is_scanning = False
    SHARED.set_phase("STOPPED")
    SHARED.set_banner("레이더 중지 요청. 시세 피드는 유지. 진행 중 스캔은 이번 주기 끝까지 간다.")
    slog("레이더 중지")
