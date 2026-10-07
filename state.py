"""Process-wide cockpit state. Scan writes here; UI only reads."""
from __future__ import annotations

import threading
import time
from typing import Any

import pandas as pd


class SharedState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.scan_running = False
        self.is_scanning = False
        self.scan_phase = "IDLE"
        self.scan_error = ""
        self.banner = "대기. 스캔 시작을 누르십시오."
        self.universe_n = 0
        self.scored_n = 0

        # LEGACY (LDPB, unused) — kept so nothing that still references it explodes.
        self.targets: list[dict[str, Any]] = []
        self.ohlcv: dict[str, pd.DataFrame] = {}
        self.bars: dict[str, pd.DataFrame] = self.ohlcv
        self.selected: str = ""

        # Tab 1 — event-driven scanners
        self.earnings_targets: list[dict[str, Any]] = []
        self.earnings_note = ""
        self.earnings_ts = 0.0
        self.squeeze_targets: list[dict[str, Any]] = []
        self.squeeze_note = ""
        self.squeeze_ts = 0.0

        self.regime = {"color": "YELLOW", "label": "UNKNOWN", "detail": ""}
        self.fmp_ok = False
        self.alpaca_ok = False
        self.last_scan_ts = 0.0
        self.price_gen = 0
        self.timeframe = "1d"
        self.scan_done = 0
        self.scan_total = 0
        self.scan_progress = 0.0
        self.scan_status_text = ""
        self.live_quotes: dict[str, dict] = {}
        now = time.time()
        self.fmp_tick_count = 0
        self.fmp_last_time = now
        self.alpaca_tick_count = 0
        self.alpaca_last_time = now

        # Fail-closed portfolio I/O. Empty string = disk is readable.
        self.portfolio_error = ""
        self.portfolio_corrupt = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "scan_running": self.scan_running,
                "is_scanning": self.is_scanning,
                "price_gen": self.price_gen,
                "scan_phase": self.scan_phase,
                "scan_error": self.scan_error,
                "banner": self.banner,
                "universe_n": self.universe_n,
                "scored_n": self.scored_n,
                "targets": [dict(r) for r in self.targets],
                "earnings_targets": [dict(r) for r in self.earnings_targets],
                "earnings_note": self.earnings_note,
                "squeeze_targets": [dict(r) for r in self.squeeze_targets],
                "squeeze_note": self.squeeze_note,
                "selected": self.selected,
                "regime": dict(self.regime),
                "fmp_ok": self.fmp_ok,
                "alpaca_ok": self.alpaca_ok,
                "last_scan_ts": self.last_scan_ts,
                "timeframe": self.timeframe,
                "scan_done": self.scan_done,
                "scan_total": self.scan_total,
                "scan_progress": self.scan_progress,
                "scan_status_text": self.scan_status_text,
                "live_quotes": dict(self.live_quotes),
                "portfolio_error": self.portfolio_error,
                "portfolio_corrupt": self.portfolio_corrupt,
            }

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.scan_phase = phase

    def set_scan_progress(self, progress: float, text: str | None = None) -> None:
        with self._lock:
            self.scan_progress = max(0.0, min(1.0, float(progress)))
            if text is not None:
                self.scan_status_text = text

    def set_banner(self, text: str) -> None:
        with self._lock:
            self.banner = text

    def set_earnings_targets(self, rows: list[dict[str, Any]], note: str = "") -> None:
        with self._lock:
            self.earnings_targets = [dict(r) for r in rows]
            self.earnings_note = str(note or "")
            self.earnings_ts = time.time()

    def set_squeeze_targets(self, rows: list[dict[str, Any]], note: str = "") -> None:
        with self._lock:
            self.squeeze_targets = [dict(r) for r in rows]
            self.squeeze_note = str(note or "")
            self.squeeze_ts = time.time()

    def set_portfolio_health(self, error: str = "", corrupt: bool = False) -> None:
        with self._lock:
            self.portfolio_error = str(error or "")
            self.portfolio_corrupt = bool(corrupt)

    def note_feed(self, feed: str, tick: bool = True) -> None:
        now = time.time()
        with self._lock:
            if feed == "fmp":
                if tick:
                    self.fmp_tick_count += 1
                self.fmp_last_time = now
            elif feed == "alpaca":
                if tick:
                    self.alpaca_tick_count += 1
                self.alpaca_last_time = now

    def telemetry(self) -> dict[str, Any]:
        with self._lock:
            return {
                "fmp_tick_count": int(self.fmp_tick_count),
                "fmp_last_time": float(self.fmp_last_time),
                "alpaca_tick_count": int(self.alpaca_tick_count),
                "alpaca_last_time": float(self.alpaca_last_time),
                "regime": dict(self.regime),
                "banner": self.banner,
                "scan_phase": self.scan_phase,
                "scan_running": self.scan_running,
                "scan_progress": self.scan_progress,
                "scan_status_text": self.scan_status_text,
                "portfolio_error": self.portfolio_error,
                "portfolio_corrupt": self.portfolio_corrupt,
            }

    def quote(self, ticker: str) -> float:
        with self._lock:
            row = self.live_quotes.get(str(ticker).upper().strip())
            if isinstance(row, dict):
                raw = row.get("price")
            else:
                raw = row
        try:
            return float(raw or 0)
        except (TypeError, ValueError):
            return 0.0


SHARED = SharedState()
