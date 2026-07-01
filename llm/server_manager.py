"""
Lifecycle manager for the local LLM serving process (llama-server or vLLM).

Privacy-critical responsibilities, per docs/architecture.md:
- Bind strictly to 127.0.0.1. `host` is read from settings.toml but this module
  refuses to launch if it is ever set to anything else.
- Set telemetry-disabling environment variables on the subprocess BEFORE it starts.
- Never perform a network request to fetch weights. Missing weights are a hard error
  directing the user to the explicit, separate `setup` command.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import httpx

from llm.model_profiles import Backend, ModelProfile, resolve_weights_path

logger = logging.getLogger(__name__)


class ServerLaunchError(RuntimeError):
    """Raised when the LLM server process fails to start or fails its health check."""


class UnsafeBindAddressError(RuntimeError):
    """Raised if configuration would bind the server to anything but loopback."""


@dataclass
class ServerHandle:
    process: subprocess.Popen
    host: str
    port: int
    profile: ModelProfile

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def is_running(self) -> bool:
        return self.process.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        if not self.is_running():
            return
        logger.info("Stopping LLM server (pid=%s)", self.process.pid)
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("LLM server did not exit in %.1fs, killing", timeout)
            self.process.kill()
            self.process.wait()


def _relay_output(stream, buffer: deque, log: logging.Logger) -> None:
    """Read lines from the llm server subprocess and forward each to the logger.
    Runs in a daemon thread so startup output (incl. GPU offload lines) is visible
    without blocking the health-check polling loop."""
    for line in stream:
        stripped = line.rstrip()
        log.info("[llm] %s", stripped)
        buffer.append(stripped)


def _privacy_env(disable_telemetry_env: list[str]) -> dict[str, str]:
    """Build the environment for the subprocess: inherit current env, then apply the
    telemetry-disabling overrides from settings.toml on top."""
    env = os.environ.copy()
    for entry in disable_telemetry_env:
        key, _, value = entry.partition("=")
        env[key] = value
    return env


def _build_launch_command(
    profile: ModelProfile,
    weights_path: Path,
    host: str,
    port: int,
) -> list[str]:
    if profile.backend == Backend.LLAMA_SERVER:
        # Resolved via PATH by default. A developer-machine-specific absolute path
        # was previously hardcoded here, which silently broke on any other machine —
        # use the LLAMA_SERVER_EXE environment variable as the one supported override.
        exe_path = os.environ.get("LLAMA_SERVER_EXE", "llama-server")
        cmd = [
            exe_path,
            "--model", str(weights_path),
            "--host", host,
            "--port", str(port),
        ]
    elif profile.backend == Backend.VLLM:
        cmd = [
            "vllm", "serve", str(weights_path),
            "--host", host,
            "--port", str(port),
        ]
    else:  # pragma: no cover - exhaustive over Backend enum
        raise ValueError(f"Unsupported backend: {profile.backend}")
    cmd.extend(profile.extra_launch_args)
    return cmd


def start_server(
    profile: ModelProfile,
    models_dir: Path,
    host: str,
    port: int,
    disable_telemetry_env: list[str],
    health_check_path: str = "/health",
    startup_timeout_seconds: float = 120.0,
    poll_interval_seconds: float = 1.0,
) -> ServerHandle:
    """Launch the configured LLM backend and block until it is healthy or the
    startup timeout elapses.

    Raises:
        UnsafeBindAddressError: if `host` is not a loopback address.
        FileNotFoundError: if the profile's weights are not present locally.
        ServerLaunchError: if the process exits early or never becomes healthy.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise UnsafeBindAddressError(
            f"Refusing to bind LLM server to '{host}'. Only loopback addresses are "
            "permitted (data-egress guarantee, see docs/architecture.md)."
        )

    weights_path = resolve_weights_path(profile, models_dir)
    cmd = _build_launch_command(profile, weights_path, host, port)
    env = _privacy_env(disable_telemetry_env)

    logger.info("Launching LLM server: %s", " ".join(cmd))
    process = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    startup_log: deque = deque(maxlen=2000)
    log_thread = threading.Thread(
        target=_relay_output, args=(process.stdout, startup_log, logger), daemon=True
    )
    log_thread.start()

    handle = ServerHandle(process=process, host=host, port=port, profile=profile)
    deadline = time.monotonic() + startup_timeout_seconds
    health_url = f"{handle.base_url}{health_check_path}"

    while time.monotonic() < deadline:
        if not handle.is_running():
            log_thread.join(timeout=2.0)
            output = "\n".join(startup_log)
            raise ServerLaunchError(
                f"LLM server process exited early (code={process.returncode}). "
                f"Output:\n{output}"
            )
        try:
            # trust_env=False: ignore HTTP_PROXY/ALL_PROXY env vars for this
            # loopback health check, consistent with the loopback-only guarantee.
            response = httpx.get(health_url, timeout=2.0, trust_env=False)
            if response.status_code == 200:
                logger.info("LLM server healthy at %s", handle.base_url)
                return handle
        except httpx.RequestError:
            pass  # not up yet, keep polling
        time.sleep(poll_interval_seconds)

    handle.stop()
    raise ServerLaunchError(
        f"LLM server did not become healthy within {startup_timeout_seconds:.0f}s "
        f"(profile={profile.name}, url={health_url})."
    )
