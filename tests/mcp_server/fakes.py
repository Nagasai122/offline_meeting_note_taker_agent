"""Shared fakes for mcp_server tests -- no real LLM server or GPU involved."""

from __future__ import annotations

from llm.client import LLMClient


class FakeLLMClient(LLMClient):
    """Returns a fixed, canned response regardless of the prompt -- used to
    exercise the extraction tool's parsing/orchestration logic, and for the
    end-to-end fake-LLM smoke test, without a GPU or a running model server."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.response
