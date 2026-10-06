"""Always hit the real Terminal, even when Streamlit swallows stdout."""
from __future__ import annotations

import sys
from datetime import datetime


def slog(msg: str) -> None:
    line = f"[LDPB {datetime.now().strftime('%H:%M:%S')}] {msg}\n"
    _write(line)


def slog_alpaca(ticker: str, price: float, tag: str) -> None:
    _write(f"[LDPB 실시간 Alpaca] {ticker}: ${price:.2f} ({tag})\n")


def _write(line: str) -> None:
    try:
        sys.__stderr__.write(line)
        sys.__stderr__.flush()
    except Exception:  # noqa: BLE001
        print(line, end="", flush=True)
