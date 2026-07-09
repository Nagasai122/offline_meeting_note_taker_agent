"""Tests for Phase 2 stalled-session detection + resume (cli/web.py):

    GET  /api/sessions/stalled
    POST /api/sessions/{session_id}/resume

plus the LLM-unavailable behaviour in run_pipeline / run_extraction_only that
makes "stalled" (as opposed to terminal FAILED) possible in the first place:
LlmUnavailableError is raised at the readiness gate and _fail_session_best_effort
is deliberately skipped for it, so the session stays at whichever state it
already reached (TRANSCRIBED for run_pipeline, TRANSCRIBED or EXTRACTED for
run_extraction_only) instead of being forced to terminal FAILED.

Follows the fixture pattern in tests/cli/test_web.py (a _FakeSettings stand-in
patched onto cli.web.settings, TestClient for the HTTP-level assertions) and
the direct-function-call pattern used for async pipeline internals -- there is
no pytest-asyncio configured in this project, so async entry points are driven
with a plain asyncio.run() inside an ordinary (sync) test function.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcp_server.state import State, create_session, load_session_state, transition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def dirs(tmp_path):
    d = {
        "data_dir":     tmp_path / "data",
        "meetings_dir": tmp_path / "data" / "meetings",
        "state_dir":    tmp_path / "data" / "state",
        "tmp_dir":      tmp_path / "data" / "tmp",
        "lock_path":    tmp_path / "data" / "state" / ".lock",
    }
    for k in ("meetings_dir", "state_dir", "tmp_dir"):
        d[k].mkdir(parents=True, exist_ok=True)
    return d


class _FakeSettings:
    def __init__(self, dirs):
        class paths:
            data_dir = str(dirs["data_dir"])
            tmp_dir = str(dirs["tmp_dir"])
        class llm:
            host = "127.0.0.1"
            port = 8199  # arbitrary, nothing real should be listening here
            health_check_path = "/health"
            startup_timeout_seconds = 1  # keep the (unused-in-these-tests) real gate fast
        class concurrency:
            lock_path = str(dirs["lock_path"])
            lock_timeout_seconds = 1.0
        class privacy:
            tmp_audio_ttl_seconds = 3600
        class whisper:
            device = "cpu"
            compute_type = "int8"
            model = "base"
        self.paths = paths
        self.llm = llm
        self.concurrency = concurrency
        self.privacy = privacy
        self.whisper = whisper


@pytest.fixture()
def client(dirs):
    import cli.web as web_module

    with patch.object(web_module, "settings", _FakeSettings(dirs)):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c, dirs


@pytest.fixture(autouse=True)
def _reset_web_globals():
    """cli.web's processing/_processing_session_id/pipeline_stage/pipeline_error
    are module-level globals -- reset them around every test in this file so
    one test's pipeline run can't leak into the next (the existing test_web.py
    suite doesn't need this because it never drives run_pipeline/run_extraction_only
    directly, but this file does)."""
    import cli.web as web_module

    yield
    web_module.processing = False
    web_module._processing_session_id = None
    web_module.pipeline_stage = None
    web_module.pipeline_error = None


# ---------------------------------------------------------------------------
# LLM-unavailable: run_pipeline leaves the session at TRANSCRIBED, not FAILED
# ---------------------------------------------------------------------------

def test_run_pipeline_llm_unavailable_leaves_session_transcribed(dirs):
    import cli.web as web_module

    session_id = "stopped-session-1"
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0, initial_state=State.STOPPED)

    async def _fake_run_subprocess(args, extra_env=None):
        # Simulate the real `cli.main process` subprocess: it is the one that
        # actually calls state_mod.transition(... TRANSCRIBED ...), not
        # run_pipeline itself -- run_pipeline only shells out to it.
        transition(
            dirs["state_dir"], session_id, State.TRANSCRIBED,
            dirs["lock_path"], 1.0, transcript_path="dummy.md",
        )
        return 0, "ok"

    async def _fake_wait_ready(*a, **kw):
        raise TimeoutError("LLM server did not become healthy within 1s")

    def _fake_spawn():
        return None, None

    with patch.object(web_module, "settings", _FakeSettings(dirs)), \
         patch.object(web_module, "_run_subprocess", _fake_run_subprocess), \
         patch.object(web_module, "_wait_for_llm_ready", _fake_wait_ready), \
         patch.object(web_module, "_spawn_serve_subprocess_with_log", _fake_spawn):
        asyncio.run(web_module.run_pipeline(session_id))

    final = load_session_state(dirs["state_dir"], session_id)
    assert final.state == State.TRANSCRIBED  # NOT FAILED
    assert final.metadata.get("error") is None  # _fail_session_best_effort never ran

    assert web_module.pipeline_error is not None
    assert "resum" in web_module.pipeline_error.lower()
    assert "agent-run" in web_module.pipeline_error
    assert web_module.pipeline_stage == "ERROR"
    assert web_module.processing is False


def test_run_pipeline_other_failure_still_fails_session(dirs):
    """Control case: a non-LLM failure (e.g. the transcribe subprocess itself
    failing) must still transition the session to FAILED as before -- only
    LlmUnavailableError gets the special treatment."""
    import cli.web as web_module

    session_id = "stopped-session-2"
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0, initial_state=State.STOPPED)

    async def _fake_run_subprocess(args, extra_env=None):
        return 1, "boom: whisper crashed"

    with patch.object(web_module, "settings", _FakeSettings(dirs)), \
         patch.object(web_module, "_run_subprocess", _fake_run_subprocess):
        asyncio.run(web_module.run_pipeline(session_id))

    final = load_session_state(dirs["state_dir"], session_id)
    assert final.state == State.FAILED
    assert "boom" in final.metadata.get("error", "")


# ---------------------------------------------------------------------------
# LLM-unavailable: run_extraction_only leaves TRANSCRIBED/EXTRACTED sessions alone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("starting_state", [State.TRANSCRIBED, State.EXTRACTED])
def test_run_extraction_only_llm_unavailable_leaves_session_in_place(dirs, starting_state):
    import cli.web as web_module

    session_id = f"resumable-{starting_state.value.lower()}"
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0, initial_state=starting_state)

    async def _fake_wait_ready(*a, **kw):
        raise TimeoutError("LLM server did not become healthy within 1s")

    def _fake_spawn():
        return None, None

    with patch.object(web_module, "settings", _FakeSettings(dirs)), \
         patch.object(web_module, "_wait_for_llm_ready", _fake_wait_ready), \
         patch.object(web_module, "_spawn_serve_subprocess_with_log", _fake_spawn):
        asyncio.run(web_module.run_extraction_only(session_id))

    final = load_session_state(dirs["state_dir"], session_id)
    assert final.state == starting_state  # unchanged, NOT FAILED
    assert final.metadata.get("error") is None

    assert web_module.pipeline_error is not None
    assert "resum" in web_module.pipeline_error.lower()
    assert web_module.pipeline_stage == "ERROR"
    assert web_module.processing is False


def test_run_extraction_only_agent_failure_still_fails_session(dirs):
    import cli.web as web_module

    session_id = "extraction-fails"
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0, initial_state=State.TRANSCRIBED)

    async def _fake_run_subprocess(args, extra_env=None):
        return 1, "agent crashed"

    async def _fake_wait_ready(*a, **kw):
        return None  # LLM "ready" immediately -- this test is about the agent-run step, not the gate

    def _fake_spawn():
        return None, None

    with patch.object(web_module, "settings", _FakeSettings(dirs)), \
         patch.object(web_module, "_run_subprocess", _fake_run_subprocess), \
         patch.object(web_module, "_wait_for_llm_ready", _fake_wait_ready), \
         patch.object(web_module, "_spawn_serve_subprocess_with_log", _fake_spawn):
        asyncio.run(web_module.run_extraction_only(session_id))

    final = load_session_state(dirs["state_dir"], session_id)
    assert final.state == State.FAILED


# ---------------------------------------------------------------------------
# GET /api/sessions/stalled
# ---------------------------------------------------------------------------

def test_stalled_flags_only_stopped_transcribed_extracted(client):
    c, dirs = client

    create_session(dirs["state_dir"], "stuck-stopped", dirs["lock_path"], 1.0, initial_state=State.STOPPED)
    create_session(dirs["state_dir"], "stuck-transcribed", dirs["lock_path"], 1.0, initial_state=State.TRANSCRIBED)
    create_session(dirs["state_dir"], "stuck-extracted", dirs["lock_path"], 1.0, initial_state=State.EXTRACTED)

    create_session(dirs["state_dir"], "awaiting-review", dirs["lock_path"], 1.0, initial_state=State.EXTRACTED)
    transition(dirs["state_dir"], "awaiting-review", State.PROPOSED, dirs["lock_path"], 1.0)

    create_session(dirs["state_dir"], "done", dirs["lock_path"], 1.0, initial_state=State.EXTRACTED)
    transition(dirs["state_dir"], "done", State.PROPOSED, dirs["lock_path"], 1.0)
    transition(dirs["state_dir"], "done", State.REVIEWED, dirs["lock_path"], 1.0)
    transition(dirs["state_dir"], "done", State.APPLIED, dirs["lock_path"], 1.0)

    create_session(dirs["state_dir"], "gone-wrong", dirs["lock_path"], 1.0, initial_state=State.RECORDING)
    transition(dirs["state_dir"], "gone-wrong", State.FAILED, dirs["lock_path"], 1.0)

    resp = c.get("/api/sessions/stalled")
    assert resp.status_code == 200
    stalled = resp.json()["stalled"]
    ids = {s["session_id"] for s in stalled}
    assert ids == {"stuck-stopped", "stuck-transcribed", "stuck-extracted"}
    states_by_id = {s["session_id"]: s["state"] for s in stalled}
    assert states_by_id["stuck-stopped"] == "STOPPED"
    assert states_by_id["stuck-transcribed"] == "TRANSCRIBED"
    assert states_by_id["stuck-extracted"] == "EXTRACTED"
    assert all(s["stalled"] is True for s in stalled)


def test_stalled_excludes_the_currently_processing_session(client):
    c, dirs = client
    import cli.web as web_module

    create_session(dirs["state_dir"], "mid-flight", dirs["lock_path"], 1.0, initial_state=State.TRANSCRIBED)
    create_session(dirs["state_dir"], "genuinely-stuck", dirs["lock_path"], 1.0, initial_state=State.TRANSCRIBED)

    web_module.processing = True
    web_module._processing_session_id = "mid-flight"

    resp = c.get("/api/sessions/stalled")
    assert resp.status_code == 200
    ids = {s["session_id"] for s in resp.json()["stalled"]}
    assert ids == {"genuinely-stuck"}


def test_stalled_empty_when_no_sessions(client):
    c, dirs = client
    resp = c.get("/api/sessions/stalled")
    assert resp.status_code == 200
    assert resp.json()["stalled"] == []


# ---------------------------------------------------------------------------
# POST /api/sessions/{session_id}/resume
# ---------------------------------------------------------------------------

def test_resume_invalid_session_id_returns_422(client):
    c, dirs = client
    resp = c.post("/api/sessions/bad id!/resume")
    assert resp.status_code == 422


def test_resume_unknown_session_returns_404(client):
    c, dirs = client
    resp = c.post("/api/sessions/no-such-session/resume")
    assert resp.status_code == 404


def test_resume_returns_409_while_another_pipeline_is_processing(client):
    c, dirs = client
    import cli.web as web_module

    create_session(dirs["state_dir"], "waiting-its-turn", dirs["lock_path"], 1.0, initial_state=State.TRANSCRIBED)
    web_module.processing = True
    web_module._processing_session_id = "some-other-session"

    resp = c.post("/api/sessions/waiting-its-turn/resume")
    assert resp.status_code == 409


def test_resume_transcribed_schedules_extraction_only(client, monkeypatch):
    c, dirs = client
    import cli.web as web_module

    session_id = "resume-transcribed"
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0, initial_state=State.TRANSCRIBED)

    calls = []

    async def _stub_extraction(sid):
        calls.append(sid)

    monkeypatch.setattr(web_module, "run_extraction_only", _stub_extraction)

    resp = c.post(f"/api/sessions/{session_id}/resume")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resumed"
    assert data["from_state"] == "TRANSCRIBED"
    # processing is flipped synchronously by the endpoint itself, before the
    # background task even gets a chance to run -- reliable to assert here.
    assert web_module.processing is True
    assert web_module._processing_session_id == session_id


def test_resume_extracted_also_uses_extraction_only(client, monkeypatch):
    """EXTRACTED reuses run_extraction_only, not a separate code path: see
    run_extraction_only's docstring -- `agent-run` is entirely state-driven
    via its own mandatory get_session_status call plus
    agent/prompts/system_prompt.md's dispatch table, which sends an EXTRACTED
    session straight to propose_todo_update. No new pipeline body is needed."""
    c, dirs = client
    import cli.web as web_module

    session_id = "resume-extracted"
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0, initial_state=State.EXTRACTED)

    calls = []

    async def _stub_extraction(sid):
        calls.append(sid)

    monkeypatch.setattr(web_module, "run_extraction_only", _stub_extraction)

    resp = c.post(f"/api/sessions/{session_id}/resume")
    assert resp.status_code == 200
    data = resp.json()
    assert data["from_state"] == "EXTRACTED"
    assert web_module.processing is True


def test_resume_stopped_reenters_pipeline_at_transcription(client, monkeypatch):
    c, dirs = client
    import cli.web as web_module

    session_id = "resume-stopped"
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0, initial_state=State.STOPPED)

    calls = []

    async def _stub_pipeline(sid, auto_accept=False, whisper_model=None):
        calls.append((sid, auto_accept, whisper_model))

    monkeypatch.setattr(web_module, "run_pipeline", _stub_pipeline)

    resp = c.post(f"/api/sessions/{session_id}/resume")
    assert resp.status_code == 200
    data = resp.json()
    assert data["from_state"] == "STOPPED"
    assert web_module.processing is True


def test_resume_proposed_returns_409_with_reason(client):
    c, dirs = client
    create_session(dirs["state_dir"], "already-proposed", dirs["lock_path"], 1.0, initial_state=State.EXTRACTED)
    transition(dirs["state_dir"], "already-proposed", State.PROPOSED, dirs["lock_path"], 1.0)

    resp = c.post("/api/sessions/already-proposed/resume")
    assert resp.status_code == 409
    assert "PROPOSED" in resp.json()["error"]


def test_resume_failed_returns_409_terminal(client):
    c, dirs = client
    create_session(dirs["state_dir"], "already-failed", dirs["lock_path"], 1.0, initial_state=State.RECORDING)
    transition(dirs["state_dir"], "already-failed", State.FAILED, dirs["lock_path"], 1.0)

    resp = c.post("/api/sessions/already-failed/resume")
    assert resp.status_code == 409


def test_resume_never_touches_todo_md(client, monkeypatch):
    """Resume must never auto-accept: run_pipeline is only ever called with
    auto_accept=False from the resume endpoint, and run_extraction_only has no
    auto-accept branch at all (it only ever runs agent-run, which stops at
    PROPOSED). This asserts the literal call arguments the endpoint uses."""
    c, dirs = client
    import cli.web as web_module

    session_id = "resume-no-auto-accept"
    create_session(dirs["state_dir"], session_id, dirs["lock_path"], 1.0, initial_state=State.STOPPED)

    captured = {}

    async def _stub_pipeline(sid, auto_accept=False, whisper_model=None):
        captured["auto_accept"] = auto_accept

    monkeypatch.setattr(web_module, "run_pipeline", _stub_pipeline)

    resp = c.post(f"/api/sessions/{session_id}/resume")
    assert resp.status_code == 200
    # Give the scheduled background task a moment to actually run so
    # `captured` is populated (fire-and-forget via asyncio.create_task).
    import time
    for _ in range(20):
        if "auto_accept" in captured:
            break
        time.sleep(0.05)
    assert captured.get("auto_accept") is False
