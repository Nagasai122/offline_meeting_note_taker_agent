"""
Tests for GET/PATCH /api/settings, extended in P2.3 to surface the new
[identity] config section (name/aliases/institution) used by ownership
classification.

DEFAULT_SETTINGS_PATH is a module-level constant resolved relative to CWD at
import time (cli/web.py), so patch_settings's real-file read/rewrite is
tested here against an isolated tmp_path copy -- never the project's own
config/settings.toml.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from config.loader import load_settings

_SETTINGS_TOML = """
[paths]
data_dir = "data"
tmp_dir = "tmp"
models_dir = "models"

[llm]
backend = "llama-server"
active_profile = "qwen2_5_7b_gguf"
host = "127.0.0.1"
port = 8080
startup_timeout_seconds = 300
health_check_path = "/health"

[whisper]
model = "distil-large-v3"
device = "cuda"
compute_type = "int8_float16"

[privacy]
disable_telemetry_env = []
tmp_audio_ttl_seconds = 3600

[concurrency]
lock_path = "data/state/.lock"
lock_timeout_seconds = 10

[identity]
name = ""
aliases = []
institution = ""
"""


@pytest.fixture()
def client(tmp_path):
    import cli.web as web_module

    settings_path = tmp_path / "settings.toml"
    settings_path.write_text(_SETTINGS_TOML, encoding="utf-8")
    fake_settings = load_settings(settings_path)

    with patch.object(web_module, "DEFAULT_SETTINGS_PATH", settings_path), \
         patch.object(web_module, "settings", fake_settings):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c, settings_path


def test_get_settings_includes_identity_defaults(client):
    c, _ = client
    resp = c.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["identity_name"] == ""
    assert body["identity_aliases"] == []
    assert body["identity_institution"] == ""


def test_patch_settings_updates_identity_fields(client):
    c, settings_path = client
    resp = c.patch(
        "/api/settings",
        json={
            "identity_name": "Naga Sai",
            "identity_aliases": ["Naga", "N. Sai"],
            "identity_institution": "UREAD",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["identity_name"] == "Naga Sai"
    assert body["identity_aliases"] == ["Naga", "N. Sai"]
    assert body["identity_institution"] == "UREAD"

    reloaded = load_settings(settings_path)
    assert reloaded.identity.name == "Naga Sai"
    assert reloaded.identity.aliases == ["Naga", "N. Sai"]
    assert reloaded.identity.institution == "UREAD"


def test_patch_settings_identity_name_length_validated(client):
    c, _ = client
    resp = c.patch("/api/settings", json={"identity_name": "x" * 201})
    assert resp.status_code == 422


def test_patch_settings_no_identity_changes_leaves_file_untouched(client):
    c, settings_path = client
    before = settings_path.read_text(encoding="utf-8")
    resp = c.patch("/api/settings", json={})
    assert resp.status_code == 200
    assert resp.json() == {"status": "no_changes"}
    assert settings_path.read_text(encoding="utf-8") == before
