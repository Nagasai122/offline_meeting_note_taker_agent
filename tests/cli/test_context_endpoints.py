"""
Tests for the P3 "Add Context" endpoints in cli/web.py:
  POST /api/context/upload  (extended: now also accepts mid-recording
      sessions, .xlsx/.png/.jpg/.jpeg, and appends rather than overwrites)
  POST /api/context/text    (new)

Test documents/pasted text are kept under doc_ingest.py's
_MIN_WORDS_FOR_SUMMARY (60 words) so add_document_context/
add_pasted_text_context never call out to the LLM client -- these tests
exercise the endpoint/accumulation/locking behavior, not summarisation.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def dirs(tmp_path):
    d = {
        "data_dir": tmp_path / "data",
        "meetings_dir": tmp_path / "data" / "meetings",
        "state_dir": tmp_path / "data" / "state",
        "tmp_dir": tmp_path / "data" / "tmp",
        "lock_path": tmp_path / "data" / "state" / ".lock",
    }
    for k in ("meetings_dir", "state_dir", "tmp_dir"):
        d[k].mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def client(dirs):
    import cli.web as web_module

    class _FakeSettings:
        class paths:
            data_dir = str(dirs["data_dir"])
            tmp_dir = str(dirs["tmp_dir"])

        class llm:
            host = "127.0.0.1"
            port = 8080
            health_check_path = "/health"
            startup_timeout_seconds = 5

        class concurrency:
            lock_path = str(dirs["lock_path"])
            lock_timeout_seconds = 5.0

        class privacy:
            tmp_audio_ttl_seconds = 3600

        class whisper:
            device = "cpu"
            compute_type = "int8"

    with patch.object(web_module, "settings", _FakeSettings()):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c, dirs, web_module


def _doc_context_path(dirs, session_id: str):
    return dirs["meetings_dir"] / f"{session_id}.doc_context.txt"


def test_upload_rejects_unknown_session(client):
    c, dirs, web_module = client
    resp = c.post(
        "/api/context/upload",
        data={"session_id": "sess-unknown"},
        files={"file": ("notes.txt", b"Short note.", "text/plain")},
    )
    assert resp.status_code == 404


def test_upload_rejects_unsupported_extension(client):
    c, dirs, web_module = client
    (dirs["state_dir"] / "sess-a.json").write_text("{}", encoding="utf-8")
    resp = c.post(
        "/api/context/upload",
        data={"session_id": "sess-a"},
        files={"file": ("notes.xyz", b"hello", "application/octet-stream")},
    )
    assert resp.status_code == 400


def test_upload_accepts_txt_and_appends_labelled(client):
    c, dirs, web_module = client
    (dirs["state_dir"] / "sess-b.json").write_text("{}", encoding="utf-8")

    resp = c.post(
        "/api/context/upload",
        data={"session_id": "sess-b"},
        files={"file": ("agenda.txt", b"Short agenda note.", "text/plain")},
    )
    assert resp.status_code == 200
    content = _doc_context_path(dirs, "sess-b").read_text(encoding="utf-8")
    assert "Document: agenda.txt" in content
    assert "Short agenda note." in content


def test_upload_accepts_while_session_actively_recording(client):
    """P3.1: a session in RECORDING state (no state file yet, only known via
    the module-level active_session_id global) must be accepted, not
    rejected as 'unknown session' -- this is what makes mid-recording
    'Add Context' possible without any pre-meeting-modal-only restriction."""
    c, dirs, web_module = client
    web_module.active_session_id = "sess-recording"
    try:
        resp = c.post(
            "/api/context/upload",
            data={"session_id": "sess-recording"},
            files={"file": ("live-note.txt", b"Note added mid-recording.", "text/plain")},
        )
        assert resp.status_code == 200
    finally:
        web_module.active_session_id = None


def test_second_upload_appends_rather_than_overwrites(client):
    c, dirs, web_module = client
    (dirs["state_dir"] / "sess-c.json").write_text("{}", encoding="utf-8")

    c.post(
        "/api/context/upload", data={"session_id": "sess-c"},
        files={"file": ("first.txt", b"First document note.", "text/plain")},
    )
    resp = c.post(
        "/api/context/upload", data={"session_id": "sess-c"},
        files={"file": ("second.txt", b"Second document note.", "text/plain")},
    )
    assert resp.status_code == 200
    content = _doc_context_path(dirs, "sess-c").read_text(encoding="utf-8")
    assert "first.txt" in content and "First document note." in content
    assert "second.txt" in content and "Second document note." in content


def test_context_text_happy_path(client):
    c, dirs, web_module = client
    (dirs["state_dir"] / "sess-d.json").write_text("{}", encoding="utf-8")

    resp = c.post("/api/context/text", json={"session_id": "sess-d", "text": "Pasted Teams chat snippet."})
    assert resp.status_code == 200
    content = _doc_context_path(dirs, "sess-d").read_text(encoding="utf-8")
    assert "Pasted text" in content
    assert "Pasted Teams chat snippet." in content


def test_context_text_rejects_empty(client):
    c, dirs, web_module = client
    (dirs["state_dir"] / "sess-e.json").write_text("{}", encoding="utf-8")
    resp = c.post("/api/context/text", json={"session_id": "sess-e", "text": "   "})
    assert resp.status_code == 422


def test_context_text_rejects_oversized(client):
    c, dirs, web_module = client
    (dirs["state_dir"] / "sess-f.json").write_text("{}", encoding="utf-8")
    resp = c.post("/api/context/text", json={"session_id": "sess-f", "text": "x" * 20_001})
    assert resp.status_code == 413


def test_context_text_rejects_unknown_session(client):
    c, dirs, web_module = client
    resp = c.post("/api/context/text", json={"session_id": "sess-unknown-2", "text": "Some note."})
    assert resp.status_code == 404


def test_context_text_accepts_while_session_actively_recording(client):
    c, dirs, web_module = client
    web_module.active_session_id = "sess-recording-2"
    try:
        resp = c.post("/api/context/text", json={"session_id": "sess-recording-2", "text": "Live pasted note."})
        assert resp.status_code == 200
    finally:
        web_module.active_session_id = None


def test_upload_omitted_session_id_falls_back_to_active_session(client):
    """P3.7: the mid-recording 'Add Context' button never sends session_id
    at all -- same implicit-session convention as POST /api/record/highlight
    -- so this must resolve to active_session_id, not 422/404."""
    c, dirs, web_module = client
    web_module.active_session_id = "sess-implicit"
    try:
        resp = c.post(
            "/api/context/upload",
            files={"file": ("note.txt", b"Implicit session note.", "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "sess-implicit"
    finally:
        web_module.active_session_id = None


def test_upload_omitted_session_id_not_recording_returns_400(client):
    c, dirs, web_module = client
    resp = c.post(
        "/api/context/upload",
        files={"file": ("note.txt", b"Note.", "text/plain")},
    )
    assert resp.status_code == 400


def test_context_text_omitted_session_id_falls_back_to_active_session(client):
    c, dirs, web_module = client
    web_module.active_session_id = "sess-implicit-2"
    try:
        resp = c.post("/api/context/text", json={"text": "Implicit session pasted note."})
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "sess-implicit-2"
    finally:
        web_module.active_session_id = None


def test_context_text_omitted_session_id_not_recording_returns_400(client):
    c, dirs, web_module = client
    resp = c.post("/api/context/text", json={"text": "Note."})
    assert resp.status_code == 400


def test_server_status_reports_ocr_availability(client):
    """P3.6: GET /api/server/status must surface whether Tesseract OCR is
    installed, so a missing install is visible ahead of time (System tab)
    rather than only surfacing the first time someone uploads a screenshot."""
    c, dirs, web_module = client
    resp = c.get("/api/server/status")
    assert resp.status_code == 200
    assert "ocr_available" in resp.json()
    assert isinstance(resp.json()["ocr_available"], bool)


def test_context_endpoints_rejected_cross_origin(client):
    """CSRF hardening (H1) must also cover the new/extended context endpoints."""
    c, dirs, web_module = client
    (dirs["state_dir"] / "sess-g.json").write_text("{}", encoding="utf-8")
    resp = c.post(
        "/api/context/text",
        json={"session_id": "sess-g", "text": "Note."},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403
