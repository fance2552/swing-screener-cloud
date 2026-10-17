"""`.env` API 키 — 사이드바 입력 또는 파일. load_dotenv 반복 호출 금지."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LEGACY_ENV = Path.home() / "Desktop" / "premarket-bot" / ".env"

API_KEY_NAMES: Tuple[str, ...] = (
    "FMP_API_KEY",
    "FMP_BASE_URL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
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
_CLOUD_KEYS: Dict[str, str] | None = None
_CLOUD_KEYS_AT = 0.0


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


def _cloud_key_map() -> Dict[str, str]:
    """프로세스 안에서 60초만 유지한다. 재배포면 다시 Gist를 읽는다."""
    global _CLOUD_KEYS, _CLOUD_KEYS_AT
    now = time.time()
    if _CLOUD_KEYS is not None and now - _CLOUD_KEYS_AT < 60:
        return _CLOUD_KEYS
    try:
        from cloud_sync import load_cloud_keys
        got = load_cloud_keys() or {}
    except Exception:
        got = {}
    if not isinstance(got, dict):
        got = {}
    _CLOUD_KEYS = {str(k): str(v).strip() for k, v in got.items() if str(v or "").strip()}
    _CLOUD_KEYS_AT = now
    return _CLOUD_KEYS


def get_api_keys() -> Dict[str, str]:
    """우선순위: st.secrets → Gist 금고 → os.environ → 로컬 .env."""
    secrets = _streamlit_secrets()
    cloud_keys = _cloud_key_map()
    disk = read_env_map()
    out: Dict[str, str] = {}
    for name in API_KEY_NAMES:
        sec_val = str(secrets.get(name) or "").strip()
        gist_val = str(cloud_keys.get(name) or "").strip()
        env_val = str(os.environ.get(name) or "").strip()
        disk_val = str(disk.get(name) or "").strip()
        final_val = sec_val or gist_val or env_val or disk_val
        out[name] = final_val
        if final_val:
            os.environ[name] = final_val
    return out


def save_api_keys(key_map: Dict[str, str]) -> None:
    """로컬 .env와 Gist api_keys.json 에 같이 쓴다. 빈 값은 금고에서 뺀다."""
    global _CLOUD_KEYS, _CLOUD_KEYS_AT
    cleaned = {str(k): str(v or "").strip() for k, v in (key_map or {}).items()}
    for key, value in cleaned.items():
        if value:
            os.environ[key] = value
        elif key in os.environ:
            os.environ[key] = ""
    upsert_env_keys(cleaned)
    try:
        from cloud_sync import save_cloud_keys
        save_cloud_keys(cleaned)
    except Exception:
        pass
    _CLOUD_KEYS = None
    _CLOUD_KEYS_AT = 0.0


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
    save_api_keys(cleaned)
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
