"""
Unit tests for llm/server_manager.py.

These cannot exercise a real llama-server/vLLM process or GPU in this environment —
that verification can only happen on your actual Blackwell machine. What's tested
here is the logic we *can* verify without hardware: the loopback-only guard, the
telemetry-env construction, command building, and the health-poll/timeout state
machine, all via mocks. Treat this as "the wiring is correct," not "it runs."
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch as mock_patch

import pytest

from llm.model_profiles import PROFILES
from llm.server_manager import (
    UnsafeBindAddressError,
    ServerLaunchError,
    _build_launch_command,
    _privacy_env,
    start_server,
)


def test_privacy_env_overrides_existing_vars(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    env = _privacy_env(["HF_HUB_OFFLINE=1", "DO_NOT_TRACK=1"])
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["DO_NOT_TRACK"] == "1"


def test_build_launch_command_llama_server(tmp_path, monkeypatch):
    # A developer machine may point LLAMA_SERVER_EXE at a local build; the
    # test asserts the *default* executable name, so isolate the env var.
    monkeypatch.delenv("LLAMA_SERVER_EXE", raising=False)
    profile = PROFILES["nemotron_nvfp4"]
    weights = tmp_path / "model.gguf"
    cmd = _build_launch_command(profile, weights, "127.0.0.1", 8080)
    assert cmd[0] == "llama-server"
    assert "--host" in cmd and "127.0.0.1" in cmd
    assert "--port" in cmd and "8080" in cmd


def test_start_server_rejects_non_loopback_host(tmp_path):
    profile = PROFILES["nemotron_nvfp4"]
    with pytest.raises(UnsafeBindAddressError):
        start_server(
            profile=profile,
            models_dir=tmp_path,
            host="0.0.0.0",
            port=8080,
            disable_telemetry_env=[],
        )


def test_start_server_raises_if_weights_missing(tmp_path):
    profile = PROFILES["nemotron_nvfp4"]
    with pytest.raises(FileNotFoundError):
        start_server(
            profile=profile,
            models_dir=tmp_path,  # empty — weights deliberately not created
            host="127.0.0.1",
            port=8080,
            disable_telemetry_env=[],
        )


def test_start_server_raises_on_early_exit(tmp_path):
    profile = PROFILES["nemotron_nvfp4"]
    weights_dir = tmp_path / profile.weights_path
    weights_dir.mkdir(parents=True)

    mock_process = MagicMock()
    mock_process.poll.return_value = 1  # already exited
    mock_process.returncode = 1
    mock_process.stdout.read.return_value = "fatal: out of memory"

    # _available_vram_gb forced to None (can't be determined) so this test's
    # outcome doesn't depend on whatever GPU the machine running it happens
    # to have -- the VRAM pre-flight check below is tested on its own.
    with mock_patch("llm.server_manager._available_vram_gb", return_value=None), \
         mock_patch("llm.server_manager.subprocess.Popen", return_value=mock_process):
        with pytest.raises(ServerLaunchError, match="exited early"):
            start_server(
                profile=profile,
                models_dir=tmp_path,
                host="127.0.0.1",
                port=8080,
                disable_telemetry_env=[],
                startup_timeout_seconds=2,
                poll_interval_seconds=0.1,
            )


def test_start_server_succeeds_on_healthy_response(tmp_path):
    profile = PROFILES["nemotron_nvfp4"]
    weights_dir = tmp_path / profile.weights_path
    weights_dir.mkdir(parents=True)

    mock_process = MagicMock()
    mock_process.poll.return_value = None  # still running

    mock_response = MagicMock()
    mock_response.status_code = 200

    with mock_patch("llm.server_manager._available_vram_gb", return_value=None), \
         mock_patch("llm.server_manager.subprocess.Popen", return_value=mock_process), \
         mock_patch("llm.server_manager.httpx.get", return_value=mock_response):
        handle = start_server(
            profile=profile,
            models_dir=tmp_path,
            host="127.0.0.1",
            port=8080,
            disable_telemetry_env=[],
            startup_timeout_seconds=5,
            poll_interval_seconds=0.1,
        )
    assert handle.base_url == "http://127.0.0.1:8080"
    assert handle.is_running()


def test_start_server_raises_when_declared_budget_exceeds_available_vram(tmp_path):
    """ModelProfile.is_within_budget() (llm/model_profiles.py) previously
    existed but was never called anywhere -- this pins it into start_server's
    actual pre-flight path: an insufficiently-free-VRAM machine must fail
    fast with an actionable message, before ever spawning the subprocess."""
    profile = PROFILES["nemotron_nvfp4"]  # declared_vram_gb=9.0
    weights_dir = tmp_path / profile.weights_path
    weights_dir.mkdir(parents=True)

    with mock_patch("llm.server_manager._available_vram_gb", return_value=4.0), \
         mock_patch("llm.server_manager.subprocess.Popen") as mock_popen:
        with pytest.raises(ServerLaunchError, match="VRAM"):
            start_server(
                profile=profile,
                models_dir=tmp_path,
                host="127.0.0.1",
                port=8080,
                disable_telemetry_env=[],
            )
        mock_popen.assert_not_called()  # must fail before ever spawning the process


def test_start_server_proceeds_when_within_declared_budget(tmp_path):
    profile = PROFILES["nemotron_nvfp4"]  # declared_vram_gb=9.0, default headroom_gb=2.0
    weights_dir = tmp_path / profile.weights_path
    weights_dir.mkdir(parents=True)

    mock_process = MagicMock()
    mock_process.poll.return_value = None
    mock_response = MagicMock()
    mock_response.status_code = 200

    with mock_patch("llm.server_manager._available_vram_gb", return_value=16.0), \
         mock_patch("llm.server_manager.subprocess.Popen", return_value=mock_process), \
         mock_patch("llm.server_manager.httpx.get", return_value=mock_response):
        handle = start_server(
            profile=profile,
            models_dir=tmp_path,
            host="127.0.0.1",
            port=8080,
            disable_telemetry_env=[],
            startup_timeout_seconds=5,
            poll_interval_seconds=0.1,
        )
    assert handle.is_running()


def test_start_server_skips_budget_check_when_vram_undeterminable(tmp_path):
    """No NVIDIA GPU / no driver / nvidia-smi missing -- _available_vram_gb
    returns None, and the check must be skipped rather than failing closed,
    since it has no meaningful answer to give on such a machine (e.g. a
    non-NVIDIA GPU or a CPU-only setup, both valid configurations)."""
    profile = PROFILES["nemotron_nvfp4"]
    weights_dir = tmp_path / profile.weights_path
    weights_dir.mkdir(parents=True)

    mock_process = MagicMock()
    mock_process.poll.return_value = None
    mock_response = MagicMock()
    mock_response.status_code = 200

    with mock_patch("llm.server_manager._available_vram_gb", return_value=None), \
         mock_patch("llm.server_manager.subprocess.Popen", return_value=mock_process), \
         mock_patch("llm.server_manager.httpx.get", return_value=mock_response):
        handle = start_server(
            profile=profile,
            models_dir=tmp_path,
            host="127.0.0.1",
            port=8080,
            disable_telemetry_env=[],
            startup_timeout_seconds=5,
            poll_interval_seconds=0.1,
        )
    assert handle.is_running()


def test_available_vram_gb_returns_none_when_nvidia_smi_absent(monkeypatch):
    from llm.server_manager import _available_vram_gb

    monkeypatch.setattr("shutil.which", lambda name: None)
    assert _available_vram_gb() is None
