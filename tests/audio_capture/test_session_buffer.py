from __future__ import annotations

import json
import wave
from pathlib import Path

from audio_capture.session_buffer import SessionBuffer, sweep_orphaned_audio
from tests.audio_capture.fakes import FakeAudioSource, ZeroChunkAudioSource


def test_clean_recording_produces_valid_wav_and_sidecar(tmp_path):
    source = FakeAudioSource(num_chunks=10)
    buf = SessionBuffer(tmp_path, "session-1", source)
    buf.start()
    result = buf.stop()

    assert not result.truncated
    assert result.wav_path.exists()
    assert source.stopped is True

    with wave.open(str(result.wav_path), "rb") as wf:
        assert wf.getnchannels() == source.channels
        assert wf.getframerate() == source.samplerate

    sidecar = json.loads(result.sidecar_path.read_text())
    assert sidecar["status"] == "stopped"
    assert sidecar["session_id"] == "session-1"


def test_stream_failure_on_teardown_marks_truncated_but_keeps_frames(tmp_path):
    source = FakeAudioSource(num_chunks=10, fail_on_stop=True)
    buf = SessionBuffer(tmp_path, "session-2", source)
    buf.start()
    result = buf.stop()

    assert result.truncated
    assert result.frames_written > 0
    sidecar = json.loads(result.sidecar_path.read_text())
    assert sidecar["status"] == "truncated"
    assert "simulated stream interruption" in sidecar["error"]


def test_zero_frames_is_truncated(tmp_path):
    source = ZeroChunkAudioSource()
    buf = SessionBuffer(tmp_path, "session-3", source)
    buf.start()
    result = buf.stop()
    assert result.truncated
    assert result.frames_written == 0

    sidecar = json.loads(result.sidecar_path.read_text())
    assert sidecar["status"] == "truncated"


def test_start_failure_does_not_leave_dangling_wave_writer(tmp_path):
    source = FakeAudioSource(fail_on_start=True)
    buf = SessionBuffer(tmp_path, "session-4", source)
    raised = False
    try:
        buf.start()
    except RuntimeError:
        raised = True
    assert raised
    assert buf._wave_writer is None


def test_double_start_raises(tmp_path):
    source = FakeAudioSource(num_chunks=1)
    buf = SessionBuffer(tmp_path, "session-5", source)
    buf.start()
    buf.stop()
    raised = False
    try:
        buf.start()
    except RuntimeError:
        raised = True
    assert raised


def test_sweep_orphaned_audio_removes_wav_and_sidecar(tmp_path):
    source = FakeAudioSource(num_chunks=3)
    buf = SessionBuffer(tmp_path, "orphan-1", source)
    buf.start()
    buf.stop()

    assert (tmp_path / "orphan-1.wav").exists()
    removed = sweep_orphaned_audio(tmp_path, ttl_seconds=0)

    assert Path(tmp_path / "orphan-1.wav") in removed
    assert not (tmp_path / "orphan-1.wav").exists()
    assert not (tmp_path / "orphan-1.json").exists()


def test_sweep_respects_ttl_for_recent_files(tmp_path):
    source = FakeAudioSource(num_chunks=1)
    buf = SessionBuffer(tmp_path, "recent-1", source)
    buf.start()
    buf.stop()

    removed = sweep_orphaned_audio(tmp_path, ttl_seconds=3600)
    assert removed == []
    assert (tmp_path / "recent-1.wav").exists()


def test_sweep_on_missing_dir_is_a_noop(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert sweep_orphaned_audio(missing) == []
