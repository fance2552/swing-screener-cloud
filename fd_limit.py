"""Raise macOS default 256-FD cap before Streamlit opens sockets."""
from __future__ import annotations

import resource


def raise_limit(n: int = 10240) -> int:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    cap = hard if hard > 0 else n
    target = min(max(n, soft), cap)
    if target > soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, cap if cap >= target else target))
        except (ValueError, OSError):
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, target))
            except (ValueError, OSError):
                return soft
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


raise_limit()
