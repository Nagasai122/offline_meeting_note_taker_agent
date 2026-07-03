"""
Real-subprocess, real-stdio integration test for agent/mcp_client.py --
launches the actual mcp_server/server.py (M4) via `python -m cli.main
mcp-serve`, the same way agent/loop.py does in production, rather than mocking
the MCP SDK's stdio transport. This is the same "probe before you build, then
keep the real-subprocess test" practice already used for
tests/fixtures/fake_llama_server.py and tests/llm/test_client.py.

It also pins down, as an executable assertion rather than a comment, the two
response-shape facts documented in agent/mcp_client.py's docstring (list-typed
tool returns are wrapped under structuredContent={"result": [...]}; dict-typed
tool returns arrive only via content[0].text) -- so a future SDK upgrade that
changes either behaviour fails this test rather than silently breaking
call_tool()'s fallback logic.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from agent.mcp_client import AgentMCPClient, MCPToolError
from mcp_server.state import create_session


def _write_settings(tmp_path: Path) -> Path:
    # TOML double-quoted strings treat backslash as an escape character, so a
    # raw Windows path (e.g. "...\Users\...") interpolated directly here can
    # crash tomllib -- "\U" in particular is parsed as the start of an
    # 8-hex-digit unicode escape and raises TOMLDecodeError on whatever
    # non-hex characters follow it. .as_posix() sidesteps this entirely
    # (forward slashes need no escaping in TOML, and Windows accepts them
    # in paths too), rather than only working around it for one temp dir.
    data_dir = tmp_path / "data"
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text(
        textwrap.dedent(
            f"""
            [paths]
            data_dir = "{data_dir.as_posix()}"
            tmp_dir = "{(tmp_path / 'tmp').as_posix()}"
            models_dir = "{(tmp_path / 'models').as_posix()}"

            [llm]
            backend = "llama-server"
            active_profile = "nemotron_nvfp4"
            host = "127.0.0.1"
            port = 8080
            startup_timeout_seconds = 120
            health_check_path = "/health"

            [whisper]
            model = "medium"
            device = "cuda"
            compute_type = "int8_float16"
            diarisation_enabled = false

            [privacy]
            disable_telemetry_env = []
            tmp_audio_ttl_seconds = 0

            [concurrency]
            lock_path = "{(data_dir / 'state' / '.lock').as_posix()}"
            lock_timeout_seconds = 10

            [agent]
            max_iterations = 12
            trace_dir = "{(tmp_path / 'traces').as_posix()}"
            """
        )
    )
    return settings_path


def _project_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def test_list_tools_against_real_server_subprocess(tmp_path):
    settings_path = _write_settings(tmp_path)

    async def _go():
        async with AgentMCPClient(settings_path, cwd=_project_root()) as client:
            tools = await client.list_tools()
            return {t["name"] for t in tools}

    names = asyncio.run(_go())
    assert names == {
        "start_meeting", "stop_meeting", "transcribe_meeting", "extract_action_items",
        "propose_todo_update", "get_session_status", "list_sessions", "get_transcript",
    }
    assert "apply_reviewed_update" not in names


def test_call_tool_dict_return_via_real_server_subprocess(tmp_path):
    settings_path = _write_settings(tmp_path)
    data_dir = tmp_path / "data"
    create_session(data_dir / "state", "probe1", data_dir / "state" / ".lock", 5.0, source="microphone")

    async def _go():
        async with AgentMCPClient(settings_path, cwd=_project_root()) as client:
            return await client.call_tool("get_session_status", {"session_id": "probe1"})

    result = asyncio.run(_go())
    assert result["session_id"] == "probe1"
    assert result["state"] == "RECORDING"


def test_call_tool_list_return_via_real_server_subprocess(tmp_path):
    settings_path = _write_settings(tmp_path)
    data_dir = tmp_path / "data"
    create_session(data_dir / "state", "probe1", data_dir / "state" / ".lock", 5.0, source="microphone")

    async def _go():
        async with AgentMCPClient(settings_path, cwd=_project_root()) as client:
            return await client.call_tool("list_sessions", {})

    result = asyncio.run(_go())
    assert result == [{"session_id": "probe1", "state": "RECORDING"}]


def test_call_tool_error_raises_typed_exception(tmp_path):
    settings_path = _write_settings(tmp_path)

    async def _go():
        async with AgentMCPClient(settings_path, cwd=_project_root()) as client:
            await client.call_tool("get_session_status", {"session_id": "does-not-exist"})

    with pytest.raises(MCPToolError):
        asyncio.run(_go())
