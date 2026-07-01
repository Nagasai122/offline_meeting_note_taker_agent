"""
Real-subprocess, real-socket test of HttpLLMClient against the fake_llama_server
fixture's new /v1/chat/completions stub -- same rationale as
test_server_manager_integration.py: this proves the HTTP wiring and response
parsing against an actual socket, not a mock.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from llm.client import HttpLLMClient, LLMRequestError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "fake_llama_server.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(FIXTURE), "--host", "127.0.0.1", "--port", str(port)])
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5, trust_env=False)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_http_llm_client_complete_against_real_socket(fake_server):
    client = HttpLLMClient(base_url=fake_server)
    response = client.complete("system prompt", "user prompt")
    assert response == "[]"


def test_http_llm_client_raises_typed_error_when_server_unreachable():
    client = HttpLLMClient(base_url="http://127.0.0.1:1", timeout_seconds=1.0)
    with pytest.raises(LLMRequestError):
        client.complete("sys", "user")
