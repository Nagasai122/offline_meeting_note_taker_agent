"""
propose_todo_update (the last agent-facing write in the pipeline -- it writes
only a draft under data/pending_review/, never data/todo.md itself, per the
draft-only-supervision constraint) plus three read-only query tools:
get_session_status, list_sessions, get_transcript.

`apply_reviewed_update`, which is the only thing permitted to write
data/todo.md, deliberately does NOT live in this module or anywhere imported
by mcp_server/server.py -- per critique amendment 2, it is implemented and
wired in M6 as a CLI-only command gated by a local capability token, and is
structurally absent from the agent loop's toolset, not merely refused at
runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from mcp_server import state as state_mod
from mcp_server.schemas import validate_session_id
from mcp_server.todo import TodoFileUnparsableError, parse_todo


def propose_todo_update(
    session_id: str,
    meetings_dir: Path | str,
    todo_path: Path | str,
    pending_review_dir: Path | str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> dict:
    validate_session_id(session_id)
    meetings_dir = Path(meetings_dir)
    actions_path = meetings_dir / f"{session_id}.actions.json"
    if not actions_path.exists():
        raise FileNotFoundError(f"No extracted action items found for session '{session_id}' at {actions_path}.")

    try:
        # Read-through validation only, per amendment 8: a malformed todo.md
        # must surface as TODO_FILE_UNPARSEABLE rather than this tool silently
        # writing a proposal next to a file it could not actually understand.
        parse_todo(todo_path)
    except TodoFileUnparsableError as exc:
        state_mod.transition(
            state_dir, session_id, state_mod.State.FAILED, lock_path, lock_timeout,
            error=f"TODO_FILE_UNPARSEABLE: {exc}",
        )
        raise

    action_items = json.loads(actions_path.read_text())

    lines = [f"# Proposed todo updates -- session {session_id}", ""]
    for item in action_items:
        meta = {
            "id": uuid4().hex[:8],
            "owner": item.get("owner"),
            "due_date": item.get("due_date"),
            "session_id": session_id,
        }
        lines.append(f"- [ ] {item['description']} <!-- meta: {json.dumps(meta)} -->")

    pending_review_path = Path(pending_review_dir) / f"{session_id}.md"
    pending_review_path.parent.mkdir(parents=True, exist_ok=True)
    pending_review_path.write_text("\n".join(lines) + "\n")

    session = state_mod.transition(
        state_dir, session_id, state_mod.State.PROPOSED, lock_path, lock_timeout,
        pending_review_path=str(pending_review_path),
    )
    return {
        "session_id": session_id,
        "state": session.state.value,
        "pending_review_path": str(pending_review_path),
        "proposed_count": len(action_items),
    }


def get_session_status(session_id: str, state_dir: Path | str) -> dict:
    validate_session_id(session_id)
    session = state_mod.load_session_state(state_dir, session_id)
    return {"session_id": session.session_id, "state": session.state.value, "history": session.history, "metadata": session.metadata}


def list_sessions(state_dir: Path | str, state_filter: str | None = None) -> list[dict]:
    results = []
    for session_id in state_mod.list_session_ids(state_dir):
        session = state_mod.load_session_state(state_dir, session_id)
        if state_filter is not None and session.state.value != state_filter:
            continue
        results.append({"session_id": session.session_id, "state": session.state.value})
    return results


def get_transcript(session_id: str, meetings_dir: Path | str) -> dict:
    validate_session_id(session_id)
    transcript_path = Path(meetings_dir) / f"{session_id}.json"
    if not transcript_path.exists():
        raise FileNotFoundError(f"No transcript found for session '{session_id}' at {transcript_path}.")
    return json.loads(transcript_path.read_text())
