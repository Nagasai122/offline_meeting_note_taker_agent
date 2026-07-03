"""
Thin client for the already-running local LLM server (llm/server_manager.py
owns the process lifecycle; this module only talks to its OpenAI-compatible
`/v1/chat/completions` endpoint, which both supported backends -- llama-server
and vLLM -- expose).

`trust_env=False` on every request is deliberate and mirrors the same
loopback-integrity rationale already applied to the health check in
llm/server_manager.py: a configured HTTP(S)_PROXY environment variable must
never be allowed to intercept a request this tool believes is loopback-only.
This is part of the data-egress guarantee, not an unrelated httpx detail.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx


class LLMClient(ABC):
    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Return the model's full text response to one system+user turn."""


class LLMRequestError(RuntimeError):
    """Raised when the local LLM server cannot be reached or returns an error."""


class HttpLLMClient(LLMClient):
    def __init__(self, base_url: str, timeout_seconds: float = 600.0, temperature: float = 0.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": 1024,
            # SB-1.1: disable prompt-caching so KV cache from one session's
            # extraction call never bleeds into another session's. This tool is
            # intentionally single-user, so the performance trade-off is accepted.
            "cache_prompt": False,
        }
        try:
            response = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout_seconds,
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMRequestError(f"Local LLM request failed: {exc}") from exc
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMRequestError(f"Unexpected response shape from local LLM server: {data!r}") from exc
