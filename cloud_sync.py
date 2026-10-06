"""portfolio.json ⇄ 비공개 GitHub Gist 미러. Streamlit Community Cloud는 재시작 시 디스크가 초기화된다.

- pull: 프로세스당 1회. 실패하면 60초 간격으로만 재시도한다(1초 화면 루프가 GitHub를 두드리지 않게).
- push: Gist를 한 번이라도 정상으로 받은 뒤에만 올린다. 못 받은 상태에서 올리면
  Gist의 기존 보유 목록을 빈 목록/부분 목록으로 덮어써 영구 손실이 난다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import requests

GIST_API = "https://api.github.com/gists"
FILENAME_IN_GIST = "portfolio.json"
_TIMEOUT = 5
_RETRY_SEC = 60.0

_LOCK = threading.RLock()
_PULLED = False
_LAST_PULL_TRY = 0.0
_LAST_PULL_ERR = ""
_PUSH_DIRTY = False
_LAST_PUSH_TRY = 0.0


def _secret(name: str) -> str:
    try:
        import streamlit as st
        val = st.secrets.get(name)
        if val:
            return str(val).strip()
    except Exception:
        pass
    return (os.environ.get(name) or "").strip()


def is_cloud_sync_enabled() -> bool:
    return bool(_secret("GIST_ID") and _secret("GITHUB_PAT"))


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_secret('GITHUB_PAT')}",
        "Accept": "application/vnd.github+json",
    }


def pull_portfolio_from_cloud(local_path: Path) -> str:
    """Gist → 로컬. 반환값 "" = 정상/불필요, 그 외 = 아직 복원 못 한 이유."""
    global _PULLED, _LAST_PULL_TRY, _LAST_PULL_ERR
    if not is_cloud_sync_enabled():
        return ""
    with _LOCK:
        if _PULLED:
            _retry_push_if_dirty(local_path)
            return ""
        if local_path.exists() and local_path.stat().st_size > 2:
            _PULLED = True
            return ""
        now = time.time()
        if _LAST_PULL_ERR and now - _LAST_PULL_TRY < _RETRY_SEC:
            return _LAST_PULL_ERR
        _LAST_PULL_TRY = now
        try:
            resp = requests.get(f"{GIST_API}/{_secret('GIST_ID')}", headers=_headers(), timeout=_TIMEOUT)
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            if FILENAME_IN_GIST not in files:
                raise KeyError(f"Gist에 {FILENAME_IN_GIST} 파일이 없음")
            content = files[FILENAME_IN_GIST].get("content") or "[]"
            if not isinstance(json.loads(content), list):
                raise ValueError("Gist의 portfolio.json 이 리스트가 아님")
            tmp = local_path.with_name(f"{local_path.name}.gist.{os.getpid()}.tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, local_path)
            _PULLED = True
            _LAST_PULL_ERR = ""
            print(f"[cloud_sync] Gist에서 portfolio.json 복원 ({len(content)} bytes)")
            return ""
        except Exception as exc:  # noqa: BLE001
            _LAST_PULL_ERR = f"{type(exc).__name__}: {exc}"
            print(f"[cloud_sync] pull 실패 — {int(_RETRY_SEC)}초 뒤 재시도: {_LAST_PULL_ERR}")
            return _LAST_PULL_ERR


def _send(local_path: Path) -> bool:
    resp = requests.patch(
        f"{GIST_API}/{_secret('GIST_ID')}",
        headers=_headers(),
        json={"files": {FILENAME_IN_GIST: {"content": local_path.read_text(encoding="utf-8")}}},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return True


def push_portfolio_to_cloud(local_path: Path) -> None:
    global _PUSH_DIRTY, _LAST_PUSH_TRY
    if not is_cloud_sync_enabled() or not local_path.exists():
        return
    with _LOCK:
        if not _PULLED:
            _PUSH_DIRTY = True
            print("[cloud_sync] push 보류 — 아직 Gist를 정상 복원하지 못해 덮어쓰지 않음")
            return
        _LAST_PUSH_TRY = time.time()
        try:
            _send(local_path)
            _PUSH_DIRTY = False
        except Exception as exc:  # noqa: BLE001
            _PUSH_DIRTY = True
            print(f"[cloud_sync] push 실패 — 다음 로드 때 재시도: {type(exc).__name__}: {exc}")


def _retry_push_if_dirty(local_path: Path) -> None:
    if _PUSH_DIRTY and time.time() - _LAST_PUSH_TRY >= _RETRY_SEC:
        push_portfolio_to_cloud(local_path)
