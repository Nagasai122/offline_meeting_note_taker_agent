"""
Tests for the three review/apply endpoints in cli/web.py:
  GET  /api/review/pending
  POST /api/review/decide
  POST /api/review/apply

Uses FastAPI's TestClient so no real server is needed.  The web app imports
`load_settings` at module level, so we patch settings on the app before making
requests.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcp_server.state import State, create_session, transition, load_session_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def dirs(tmp_path):
    d = {
        "data_dir":          tmp_path / "data",
        "meetings_dir":      tmp_path / "data" / "meetings",
        "state_dir":         tmp_path / "data" / "state",
        "pending_review_dir": tmp_path / "data" / "pending_review",
        "todo_path":         tmp_path / "data" / "todo.md",
        "lock_path":         tmp_path / "data" / "state" / ".lock",
    }
    for k in ("meetings_dir", "state_dir", "pending_review_dir"):
        d[k].mkdir(parents=True, exist_ok=True)
    d["todo_path"].write_text("")
    return d


def _make_draft(pending_review_dir: Path, session_id: str, items: list[dict]) -> None:
    lines = [f"# Proposed todo updates -- session {session_id}", ""]
    for item in items:
        meta = {
            "id": item["id"],
            "owner": item.get("owner"),
            "due_date": item.get("due_date"),
            "session_id": session_id,
        }
        lines.append(f"- [ ] {item['description']} <!-- meta: {json.dumps(meta)} -->")
    (pending_review_dir / f"{session_id}.md").write_text("\n".join(lines) + "\n")


def _advance_to_proposed(dirs, session_id: str, items: list[dict]) -> None:
    """Create state in PROPOSED and write the draft file."""
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0,
                   initial_state=State.EXTRACTED)
    transition(dirs["state_dir"], session_id, State.PROPOSED,
               dirs["lock_path"], 1.0)
    _make_draft(dirs["pending_review_dir"], session_id, items)


def _advance_to_reviewed(dirs, session_id: str, items: list[dict]) -> None:
    """Advance a session all the way to REVIEWED with all items accepted."""
    _advance_to_proposed(dirs, session_id, items)
    reviewed_payload = [
        {
            "id": it["id"],
            "decision": "accept",
            "description": it["description"],
            "owner": it.get("owner"),
            "due_date": it.get("due_date"),
            "session_id": session_id,
        }
        for it in items
    ]
    reviewed_path = dirs["pending_review_dir"] / f"{session_id}.reviewed.json"
    reviewed_path.write_text(json.dumps(reviewed_payload))
    transition(dirs["state_dir"], session_id, State.REVIEWED,
               dirs["lock_path"], 1.0)


@pytest.fixture()
def client(dirs):
    """Return a TestClient with settings patched to use tmp_path dirs."""
    import cli.web as web_module

    class _FakeSettings:
        class paths:
            data_dir = str(dirs["data_dir"])
            tmp_dir  = str(dirs["data_dir"] / "tmp")
        class llm:
            host = "127.0.0.1"
            port = 8080
            health_check_path = "/health"
            startup_timeout_seconds = 5
        class concurrency:
            lock_path = str(dirs["lock_path"])
            lock_timeout_seconds = 1.0
        class privacy:
            tmp_audio_ttl_seconds = 3600
        class whisper:
            device = "cpu"
            compute_type = "int8"

    with patch.object(web_module, "settings", _FakeSettings()):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c, dirs


# ---------------------------------------------------------------------------
# GET /api/review/pending
# ---------------------------------------------------------------------------

def test_pending_empty_when_no_sessions(client):
    c, dirs = client
    resp = c.get("/api/review/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert data["awaiting_review"] == []
    assert data["awaiting_apply"] == []


def test_pending_returns_items_for_proposed_session(client):
    c, dirs = client
    items = [
        {"id": "abc123", "description": "Write the report", "owner": "Alice", "due_date": "2026-07-04"},
        {"id": "def456", "description": "Book the venue",   "owner": None,    "due_date": None},
    ]
    _advance_to_proposed(dirs, "s1", items)

    resp = c.get("/api/review/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["awaiting_review"]) == 1
    sess = data["awaiting_review"][0]
    assert sess["session_id"] == "s1"
    assert len(sess["items"]) == 2
    assert sess["items"][0]["id"] == "abc123"
    assert sess["items"][0]["description"] == "Write the report"
    assert sess["items"][0]["owner"] == "Alice"


def test_pending_shows_reviewed_sessions_in_awaiting_apply(client):
    c, dirs = client
    items = [{"id": "aaa111", "description": "Send memo", "owner": "Bob", "due_date": None}]
    _advance_to_reviewed(dirs, "s2", items)

    resp = c.get("/api/review/pending")
    assert resp.status_code == 200
    data = resp.json()
    assert "s2" in data["awaiting_apply"]
    # Should not also appear in awaiting_review
    assert not any(s["session_id"] == "s2" for s in data["awaiting_review"])


# ---------------------------------------------------------------------------
# POST /api/review/decide
# ---------------------------------------------------------------------------

def test_decide_happy_path_accept_all(client):
    c, dirs = client
    items = [
        {"id": "aaa111", "description": "Task one",  "owner": "Alice", "due_date": "2026-07-01"},
        {"id": "bbb222", "description": "Task two",  "owner": None,    "due_date": None},
    ]
    _advance_to_proposed(dirs, "s1", items)

    resp = c.post("/api/review/decide", json={
        "session_id": "s1",
        "decisions": [
            {"id": "aaa111", "decision": "accept", "description": "Task one",
             "owner": "Alice", "due_date": "2026-07-01"},
            {"id": "bbb222", "decision": "accept", "description": "Task two",
             "owner": None, "due_date": None},
        ],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "REVIEWED"
    assert data["accepted_count"] == 2
    assert data["rejected_count"] == 0

    # Reviewed file must exist
    assert (dirs["pending_review_dir"] / "s1.reviewed.json").exists()


def test_decide_with_rejected_item(client):
    c, dirs = client
    items = [
        {"id": "aaa111", "description": "Keep this",  "owner": "Alice", "due_date": None},
        {"id": "bbb222", "description": "Drop this",   "owner": None,    "due_date": None},
    ]
    _advance_to_proposed(dirs, "s1", items)

    resp = c.post("/api/review/decide", json={
        "session_id": "s1",
        "decisions": [
            {"id": "aaa111", "decision": "accept",  "description": "Keep this",  "owner": "Alice", "due_date": None},
            {"id": "bbb222", "decision": "reject",  "description": "Drop this",  "owner": None,    "due_date": None},
        ],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted_count"] == 1
    assert data["rejected_count"] == 1

    reviewed = json.loads((dirs["pending_review_dir"] / "s1.reviewed.json").read_text())
    reject_entry = next(d for d in reviewed if d["id"] == "bbb222")
    assert reject_entry["decision"] == "reject"


def test_decide_with_edited_owner_and_due_date(client):
    c, dirs = client
    items = [{"id": "ccc333", "description": "Old desc", "owner": "Bob", "due_date": "2026-07-10"}]
    _advance_to_proposed(dirs, "s1", items)

    resp = c.post("/api/review/decide", json={
        "session_id": "s1",
        "decisions": [{
            "id": "ccc333", "decision": "accept",
            "description": "New desc",  # edited
            "owner": "Charlie",          # edited
            "due_date": "2026-08-01",    # edited
        }],
    })
    assert resp.status_code == 200
    reviewed = json.loads((dirs["pending_review_dir"] / "s1.reviewed.json").read_text())
    entry = reviewed[0]
    assert entry["description"] == "New desc"
    assert entry["owner"] == "Charlie"
    assert entry["due_date"] == "2026-08-01"


def test_decide_wrong_state_returns_409(client):
    c, dirs = client
    # Session doesn't exist at all → state machine will raise
    resp = c.post("/api/review/decide", json={
        "session_id": "ghost",
        "decisions": [],
    })
    # Either 404 (no file) or 409 (wrong state) — both are correct rejections
    assert resp.status_code in (404, 409)


# ---------------------------------------------------------------------------
# POST /api/review/apply
# ---------------------------------------------------------------------------

def test_apply_happy_path(client):
    c, dirs = client
    items = [{"id": "ddd444", "description": "Write the summary", "owner": "Dave", "due_date": "2026-07-05"}]
    _advance_to_reviewed(dirs, "s1", items)

    resp = c.post("/api/review/apply", json={"session_id": "s1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "APPLIED"
    assert data["applied_count"] == 1
    assert data["conflicts"] == []

    todo_content = dirs["todo_path"].read_text()
    assert "Write the summary" in todo_content
    assert "ddd444" in todo_content

    final_state = load_session_state(dirs["state_dir"], "s1")
    assert final_state.state == State.APPLIED


def test_docx_export_returns_a_downloadable_file(client):
    c, dirs = client
    items = [{"id": "ddd444", "description": "Write the summary", "owner": "Dave", "due_date": "2026-07-05"}]
    _advance_to_reviewed(dirs, "s1", items)
    resp_apply = c.post("/api/review/apply", json={"session_id": "s1"})
    assert resp_apply.status_code == 200

    resp = c.post("/api/export/docx", json={"session_id": "s1"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert len(resp.content) > 0


def test_docx_export_unreviewed_session_returns_400(client):
    c, dirs = client
    # EXTRACTED, not yet PROPOSED -- no human has reviewed this session's
    # draft items yet, so it must not be exportable (PROPOSED/REVIEWED/
    # APPLIED are; EXTRACTED and earlier are not).
    create_session(dirs["state_dir"], "s1", dirs["lock_path"], 1.0, initial_state=State.EXTRACTED)

    resp = c.post("/api/export/docx", json={"session_id": "s1"})
    assert resp.status_code == 400
    assert "human review" in resp.json()["error"]


def test_apply_surfaces_conflicts(client):
    c, dirs = client
    # Plant the same id in todo.md first
    items = [{"id": "eee555", "description": "Conflicting task", "owner": None, "due_date": None}]
    dirs["todo_path"].write_text(
        '- [ ] Conflicting task <!-- meta: {"id": "eee555", "owner": null, "due_date": null, "session_id": "old"} -->\n'
    )
    _advance_to_reviewed(dirs, "s1", items)

    resp = c.post("/api/review/apply", json={"session_id": "s1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied_count"] == 0
    assert len(data["conflicts"]) == 1
    conflict = data["conflicts"][0]
    assert conflict["id"] == "eee555"
    # Both existing and incoming must be present (amendment 3)
    assert "existing" in conflict
    assert "incoming" in conflict


def test_apply_wrong_state_returns_409(client):
    c, dirs = client
    # Session in PROPOSED (not REVIEWED) → should 409
    items = [{"id": "fff666", "description": "Not ready", "owner": None, "due_date": None}]
    _advance_to_proposed(dirs, "s1", items)

    resp = c.post("/api/review/apply", json={"session_id": "s1"})
    assert resp.status_code == 409


def test_apply_missing_session_returns_404(client):
    c, dirs = client
    resp = c.post("/api/review/apply", json={"session_id": "no-such-session"})
    assert resp.status_code in (404, 409)
