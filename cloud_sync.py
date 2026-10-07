"""portfolio.json / closed_trades.jsonl ⇄ 비공개 GitHub Gist.

- pull: 파일마다 프로세스당 1회. 실패하면 60초 간격으로만 재시도한다.
- push: 그 파일을 Gist에서 한 번이라도 확인한 뒤에만 올린다. 확인 전에 올리면
  재시작으로 비어 있는 로컬이 Gist의 보유 목록을 덮어쓴다.
- peak_price 틱 저장은 여기로 오지 않는다. 진입/청산만 올라간다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import requests

GIST_API = "https://api.github.com/gists"
_TIMEOUT = 5
_RETRY_SEC = 60.0

_LOCK = threading.RLock()
_PULLED: set[str] = set()
_ABSENT: set[str] = set()
_LAST_PULL_TRY: dict[str, float] = {}
_LAST_PULL_ERR: dict[str, str] = {}
_PUSH_DIRTY: set[str] = set()
_LAST_PUSH_TRY: dict[str, float] = {}
_PUSH_PATH: dict[str, Path] = {}


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


def _mark_pulled(filename: str) -> None:
    _PULLED.add(filename)
    _LAST_PULL_ERR.pop(filename, None)


def pull_file_from_cloud(local_path: Path, filename_in_gist: str) -> str:
    """Gist → 로컬. "" = 정상 또는 불필요. 그 외는 아직 복원 못 한 이유."""
    if not is_cloud_sync_enabled():
        return ""
    name = filename_in_gist
    with _LOCK:
        if name in _PULLED:
            _retry_push_if_dirty(name)
            return ""
        if local_path.exists() and local_path.stat().st_size > 2:
            _mark_pulled(name)
            return ""
        now = time.time()
        err = _LAST_PULL_ERR.get(name, "")
        if err and now - _LAST_PULL_TRY.get(name, 0.0) < _RETRY_SEC:
            return err
        _LAST_PULL_TRY[name] = now
        try:
            resp = requests.get(
                f"{GIST_API}/{_secret('GIST_ID')}",
                headers=_headers(),
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            if name not in files:
                if name == "portfolio.json":
                    raise KeyError(f"Gist에 {name} 파일이 없음")
                _ABSENT.add(name)
                _mark_pulled(name)
                return ""
            content = files[name].get("content") or ""
            if name == "portfolio.json":
                if not isinstance(json.loads(content or "[]"), list):
                    raise ValueError("Gist의 portfolio.json 이 리스트가 아님")
                content = content or "[]"
            tmp = local_path.with_name(f"{local_path.name}.gist.{os.getpid()}.tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, local_path)
            _ABSENT.discard(name)
            _mark_pulled(name)
            print(f"[cloud_sync] Gist에서 {name} 복원 ({len(content)} bytes)")
            return ""
        except Exception as exc:  # noqa: BLE001
            _LAST_PULL_ERR[name] = f"{type(exc).__name__}: {exc}"
            print(f"[cloud_sync] pull({name}) 실패 — {int(_RETRY_SEC)}초 뒤 재시도: {_LAST_PULL_ERR[name]}")
            return _LAST_PULL_ERR[name]


def push_file_to_cloud(local_path: Path, filename_in_gist: str) -> None:
    if not is_cloud_sync_enabled() or not local_path.exists():
        return
    name = filename_in_gist
    with _LOCK:
        _PUSH_PATH[name] = local_path
        if name not in _PULLED and name not in _ABSENT:
            _PUSH_DIRTY.add(name)
            print(f"[cloud_sync] push({name}) 보류 — 아직 Gist를 확인하지 못해 덮어쓰지 않음")
            return
        _LAST_PUSH_TRY[name] = time.time()
        try:
            resp = requests.patch(
                f"{GIST_API}/{_secret('GIST_ID')}",
                headers=_headers(),
                json={"files": {name: {"content": local_path.read_text(encoding="utf-8")}}},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            _PUSH_DIRTY.discard(name)
            _ABSENT.discard(name)
        except Exception as exc:  # noqa: BLE001
            _PUSH_DIRTY.add(name)
            print(f"[cloud_sync] push({name}) 실패 — 다음 쓰기 때 재시도됨: {type(exc).__name__}: {exc}")


def _retry_push_if_dirty(name: str) -> None:
    if name in _PUSH_DIRTY and time.time() - _LAST_PUSH_TRY.get(name, 0.0) >= _RETRY_SEC:
        path = _PUSH_PATH.get(name)
        if path is not None:
            push_file_to_cloud(path, name)


def pull_portfolio_from_cloud(local_path: Path) -> str:
    return pull_file_from_cloud(local_path, "portfolio.json")


def push_portfolio_to_cloud(local_path: Path) -> None:
    push_file_to_cloud(local_path, "portfolio.json")


def pull_closed_trades_from_cloud(local_path: Path) -> str:
    return pull_file_from_cloud(local_path, "closed_trades.jsonl")


def push_closed_trades_to_cloud(local_path: Path) -> None:
    push_file_to_cloud(local_path, "closed_trades.jsonl")
