"""`.env` API 키 — 사이드바 입력 또는 파일. load_dotenv 반복 호출 금지."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LEGACY_ENV = Path.home() / "Desktop" / "premarket-bot" / ".env"

API_KEY_NAMES: Tuple[str, ...] = (
    "FMP_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "GROQ_API_KEY",
)
# Groq 는 선택. 없어도 스캔은 돈다. api_keys_ready() 는 이 목록만 본다.
REQUIRED_API_KEY_NAMES: Tuple[str, ...] = (
    "FMP_API_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
)

_DEFAULT = """# LDPB Swing Screener .env — 사이드바 또는 이 파일. git 금지.

FMP_API_KEY=
FMP_BASE_URL=https://financialmodelingprep.com
FMP_POLL_INTERVAL_SEC=1.0

ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ALPACA_DATA_FEED=iex
"""

_LOADED = False


def ensure_env_file() -> Path:
    if not ENV_PATH.exists():
        try:
            ENV_PATH.write_text(_DEFAULT, encoding="utf-8")
        except OSError:
            return ENV_PATH
    return ENV_PATH


def read_env_map() -> Dict[str, str]:
    ensure_env_file()
    result: Dict[str, str] = {}
    if not ENV_PATH.exists():
        return result
    text = ENV_PATH.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def load() -> None:
    """디스크 → os.environ. 이미 로드됐으면 파일을 다시 열지 않는다."""
    global _LOADED
    if _LOADED:
        return
    disk = read_env_map()
    for key, value in disk.items():
        if key not in os.environ or not str(os.environ.get(key) or "").strip():
            os.environ[key] = value
    _LOADED = True


def _ensure() -> None:
    if not _LOADED:
        load()


def _streamlit_secrets() -> Dict[str, str]:
    try:
        import streamlit as st
        return dict(st.secrets)
    except Exception:
        return {}


def get_api_keys() -> Dict[str, str]:
    """우선순위: st.secrets(클라우드) → os.environ → .env(로컬)."""
    secrets = _streamlit_secrets()
    disk = read_env_map()
    out: Dict[str, str] = {}
    for name in API_KEY_NAMES:
        val = secrets.get(name) or os.environ.get(name) or disk.get(name) or ""
        out[name] = str(val).strip()
    return out


def missing_api_keys(values: Dict[str, str] | None = None) -> List[str]:
    vals = values if values is not None else get_api_keys()
    return [k for k in REQUIRED_API_KEY_NAMES if not (vals.get(k) or "").strip()]


def api_keys_ready(values: Dict[str, str] | None = None) -> bool:
    return not missing_api_keys(values)


def upsert_env_keys(updates: Dict[str, str]) -> None:
    ensure_env_file()
    raw = ENV_PATH.read_text(encoding="utf-8") or _DEFAULT
    pending = {k: (updates.get(k) or "").strip() for k in updates}
    out: List[str] = []
    seen: set[str] = set()
    for line in raw.splitlines(keepends=True):
        core = line.rstrip("\r\n")
        stripped = core.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pending:
                ending = "\r\n" if line.endswith("\r\n") else "\n"
                out.append(f"{key}={pending[key]}{ending}")
                seen.add(key)
                continue
        out.append(line)
    missing = [k for k in pending if k not in seen]
    if missing:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append("\n# --- UI에서 추가된 API 키 ---\n")
        for key in missing:
            out.append(f"{key}={pending[key]}\n")
    ENV_PATH.write_text("".join(out), encoding="utf-8")


def apply_secrets_to_runtime(values: Dict[str, str]) -> None:
    global _LOADED
    for key, value in values.items():
        os.environ[key] = (value or "").strip()
    _LOADED = True


def save_api_keys_and_apply(values: Dict[str, str]) -> List[str]:
    cleaned = {k: (values.get(k) or "").strip() for k in API_KEY_NAMES}
    upsert_env_keys(cleaned)
    apply_secrets_to_runtime({**read_env_map(), **cleaned})
    return missing_api_keys(cleaned)


def import_from_legacy_bot() -> int:
    """premarket-bot/.env 에서 FMP/Alpaca 키만 복사. 값은 로그하지 않는다."""
    if not LEGACY_ENV.exists():
        return 0
    src: Dict[str, str] = {}
    for raw in LEGACY_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith(("FMP_", "ALPACA_")):
            src[key] = value.strip().strip('"').strip("'")
    if not src:
        return 0
    upsert_env_keys(src)
    apply_secrets_to_runtime(src)
    return len([k for k in API_KEY_NAMES if src.get(k)])


def fmp_key() -> str:
    _ensure()
    return (os.getenv("FMP_API_KEY") or "").strip()


def alpaca_keys() -> tuple[str, str]:
    _ensure()
    return (
        (os.getenv("ALPACA_API_KEY") or "").strip(),
        (os.getenv("ALPACA_SECRET_KEY") or "").strip(),
    )


def fmp_ready() -> bool:
    return bool(fmp_key())


def alpaca_ready() -> bool:
    k, s = alpaca_keys()
    return bool(k and s)


def fmp_base() -> str:
    _ensure()
    return (os.getenv("FMP_BASE_URL") or "https://financialmodelingprep.com").rstrip("/")


def quote_path() -> str:
    _ensure()
    return "/stable/quote"


def poll_interval() -> float:
    _ensure()
    try:
        return max(1.0, float(os.getenv("FMP_POLL_INTERVAL_SEC") or 1.0))
    except ValueError:
        return 1.0
