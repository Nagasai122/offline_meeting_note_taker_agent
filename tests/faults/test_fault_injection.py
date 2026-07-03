"""Fault-injection tests (audit Strand C).

Covers the failure modes the happy-path suites never exercise:
- malformed/truncated transcripts in every supported import format;
- corrupted data/state/ session files;
- concurrent mutation of todo.md through the capability-gated writers;
- the web upload endpoint's behaviour when handed garbage (regression for
  the audit fix: malformed uploads must be a 400, not an unhandled 500).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from transcribe.import_parsers import parse_transcript_file

# ---------------------------------------------------------------------------
# Malformed transcript files
# ---------------------------------------------------------------------------


def test_truncated_whisper_json_raises_value_error(tmp_path):
    bad = tmp_path / "t.json"
    bad.write_text('{"segments": [{"start": 0.0, "end": 2.0, "te', encoding="utf-8")
    with pytest.raises(ValueError):
        parse_transcript_file(bad)


def test_whisper_json_wrong_shape_raises_value_error(tmp_path):
    bad = tmp_path / "t.json"
    bad.write_text('{"not_segments": true}', encoding="utf-8")
    with pytest.raises(ValueError):
        parse_transcript_file(bad)


def test_whisper_json_segment_missing_keys_raises_value_error(tmp_path):
    """Regression: this used to leak a bare KeyError (=> 500 from the web
    endpoint) instead of the documented ValueError."""
    bad = tmp_path / "t.json"
    bad.write_text('{"segments": [{"start": 0.0}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        parse_transcript_file(bad)


def test_whisper_json_non_numeric_start_raises_value_error(tmp_path):
    bad = tmp_path / "t.json"
    bad.write_text('{"segments": [{"start": "zero", "end": 1, "text": "hi"}]}', encoding="utf-8")
    with pytest.raises(ValueError):
        parse_transcript_file(bad)


def test_vtt_with_garbage_returns_empty_list(tmp_path):
    """A structurally-hopeless VTT yields zero segments (callers treat an
    empty parse as a 400), rather than raising."""
    bad = tmp_path / "t.vtt"
    bad.write_text("WEBVTT\n\nnot a timestamp\ngarbage", encoding="utf-8")
    assert parse_transcript_file(bad) == []


def test_vtt_truncated_mid_timestamp_skips_bad_cue(tmp_path):
    bad = tmp_path / "t.vtt"
    bad.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nGood cue.\n\n00:00:03.0",
        encoding="utf-8",
    )
    segments = parse_transcript_file(bad)
    assert len(segments) == 1
    assert segments[0]["text"] == "Good cue."


def test_srt_with_malformed_index_lines_still_parses_valid_blocks(tmp_path):
    bad = tmp_path / "t.srt"
    bad.write_text(
        "not-a-number\n00:00:01,000 --> 00:00:02,000\nFirst line.\n\n"
        "2\nbroken timestamp line\nSecond line lost.\n",
        encoding="utf-8",
    )
    segments = parse_transcript_file(bad)
    assert [s["text"] for s in segments] == ["First line."]


def test_empty_txt_returns_no_segments(tmp_path):
    empty = tmp_path / "t.txt"
    empty.write_text("", encoding="utf-8")
    assert parse_transcript_file(empty) == []


def test_unsupported_extension_raises_value_error(tmp_path):
    bad = tmp_path / "t.docx"
    bad.write_text("irrelevant", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_transcript_file(bad)


# ---------------------------------------------------------------------------
# Corrupted data/state/
# ---------------------------------------------------------------------------


def test_load_session_state_on_corrupted_json_raises_value_error(tmp_path):
    from mcp_server.state import load_session_state

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "corrupt-1.json").write_text('{"session_id": "corrupt-1", "state": "TRANS', encoding="utf-8")
    with pytest.raises(ValueError):
        load_session_state(state_dir, "corrupt-1")


def test_load_session_state_on_unknown_state_value_raises_value_error(tmp_path):
    from mcp_server.state import load_session_state

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "weird.json").write_text(
        json.dumps({"session_id": "weird", "state": "LIMBO", "history": [], "metadata": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_session_state(state_dir, "weird")


def test_reaper_skips_corrupted_state_files_and_reaps_the_rest(tmp_path):
    """An interrupted agent-run / crashed process can leave a corrupt state
    file; the orphan reaper must not die on it, and must still reap genuine
    orphans alongside it."""
    from mcp_server.state import State, create_session, load_session_state, reap_orphaned_recordings

    state_dir = tmp_path / "state"
    lock = tmp_path / ".lock"
    (state_dir).mkdir()
    (state_dir / "corrupt-1.json").write_text("NOT JSON", encoding="utf-8")
    create_session(state_dir, "orphan-1", lock, 1.0, initial_state=State.RECORDING, pid=999999999)

    reaped = reap_orphaned_recordings(state_dir, lock, 1.0)

    assert reaped == ["orphan-1"]
    assert load_session_state(state_dir, "orphan-1").state == State.FAILED


def test_briefing_survives_corrupted_state_file(tmp_path):
    from cli.briefing import build_daily_briefing

    (tmp_path / "todo.md").write_text("- [ ] one item\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "corrupt.json").write_text("{{{{", encoding="utf-8")

    briefing = build_daily_briefing(tmp_path / "todo.md", state_dir)
    assert briefing["sessions"]["unreadable"] == ["corrupt"]


# ---------------------------------------------------------------------------
# Concurrent todo.md mutation
# ---------------------------------------------------------------------------


def test_concurrent_manual_task_writes_do_not_lose_items(tmp_path):
    """N threads racing through write_manual_task must serialise on the
    FileLock: every item present afterwards, file parsable, no torn writes."""
    from cli.capability import mint_capability_token
    from cli.review_apply import write_manual_task
    from mcp_server.todo import parse_todo

    todo_path = tmp_path / "todo.md"
    todo_path.write_text("")
    lock = tmp_path / ".lock"
    n = 12
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            write_manual_task(
                mint_capability_token(),
                {"description": f"concurrent task {i}"},
                todo_path, lock, lock_timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    parsed = parse_todo(todo_path)
    descriptions = {item.description for item in parsed.items}
    assert descriptions == {f"concurrent task {i}" for i in range(n)}


# ---------------------------------------------------------------------------
# Web upload endpoint: malformed uploads are client errors, not 500s
# ---------------------------------------------------------------------------


@pytest.fixture()
def upload_client(tmp_path):
    import cli.web as web_module

    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)

    class _FakeSettings:
        class paths:
            data_dir = str(tmp_path / "data")
            tmp_dir = str(tmp_path / "tmp")
        class llm:
            host = "127.0.0.1"
            port = 8080
            health_check_path = "/health"
            startup_timeout_seconds = 5
        class concurrency:
            lock_path = str(tmp_path / "data" / "state" / ".lock")
            lock_timeout_seconds = 1.0
        class privacy:
            tmp_audio_ttl_seconds = 3600
        class whisper:
            device = "cpu"
            compute_type = "int8"

    with patch.object(web_module, "settings", _FakeSettings()):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c


def test_upload_truncated_whisper_json_returns_400(upload_client):
    """Regression for the audit fix: this was an unhandled 500 before."""
    resp = upload_client.post(
        "/api/upload/transcript",
        files={"file": ("broken.json", b'{"segments": [{"sta', "application/json")},
        data={"meeting_type": "project-meeting"},
    )
    assert resp.status_code == 400
    assert "Could not parse" in resp.json()["error"]


def test_upload_unparseable_vtt_returns_400_no_segments(upload_client):
    resp = upload_client.post(
        "/api/upload/transcript",
        files={"file": ("empty.vtt", b"WEBVTT\n\ngarbage only", "text/vtt")},
        data={"meeting_type": "seminar"},
    )
    assert resp.status_code == 400


def test_upload_unsupported_extension_returns_400(upload_client):
    resp = upload_client.post(
        "/api/upload/transcript",
        files={"file": ("t.mp3", b"\x00\x01", "audio/mpeg")},
        data={"meeting_type": "is-call"},
    )
    assert resp.status_code == 400


def test_upload_path_traversal_session_id_rejected(upload_client):
    resp = upload_client.post(
        "/api/upload/transcript",
        files={"file": ("ok.txt", b"Some transcript text.", "text/plain")},
        data={"meeting_type": "project-meeting", "session_id": "../../evil"},
    )
    assert resp.status_code == 422
