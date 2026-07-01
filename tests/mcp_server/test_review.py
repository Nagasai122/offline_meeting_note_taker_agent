from __future__ import annotations

import json

import pytest

from mcp_server.state import State, create_session, load_session_state
from mcp_server.tools.review import get_session_status, get_transcript, list_sessions, propose_todo_update


def _dirs(tmp_path):
    return (
        tmp_path / "meetings",
        tmp_path / "todo.md",
        tmp_path / "pending_review",
        tmp_path / "state",
        tmp_path / ".lock",
    )


def test_propose_todo_update_writes_draft_and_transitions(tmp_path):
    meetings_dir, todo_path, pending_review_dir, state_dir, lock_path = _dirs(tmp_path)
    meetings_dir.mkdir()
    (meetings_dir / "s1.actions.json").write_text(
        json.dumps([{"description": "Send the report", "owner": "Naga", "due_date": "2026-07-03"}])
    )
    create_session(state_dir, "s1", lock_path, 1.0, initial_state=State.EXTRACTED)

    result = propose_todo_update("s1", meetings_dir, todo_path, pending_review_dir, state_dir, lock_path, 1.0)

    assert result["state"] == "PROPOSED"
    assert result["proposed_count"] == 1
    draft = (pending_review_dir / "s1.md").read_text()
    assert "Send the report" in draft
    assert not todo_path.exists()  # draft-only: todo.md itself must never be touched here
    assert load_session_state(state_dir, "s1").state == State.PROPOSED


def test_propose_todo_update_with_malformed_existing_todo_fails_loudly(tmp_path):
    meetings_dir, todo_path, pending_review_dir, state_dir, lock_path = _dirs(tmp_path)
    meetings_dir.mkdir()
    (meetings_dir / "s1.actions.json").write_text(json.dumps([{"description": "X"}]))
    todo_path.write_text("- [?] a hand-edited, broken line\n")
    create_session(state_dir, "s1", lock_path, 1.0, initial_state=State.EXTRACTED)

    with pytest.raises(Exception, match="TODO_FILE_UNPARSEABLE"):
        propose_todo_update("s1", meetings_dir, todo_path, pending_review_dir, state_dir, lock_path, 1.0)

    session = load_session_state(state_dir, "s1")
    assert session.state == State.FAILED
    assert "TODO_FILE_UNPARSEABLE" in session.metadata["error"]
    assert not (pending_review_dir / "s1.md").exists()


def test_propose_todo_update_missing_actions_raises(tmp_path):
    meetings_dir, todo_path, pending_review_dir, state_dir, lock_path = _dirs(tmp_path)
    create_session(state_dir, "s1", lock_path, 1.0, initial_state=State.EXTRACTED)
    with pytest.raises(FileNotFoundError):
        propose_todo_update("s1", meetings_dir, todo_path, pending_review_dir, state_dir, lock_path, 1.0)


def test_get_session_status_and_list_sessions(tmp_path):
    state_dir = tmp_path / "state"
    lock_path = tmp_path / ".lock"
    create_session(state_dir, "a", lock_path, 1.0)
    create_session(state_dir, "b", lock_path, 1.0, initial_state=State.RECORDING)

    status = get_session_status("a", state_dir)
    assert status["session_id"] == "a"
    assert status["state"] == "RECORDING"

    all_sessions = list_sessions(state_dir)
    assert {s["session_id"] for s in all_sessions} == {"a", "b"}

    filtered = list_sessions(state_dir, state_filter="RECORDING")
    assert {s["session_id"] for s in filtered} == {"a", "b"}
    assert list_sessions(state_dir, state_filter="APPLIED") == []


def test_get_transcript_reads_structured_json(tmp_path):
    meetings_dir = tmp_path / "meetings"
    meetings_dir.mkdir()
    payload = {"session_id": "s1", "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}]}
    (meetings_dir / "s1.json").write_text(json.dumps(payload))

    assert get_transcript("s1", meetings_dir) == payload


def test_get_transcript_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        get_transcript("nope", tmp_path / "meetings")
