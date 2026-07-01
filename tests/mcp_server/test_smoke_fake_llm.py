"""
Fake-LLM end-to-end smoke test of the tool-call schema round-trip (critique
amendment 6) -- pulled forward from M5 deliberately, so this integration risk
(does the JSON contract between extract_action_items and the model actually
survive being written to disk and read back by propose_todo_update?) is
caught here, at M4, rather than only being discovered once the agent loop
exists.

This does NOT touch a GPU or a running llama-server/vLLM process: `FakeLLMClient`
stands in for the model, returning a fixed JSON action-item array. What this
test DOES exercise for real is every other hop in the chain: real file I/O for
the transcript fixture, real JSON parsing/validation, real state-machine
transitions under a real file lock, and a real Markdown draft written to
data/pending_review/.
"""

from __future__ import annotations

import json

from mcp_server.state import State, create_session, load_session_state
from mcp_server.tools.extraction import extract_action_items
from mcp_server.tools.review import propose_todo_update
from tests.mcp_server.fakes import FakeLLMClient


def test_transcribed_to_proposed_round_trip_with_fake_llm(tmp_path):
    meetings_dir = tmp_path / "meetings"
    meetings_dir.mkdir()
    todo_path = tmp_path / "todo.md"
    pending_review_dir = tmp_path / "pending_review"
    state_dir = tmp_path / "state"
    lock_path = tmp_path / ".lock"

    (meetings_dir / "standup-1.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"speaker": "Naga", "text": "I'll write up the architecture doc by Thursday."},
                    {"speaker": "Alex", "text": "Sounds good, I'll review it Friday."},
                ]
            }
        )
    )
    create_session(state_dir, "standup-1", lock_path, 1.0, initial_state=State.TRANSCRIBED)

    fake_llm = FakeLLMClient(
        response=json.dumps(
            [
                {"description": "Write up the architecture doc", "owner": "Naga", "due_date": "2026-07-02"},
                {"description": "Review the architecture doc", "owner": "Alex", "due_date": "2026-07-03"},
            ]
        )
    )

    extraction_result = extract_action_items("standup-1", meetings_dir, state_dir, lock_path, 1.0, fake_llm)
    assert extraction_result["state"] == "EXTRACTED"
    assert len(extraction_result["action_items"]) == 2
    assert load_session_state(state_dir, "standup-1").state == State.EXTRACTED

    proposal_result = propose_todo_update(
        "standup-1", meetings_dir, todo_path, pending_review_dir, state_dir, lock_path, 1.0
    )
    assert proposal_result["state"] == "PROPOSED"
    assert proposal_result["proposed_count"] == 2

    draft = (pending_review_dir / "standup-1.md").read_text()
    assert "Write up the architecture doc" in draft
    assert "Review the architecture doc" in draft
    assert not todo_path.exists()  # draft-only end to end: todo.md is never touched by this chain

    final_session = load_session_state(state_dir, "standup-1")
    assert final_session.state == State.PROPOSED
    assert [h["state"] for h in final_session.history] == ["TRANSCRIBED", "EXTRACTED", "PROPOSED"]
