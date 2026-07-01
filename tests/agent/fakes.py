"""Test doubles for agent/loop.py, kept separate from tests/mcp_server/fakes.py
since that module's FakeLLMClient returns one fixed response, whereas loop
tests need a different scripted response per ReAct turn."""

from __future__ import annotations

from agent.mcp_client import MCPToolError
from llm.client import LLMClient


class ScriptedLLMClient(LLMClient):
    """Returns canned raw turns in order, one per call to complete()."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if not self._responses:
            raise AssertionError("ScriptedLLMClient ran out of canned responses.")
        return self._responses.pop(0)


class FakeMCPClient:
    """
    Scripted stand-in for agent.mcp_client.AgentMCPClient. `script` is a list
    of (expected_tool_name, outcome) pairs consumed strictly in order, where
    outcome is either a return value or an exception instance to raise.
    """

    def __init__(self, tools: list[dict], script: list[tuple[str, object]]) -> None:
        self._tools = tools
        self._script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self) -> list[dict]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict) -> object:
        self.calls.append((name, arguments))
        if not self._script:
            raise AssertionError(f"No more scripted tool results, but call_tool({name!r}, {arguments!r}) was invoked.")
        expected_name, outcome = self._script.pop(0)
        if name != expected_name:
            raise AssertionError(f"Expected next call to be {expected_name!r}, got {name!r}.")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


TOOLS_FIXTURE = [
    {"name": "start_meeting", "description": "Begin recording.", "input_schema": {}},
    {"name": "stop_meeting", "description": "Stop recording.", "input_schema": {}},
    {"name": "transcribe_meeting", "description": "Transcribe.", "input_schema": {}},
    {"name": "extract_action_items", "description": "Extract action items.", "input_schema": {}},
    {"name": "propose_todo_update", "description": "Write a draft proposal.", "input_schema": {}},
    {"name": "get_session_status", "description": "Read-only status.", "input_schema": {}},
    {"name": "list_sessions", "description": "Read-only list.", "input_schema": {}},
    {"name": "get_transcript", "description": "Read-only transcript.", "input_schema": {}},
]


def mcp_tool_error(message: str) -> MCPToolError:
    return MCPToolError(message)
