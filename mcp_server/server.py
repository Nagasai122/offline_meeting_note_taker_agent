"""
The agent-facing MCP tool server.

Transport: stdio, not SSE/HTTP. This is a stronger form of the data-egress
guarantee than "loopback only" -- stdio opens no socket at all, so there is
nothing for `scripts/network_audit.py` to even need to check on this
component specifically. The agent loop (M5) is expected to launch this
process and talk to it over its stdin/stdout, the same way any local MCP
client launches a local MCP server.

Exactly 8 tools are registered below, matching the state-machine-named steps
in docs/architecture.md (start_meeting, stop_meeting, transcribe_meeting,
extract_action_items, propose_todo_update) plus three read-only query tools
(get_session_status, list_sessions, get_transcript) that give the agent loop
enough situational awareness to decide what to do next without re-deriving it
from raw files.

`apply_reviewed_update` is deliberately not imported here at all (see
mcp_server/tools/review.py's module docstring) -- per critique amendment 2,
this is enforced structurally, not by registering it and then refusing calls
to it at runtime.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from config.loader import Settings, load_settings
from llm.client import HttpLLMClient
from mcp_server.tools.extraction import extract_action_items as _extract_action_items
from mcp_server.tools.recording import start_meeting as _start_meeting
from mcp_server.tools.recording import stop_meeting as _stop_meeting
from mcp_server.tools.review import get_session_status as _get_session_status
from mcp_server.tools.review import get_transcript as _get_transcript
from mcp_server.tools.review import list_sessions as _list_sessions
from mcp_server.tools.review import propose_todo_update as _propose_todo_update
from mcp_server.tools.transcription import transcribe_meeting as _transcribe_meeting


def build_server(settings: Settings) -> FastMCP:
    mcp = FastMCP(name="meeting-agent", host=settings.llm.host)

    paths = settings.paths
    state_dir = Path(paths.data_dir) / "state"
    meetings_dir = Path(paths.data_dir) / "meetings"
    todo_path = Path(paths.data_dir) / "todo.md"
    pending_review_dir = Path(paths.data_dir) / "pending_review"
    tmp_dir = Path(paths.tmp_dir)
    lock_path = settings.concurrency.lock_path
    lock_timeout = settings.concurrency.lock_timeout_seconds
    llm_client = HttpLLMClient(base_url=f"http://{settings.llm.host}:{settings.llm.port}")

    @mcp.tool()
    def start_meeting(session_id: str, source: str, device_index: int | None = None) -> dict:
        """Begin recording a meeting (IDLE -> RECORDING). source is 'microphone' or 'loopback'."""
        return _start_meeting(session_id, source, tmp_dir, state_dir, lock_path, lock_timeout, device_index)

    @mcp.tool()
    def stop_meeting(session_id: str) -> dict:
        """Stop an in-progress recording (RECORDING -> STOPPED)."""
        return _stop_meeting(session_id, state_dir, lock_path, lock_timeout)

    @mcp.tool()
    def transcribe_meeting(session_id: str, diarisation: bool | None = None) -> dict:
        """Transcribe a stopped session and delete its audio (STOPPED -> TRANSCRIBED)."""
        diarisation_enabled = diarisation if diarisation is not None else settings.whisper.diarisation_enabled
        return _transcribe_meeting(
            session_id, tmp_dir, meetings_dir, state_dir, lock_path, lock_timeout,
            model_size=settings.whisper.model, device=settings.whisper.device,
            compute_type=settings.whisper.compute_type, diarisation_enabled=diarisation_enabled,
        )

    @mcp.tool()
    def extract_action_items(session_id: str) -> dict:
        """Ask the local LLM for action items from a transcript (TRANSCRIBED -> EXTRACTED)."""
        return _extract_action_items(session_id, meetings_dir, state_dir, lock_path, lock_timeout, llm_client)

    @mcp.tool()
    def propose_todo_update(session_id: str) -> dict:
        """Write a draft proposal under data/pending_review/ (EXTRACTED -> PROPOSED). Never touches data/todo.md."""
        return _propose_todo_update(session_id, meetings_dir, todo_path, pending_review_dir, state_dir, lock_path, lock_timeout)

    @mcp.tool()
    def get_session_status(session_id: str) -> dict:
        """Read-only: current state and history for one session."""
        return _get_session_status(session_id, state_dir)

    @mcp.tool()
    def list_sessions(state_filter: str | None = None) -> list[dict]:
        """Read-only: all known sessions, optionally filtered by state."""
        return _list_sessions(state_dir, state_filter)

    @mcp.tool()
    def get_transcript(session_id: str) -> dict:
        """Read-only: the structured transcript for a transcribed session."""
        return _get_transcript(session_id, meetings_dir)

    return mcp


def main(settings_path: Path = Path("config/settings.toml")) -> None:
    settings = load_settings(settings_path)
    server = build_server(settings)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
