"""Gist 키 금고. 값은 가짜만 쓰고, 네트워크와 실제 .env 는 건드리지 않는다."""
import json
import os
import time

import cloud_sync
import env_settings


def test_load_cloud_keys_parses_gist_file(monkeypatch):
    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"files": {"api_keys.json": {"content": json.dumps({"DEEPSEEK_API_KEY": "fake-deep"})}}}

    monkeypatch.setattr(cloud_sync, "_secret", lambda name: "gist" if name == "GIST_ID" else "pat")
    monkeypatch.setattr(cloud_sync.requests, "get", lambda *a, **k: Resp())
    got = cloud_sync.load_cloud_keys()
    assert got == {"DEEPSEEK_API_KEY": "fake-deep"}


def test_gist_key_beats_environ_and_save_pushes(monkeypatch, tmp_path):
    env_settings.ENV_PATH = tmp_path / ".env"
    env_settings.ENV_PATH.write_text("DEEPSEEK_API_KEY=from-disk\n", encoding="utf-8")
    env_settings._CLOUD_KEYS = {"DEEPSEEK_API_KEY": "from-gist"}
    env_settings._CLOUD_KEYS_AT = time.time()
    os.environ["DEEPSEEK_API_KEY"] = "from-env"
    monkeypatch.setattr(env_settings, "_streamlit_secrets", lambda: {})
    got = env_settings.get_api_keys()
    assert got["DEEPSEEK_API_KEY"] == "from-gist"
    assert os.environ["DEEPSEEK_API_KEY"] == "from-gist"

    pushed = {}

    def fake_save(key_map):
        pushed.update(key_map)
        return True

    monkeypatch.setattr("cloud_sync.save_cloud_keys", fake_save)
    env_settings.save_api_keys({"DEEPSEEK_API_KEY": "typed-deep", "GROQ_API_KEY": ""})
    assert pushed["DEEPSEEK_API_KEY"] == "typed-deep"
    assert os.environ["DEEPSEEK_API_KEY"] == "typed-deep"
    assert "DEEPSEEK_API_KEY=typed-deep" in env_settings.ENV_PATH.read_text(encoding="utf-8")
