"""Regression tests for two 2026-07 changes:

1. Session chaining injects prior-session SUMMARIES, not full transcripts
   (the "context bomb" audit finding D-3.3 — three full transcripts could
   exceed the model context alone and silently starve the current meeting).
2. The `evidence` provenance quote flows extraction → pending-review draft
   → todo.md (research doc recommendation 2: context-linked tasks).
"""

from __future__ import annotations

import json

from mcp_server.meeting_type import MeetingType
from mcp_server.tools.extraction import _PRIOR_SESSION_FALLBACK_WORDS, _load_prior_sessions
from mcp_server.tools.review import propose_todo_update
from mcp_server.state import State, create_session
from mcp_server.todo import parse_todo


def test_chaining_prefers_summary_over_full_transcript(tmp_path):
    meetings = tmp_path / "meetings"
    meetings.mkdir()
    (meetings / "is-call-20260701-090000.md").write_text(
        "FULL TRANSCRIPT " + "verbose spoken words " * 500, encoding="utf-8"
    )
    (meetings / "is-call-20260701-090000.summary.md").write_text(
        "- Agreed to migrate the cache\n- Naga to draft the ADR\n", encoding="utf-8"
    )

    ctx = _load_prior_sessions("is-call-20260702-090000", meetings, MeetingType.IS_CALL)

    assert "Agreed to migrate the cache" in ctx
    assert "FULL TRANSCRIPT" not in ctx


def test_chaining_falls_back_to_capped_transcript_when_no_summary(tmp_path):
    meetings = tmp_path / "meetings"
    meetings.mkdir()
    words = "word " * (_PRIOR_SESSION_FALLBACK_WORDS * 3)
    (meetings / "is-call-20260701-090000.md").write_text(words, encoding="utf-8")

    ctx = _load_prior_sessions("is-call-20260702-090000", meetings, MeetingType.IS_CALL)

    assert "truncated prior-transcript fallback" in ctx
    # The injected block must be dramatically smaller than the raw transcript.
    assert len(ctx.split()) < _PRIOR_SESSION_FALLBACK_WORDS + 100


def test_evidence_flows_from_extraction_to_draft_to_parse(tmp_path):
    meetings = tmp_path / "meetings"
    meetings.mkdir()
    state_dir = tmp_path / "state"
    lock = tmp_path / ".lock"
    todo_path = tmp_path / "todo.md"
    todo_path.write_text("")
    pending = tmp_path / "pending_review"

    create_session(state_dir, "ev-1", lock, 1.0, initial_state=State.EXTRACTED)
    (meetings / "ev-1.actions.json").write_text(json.dumps([
        {
            "description": "Draft the ADR",
            "owner": "Naga",
            "due_date": None,
            "priority": "HIGH",
            "evidence": "Tom said: Naga, please draft the ADR by Thursday.",
        },
        {"description": "No-evidence item", "owner": None, "due_date": None},
    ]), encoding="utf-8")

    propose_todo_update("ev-1", meetings, todo_path, pending, state_dir, lock, 1.0)

    items = parse_todo(pending / "ev-1.md").items
    assert items[0].evidence == "Tom said: Naga, please draft the ADR by Thursday."
    assert items[1].evidence is None
