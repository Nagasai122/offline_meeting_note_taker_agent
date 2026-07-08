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


def resolve_llama_server_exe() -> str:
    """Resolve the llama-server executable, with a clear diagnostic for the one
    case that is unambiguously a misconfiguration: `LLAMA_SERVER_EXE` is set but
    points at a file that doesn't exist (root cause A from the bugfix-01 report --
    a session-scoped env var that got lost on terminal close and was re-set wrong,
    or a stale path after moving llama-server.exe).

    When `LLAMA_SERVER_EXE` is unset, this deliberately does NOT pre-validate
    against `shutil.which` and hard-fail here: on Windows, `subprocess.Popen`'s
    own CreateProcess-based PATH resolution can succeed for names `shutil.which`
    doesn't detect the same way (e.g. App Execution Aliases), so a `shutil.which`
    pre-check would produce false-negative failures for a setup that actually
    works. The bare 'llama-server' string is passed straight to Popen, exactly as
    before this function existed; if that genuinely can't be found, Popen itself
    raises immediately, and the early-exit / timeout diagnostics in start_server
    below (last output, common causes) still apply.
    """
    env_override = os.getenv("LLAMA_SERVER_EXE", "").strip()
    if env_override:
        if not Path(env_override).exists():
            raise FileNotFoundError(
                f"LLAMA_SERVER_EXE is set to '{env_override}' but that file does not exist.\n"
                "Fix: check the path, or re-point it at your actual llama-server.exe, e.g.:\n"
                "  [System.Environment]::SetEnvironmentVariable('LLAMA_SERVER_EXE', "
                "'D:\\llama.cpp\\llama-server.exe', 'User')\n"
                "Then open a new terminal and retry."
            )
        return env_override

    return "llama-server"


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
        # resolve_llama_server_exe() fails fast with actionable instructions instead
        # of letting a missing/misconfigured exe surface later as a 300s health-check
        # timeout with no explanation.
        exe_path = resolve_llama_server_exe()
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


def _available_vram_gb() -> float | None:
    """Query free GPU VRAM via `nvidia-smi`, in GB. Returns None (not 0.0) if
    it can't be determined -- no NVIDIA GPU, driver not installed/visible, or
    any other query failure -- so callers can distinguish "no data, skip the
    check" from "definitely insufficient". Same tool scripts/gpu_check.py
    already uses for its own diagnostics; this is a narrower, single-purpose
    reuse of the same `nvidia-smi --query-gpu` approach, not a new dependency."""
    import shutil

    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        # First GPU only -- this project's pre-flight budget check is about
        # "will the configured profile fit on this machine", not multi-GPU
        # placement, which nothing here supports launching across anyway.
        first_line = result.stdout.strip().splitlines()[0]
        return float(first_line.strip()) / 1024.0  # MiB -> GB
    except Exception:
        return None


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

    # Bug fix: ModelProfile.is_within_budget() was written specifically "to
    # fail fast and obviously before attempting to launch a profile that was
    # never going to fit" (see its own docstring) but was never actually
    # called anywhere -- a real safety check that shipped inert. Only enforced
    # when VRAM can actually be queried (_available_vram_gb returns None on
    # non-NVIDIA/no-driver machines, where this check cannot mean anything and
    # must not block a valid CPU-or-other-GPU setup). This is still just the
    # declared-budget pre-flight check the docstring describes, not a
    # replacement for the measured-baseline work called out there.
    available_vram_gb = _available_vram_gb()
    if available_vram_gb is not None and not profile.is_within_budget(available_vram_gb):
        raise ServerLaunchError(
            f"Profile '{profile.name}' declares a {profile.declared_vram_gb:.1f}GB VRAM "
            f"budget, but only {available_vram_gb:.1f}GB is currently free (nvidia-smi). "
            "Close other GPU workloads, or switch to a smaller profile "
            "(config/settings.toml's [llm].active_profile) -- see llm/model_profiles.py "
            "for the available profiles and their declared budgets."
        )

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
    # Some llama-server builds expose /v1/health instead of (or in addition to)
    # /health; try the configured path first, then that fallback, each poll.
    health_paths = [health_check_path]
    if "/v1/health" not in health_paths:
        health_paths.append("/v1/health")

    while time.monotonic() < deadline:
        if not handle.is_running():
            log_thread.join(timeout=2.0)
            output = "\n".join(startup_log)
            raise ServerLaunchError(
                f"LLM server process exited early (code={process.returncode}). "
                f"Output:\n{output}\n"
                "Common causes: model weights not found (run `meeting-agent setup "
                "--profile <name>`), CUDA out of memory (reduce --n-gpu-layers), or "
                "a corrupt download (delete models/ and re-run setup)."
            )
        for path in health_paths:
            try:
                # trust_env=False: ignore HTTP_PROXY/ALL_PROXY env vars for this
                # loopback health check, consistent with the loopback-only guarantee.
                response = httpx.get(f"{handle.base_url}{path}", timeout=2.0, trust_env=False)
                if response.status_code == 200:
                    logger.info("LLM server healthy at %s%s", handle.base_url, path)
                    return handle
            except httpx.RequestError:
                pass  # not up yet, keep polling
        time.sleep(poll_interval_seconds)

    handle.stop()
    log_tail = list(startup_log)[-20:]
    log_text = "\n".join(log_tail) if log_tail else "(no output captured — process may not have started)"
    raise ServerLaunchError(
        f"LLM server did not become healthy within {startup_timeout_seconds:.0f}s "
        f"(profile={profile.name}, url={handle.base_url}{health_check_path}).\n"
        f"Last server output:\n{log_text}\n"
        "If you see 'model file does not exist': run `meeting-agent setup --profile <name>`.\n"
        "If you see a CUDA error: check `nvidia-smi` and your driver version.\n"
        "If there is no output at all: confirm LLAMA_SERVER_EXE points at a real "
        "executable (see resolve_llama_server_exe)."
    )
