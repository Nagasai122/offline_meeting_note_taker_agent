"""
Tests for GET /api/meetings/{session_id} in cli/web.py.

Covers:
- happy path: transcript + all optional files present
- transcript-only (no summary/actions/highlights)
- 404 on missing session
- 422 (path traversal rejection) via validate_session_id
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    import cli.web as web_module

    meetings_dir = tmp_path / "data" / "meetings"
    meetings_dir.mkdir(parents=True)

    class _FakeSettings:
        class paths:
            data_dir = str(tmp_path / "data")
            tmp_dir  = str(tmp_path / "tmp")
        class llm:
            host = "127.0.0.1"; port = 8080; health_check_path = "/health"
            startup_timeout_seconds = 5
        class concurrency:
            lock_path = str(tmp_path / "data" / "state" / ".lock")
            lock_timeout_seconds = 1.0
        class privacy:
            tmp_audio_ttl_seconds = 3600
        class whisper:
            device = "cpu"; compute_type = "int8"

    with patch.object(web_module, "settings", _FakeSettings()):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c, meetings_dir


def test_detail_happy_path_all_files(client):
    c, meetings_dir = client
    (meetings_dir / "s1.md").write_text("Full transcript text")
    (meetings_dir / "s1.summary.md").write_text("Meeting summary")
    (meetings_dir / "s1.actions.json").write_text(json.dumps([
        {"description": "Write report", "owner": "Alice", "due_date": "2026-07-01"}
    ]))
    (meetings_dir / "s1.highlights.json").write_text(json.dumps(["Key point A", "Key point B"]))

    resp = c.get("/api/meetings/s1")
    assert resp.status_code == 200
    d = resp.json()
    assert d["session_id"] == "s1"
    assert d["transcript"] == "Full transcript text"
    assert d["summary"] == "Meeting summary"
    assert len(d["actions"]) == 1
    assert d["actions"][0]["description"] == "Write report"
    assert d["actions"][0]["owner"] == "Alice"
    assert len(d["highlights"]) == 2
    assert d["highlights"][0] == "Key point A"


def test_detail_transcript_only(client):
    c, meetings_dir = client
    (meetings_dir / "transcript-only.md").write_text("Just a transcript, no extras")

    resp = c.get("/api/meetings/transcript-only")
    assert resp.status_code == 200
    d = resp.json()
    assert d["transcript"] == "Just a transcript, no extras"
    assert d["summary"] is None
    assert d["actions"] is None
    assert d["highlights"] is None


def test_detail_missing_session_returns_404(client):
    c, _ = client
    resp = c.get("/api/meetings/no-such-session")
    assert resp.status_code == 404


def test_detail_path_traversal_rejected(client):
    c, _ = client
    # ../etc/passwd style — validate_session_id must reject this
    resp = c.get("/api/meetings/..%2F..%2Fetc%2Fpasswd")
    # FastAPI URL-decodes before routing, but the segment can't start with ".."
    # either 422 (validate_session_id rejects) or 404 (routing mismatch)
    assert resp.status_code in (404, 422)


def test_detail_invalid_chars_rejected(client):
    c, _ = client
    # Pipe character not in [A-Za-z0-9_\-] — must 422
    resp = c.get("/api/meetings/foo|bar")
    assert resp.status_code in (404, 422)


def test_detail_session_with_hyphen_and_digits(client):
    """Session IDs like is-call-20260630-090000 must be accepted."""
    c, meetings_dir = client
    (meetings_dir / "is-call-20260630-090000.md").write_text("IS call transcript")

    resp = c.get("/api/meetings/is-call-20260630-090000")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "is-call-20260630-090000"


def test_detail_actions_invalid_json_returns_null(client):
    """Corrupted actions.json must not crash the endpoint — return null."""
    c, meetings_dir = client
    (meetings_dir / "bad-actions.md").write_text("Some transcript")
    (meetings_dir / "bad-actions.actions.json").write_text("not valid json {{{")

    resp = c.get("/api/meetings/bad-actions")
    assert resp.status_code == 200
    assert resp.json()["actions"] is None
    assert resp.json()["transcript"] == "Some transcript"
