"""
Note: deliberately NOT using pytest-asyncio (not a dependency of this
project -- pyproject.toml's [dev] extra is pytest+pytest-cov only). Each test
wraps its async body in a small sync helper and drives it with asyncio.run(),
keeping the dependency surface unchanged rather than pulling in a plugin for
one module's worth of coroutine tests.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.loop import AgentLoop, MaxIterationsExceededError
from tests.agent.fakes import TOOLS_FIXTURE, FakeMCPClient, ScriptedLLMClient, mcp_tool_error


def _run(coro):
    return asyncio.run(coro)


def _final(summary: str) -> str:
    return json.dumps({"thought": "done", "action": "final", "summary": summary})


def _call(tool: str, thought: str = "next step", **arguments) -> str:
    return json.dumps({"thought": thought, "action": tool, "arguments": arguments})


def test_happy_path_chains_tool_calls_then_final(tmp_path):
    llm = ScriptedLLMClient(
        [
            _call("stop_meeting"),
            _call("transcribe_meeting"),
            _call("extract_action_items"),
            _call("propose_todo_update"),
            _final("Drafted a proposal for human review."),
        ]
    )
    mcp = FakeMCPClient(
        TOOLS_FIXTURE,
        [
            ("stop_meeting", {"session_id": "s1", "state": "STOPPED"}),
            ("transcribe_meeting", {"session_id": "s1", "state": "TRANSCRIBED"}),
            ("extract_action_items", {"session_id": "s1", "state": "EXTRACTED"}),
            ("propose_todo_update", {"session_id": "s1", "state": "PROPOSED"}),
        ],
    )
    loop = AgentLoop(llm, mcp, trace_dir=tmp_path, max_iterations=12)

    result = _run(loop.run("s1"))

    assert result.outcome == "final"
    assert result.summary == "Drafted a proposal for human review."
    assert result.iterations == 5
    assert [call[0] for call in mcp.calls] == [
        "stop_meeting", "transcribe_meeting", "extract_action_items", "propose_todo_update",
    ]
    assert all(call[1]["session_id"] == "s1" for call in mcp.calls)


def test_session_id_argument_is_not_overridden_if_model_supplies_one(tmp_path):
    llm = ScriptedLLMClient([_call("get_session_status", session_id="s1"), _final("ok")])
    mcp = FakeMCPClient(TOOLS_FIXTURE, [("get_session_status", {"session_id": "s1", "state": "RECORDING"})])
    loop = AgentLoop(llm, mcp, trace_dir=tmp_path)

    _run(loop.run("s1"))
    assert mcp.calls[0][1] == {"session_id": "s1"}


def test_unknown_tool_is_rejected_without_calling_mcp_and_run_continues(tmp_path):
    llm = ScriptedLLMClient([_call("delete_everything"), _final("recovered")])
    mcp = FakeMCPClient(TOOLS_FIXTURE, [])  # no tool call should ever reach the MCP client
    loop = AgentLoop(llm, mcp, trace_dir=tmp_path)

    result = _run(loop.run("s1"))

    assert result.outcome == "final"
    assert mcp.calls == []


def test_malformed_model_turn_is_rejected_and_fed_back(tmp_path):
    llm = ScriptedLLMClient(["this is not json", _final("recovered")])
    mcp = FakeMCPClient(TOOLS_FIXTURE, [])
    loop = AgentLoop(llm, mcp, trace_dir=tmp_path)

    result = _run(loop.run("s1"))

    assert result.outcome == "final"
    assert "rejected" in llm.calls[1][1]


def test_max_iterations_exceeded_raises_and_logs(tmp_path):
    llm = ScriptedLLMClient([_call("get_session_status") for _ in range(5)])
    mcp = FakeMCPClient(
        TOOLS_FIXTURE,
        [("get_session_status", {"session_id": "s1", "state": "RECORDING"}) for _ in range(3)],
    )
    loop = AgentLoop(llm, mcp, trace_dir=tmp_path, max_iterations=3)

    with pytest.raises(MaxIterationsExceededError):
        _run(loop.run("s1"))

    trace_files = list(tmp_path.glob("*.jsonl"))
    assert len(trace_files) == 1
    last_record = json.loads(trace_files[0].read_text().strip().splitlines()[-1])
    assert last_record["outcome"] == "max_iterations_exceeded"


def test_tool_error_followed_by_failed_status_halts_run(tmp_path):
    llm = ScriptedLLMClient([_call("extract_action_items"), _call("extract_action_items")])
    mcp = FakeMCPClient(
        TOOLS_FIXTURE,
        [
            ("extract_action_items", mcp_tool_error("LLM did not return valid JSON action items")),
            ("get_session_status", {"session_id": "s1", "state": "FAILED", "metadata": {"error": "boom"}}),
        ],
    )
    loop = AgentLoop(llm, mcp, trace_dir=tmp_path)

    result = _run(loop.run("s1"))

    assert result.outcome == "session_failed"
    assert result.summary == "boom"
    assert len(llm.calls) == 1


def test_tool_error_without_failed_status_is_fed_back_and_run_continues(tmp_path):
    llm = ScriptedLLMClient([_call("get_transcript"), _final("gave up gracefully")])
    mcp = FakeMCPClient(
        TOOLS_FIXTURE,
        [
            ("get_transcript", mcp_tool_error("No transcript found")),
            ("get_session_status", mcp_tool_error("No session state found")),
        ],
    )
    loop = AgentLoop(llm, mcp, trace_dir=tmp_path)

    result = _run(loop.run("s1"))

    assert result.outcome == "final"
    assert result.summary == "gave up gracefully"


def test_read_only_tool_returning_failed_state_halts_immediately(tmp_path):
    llm = ScriptedLLMClient([_call("get_session_status")])
    mcp = FakeMCPClient(
        TOOLS_FIXTURE,
        [("get_session_status", {"session_id": "s1", "state": "FAILED", "metadata": {"error": "whisper crashed"}})],
    )
    loop = AgentLoop(llm, mcp, trace_dir=tmp_path)

    result = _run(loop.run("s1"))

    assert result.outcome == "session_failed"
    assert result.summary == "whisper crashed"
    assert len(llm.calls) == 1
