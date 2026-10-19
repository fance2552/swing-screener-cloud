"""앱 버전 표시. 업데이트할 때마다 APP_VERSION 과 RELEASED_AT(KST)을 함께 올린다."""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

APP_VERSION = "v4.4.1"
RELEASED_AT = "2026-10-19 17:48 KST"


@lru_cache(maxsize=1)
def deployed_commit() -> str:
    """배포된 깃 커밋 해시(짧은 형식). 깃이 없으면 빈 문자열."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=3,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def version_label() -> str:
    commit = deployed_commit()
    tail = f" · {commit}" if commit else ""
    return f"{APP_VERSION} · 업데이트 {RELEASED_AT}{tail}"
