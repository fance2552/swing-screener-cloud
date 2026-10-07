"""get_api_keys() 우선순위: st.secrets → os.environ → .env. 값은 출력하지 않는다."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import env_settings  # noqa: E402

os.environ["GROQ_API_KEY"] = "env-override-test-value"
keys = env_settings.get_api_keys()
secret_present = False
try:
    import streamlit as st

    secret_present = bool(str(st.secrets.get("GROQ_API_KEY") or "").strip())
except Exception:
    secret_present = False
got = keys.get("GROQ_API_KEY") or ""
if secret_present:
    assert got and got != "env-override-test-value", "secrets 가 environ 보다 우선해야 한다"
else:
    assert got == "env-override-test-value", "environ 값이 비어 있다"
print("PASS: get_api_keys() 우선순위 secrets → environ")
del os.environ["GROQ_API_KEY"]
