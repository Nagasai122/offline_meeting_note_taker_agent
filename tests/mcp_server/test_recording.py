from __future__ import annotations

import pytest

from mcp_server.state import State, load_session_state
from mcp_server.tools.recording import start_meeting, stop_meeting, _ACTIVE_BUFFERS
from tests.audio_capture.fakes import FakeAudioSource


@pytest.fixture(autouse=True)
def _clear_active_buffers():
    _ACTIVE_BUFFERS.clear()
    yield
    _ACTIVE_BUFFERS.clear()


def _dirs(tmp_path):
    return tmp_path / "tmp", tmp_path / "state", tmp_path / ".lock"


def test_start_meeting_creates_recording_state(tmp_path):
    tmp_dir, state_dir, lock_path = _dirs(tmp_path)
    result = start_meeting(
        "s1", "microphone", tmp_dir, state_dir, lock_path, 1.0,
        source_factory=lambda: FakeAudioSource(),
    )
    assert result == {"session_id": "s1", "state": "RECORDING"}
    assert "s1" in _ACTIVE_BUFFERS


def test_start_meeting_twice_for_same_session_raises(tmp_path):
    tmp_dir, state_dir, lock_path = _dirs(tmp_path)
    start_meeting("s1", "microphone", tmp_dir, state_dir, lock_path, 1.0, source_factory=lambda: FakeAudioSource())
    with pytest.raises(RuntimeError, match="already recording"):
        start_meeting("s1", "microphone", tmp_dir, state_dir, lock_path, 1.0, source_factory=lambda: FakeAudioSource())


def test_stop_meeting_transitions_to_stopped_and_clears_active_buffer(tmp_path):
    tmp_dir, state_dir, lock_path = _dirs(tmp_path)
    start_meeting("s1", "microphone", tmp_dir, state_dir, lock_path, 1.0, source_factory=lambda: FakeAudioSource())
    result = stop_meeting("s1", state_dir, lock_path, 1.0)

    assert result["state"] == "STOPPED"
    assert result["truncated"] is False
    assert "s1" not in _ACTIVE_BUFFERS
    assert load_session_state(state_dir, "s1").state == State.STOPPED


def test_stop_meeting_with_no_active_recording_raises(tmp_path):
    _, state_dir, lock_path = _dirs(tmp_path)
    with pytest.raises(RuntimeError, match="No active recording"):
        stop_meeting("nope", state_dir, lock_path, 1.0)


def test_stop_meeting_surfaces_truncation(tmp_path):
    tmp_dir, state_dir, lock_path = _dirs(tmp_path)
    start_meeting(
        "s1", "microphone", tmp_dir, state_dir, lock_path, 1.0,
        source_factory=lambda: FakeAudioSource(fail_on_stop=True),
    )
    result = stop_meeting("s1", state_dir, lock_path, 1.0)
    assert result["truncated"] is True
