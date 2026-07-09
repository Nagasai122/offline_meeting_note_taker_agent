from __future__ import annotations

from config.loader import IdentityConfig, load_settings

_REQUIRED_SECTIONS = """
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
"""


def test_missing_identity_section_defaults_to_empty(tmp_path):
    # Older config files (pre-P2) have no [identity] section at all -- must
    # load without modification, same guarantee [agent]/[export] already have.
    path = tmp_path / "settings.toml"
    path.write_text(_REQUIRED_SECTIONS, encoding="utf-8")

    settings = load_settings(path)

    assert settings.identity == IdentityConfig(name="", aliases=[], institution="")


def test_identity_section_parses(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(
        _REQUIRED_SECTIONS
        + '\n[identity]\nname = "Naga Sai"\naliases = ["Naga", "N. Sai"]\ninstitution = "UREAD"\n',
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.identity == IdentityConfig(name="Naga Sai", aliases=["Naga", "N. Sai"], institution="UREAD")
