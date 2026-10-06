"""6번 수정 검증: get_api_keys()가 os.environ을 우선 반영한다."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import env_settings  # noqa: E402

os.environ["GEMINI_API_KEY"] = "env-override-test-value"
keys = env_settings.get_api_keys()
assert keys["GEMINI_API_KEY"] == "env-override-test-value", f"os.environ 미반영: {keys}"
print("PASS: get_api_keys()가 os.environ을 우선한다")
del os.environ["GEMINI_API_KEY"]
