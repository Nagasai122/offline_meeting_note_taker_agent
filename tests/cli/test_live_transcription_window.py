"""
Tests for _extract_recent_audio_window (cli/web.py), the fix for
live_transcription_worker's O(n^2) hot path: previously the worker
transcribed the ENTIRE growing recording every 3s poll cycle, so total work
across a meeting grew quadratically with elapsed time. This bounds each
cycle's input to a fixed-size trailing window regardless of total recording
length.
"""

from __future__ import annotations

import wave

from cli.web import _extract_recent_audio_window


def _write_wav(path, seconds: float, framerate: int = 16000, n_channels: int = 1, sampwidth: int = 2) -> None:
    n_frames = int(seconds * framerate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        # Distinct byte pattern per frame index so we can tell which frames
        # survived the windowing, not just how many.
        frame_bytes = bytearray()
        for i in range(n_frames):
            value = i % 256
            frame_bytes += bytes([value, 0]) * n_channels
        w.writeframes(bytes(frame_bytes))


def test_window_shorter_than_recording_keeps_only_the_tail(tmp_path):
    src = tmp_path / "session-loop.wav"
    out = tmp_path / "session-live-window.wav"
    _write_wav(src, seconds=60.0, framerate=16000)

    assert _extract_recent_audio_window(src, window_seconds=30.0, out_path=out) is True
    assert out.exists()

    with wave.open(str(out), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        # ~30s worth of frames, not the full 60s -- this is the actual fix:
        # bounded output regardless of source length.
        assert w.getnframes() == int(30.0 * 16000)


def test_window_longer_than_recording_keeps_everything(tmp_path):
    src = tmp_path / "session-mic.wav"
    out = tmp_path / "session-live-window.wav"
    _write_wav(src, seconds=5.0, framerate=16000)

    assert _extract_recent_audio_window(src, window_seconds=30.0, out_path=out) is True
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() == int(5.0 * 16000)


def test_window_output_cost_does_not_grow_with_source_length(tmp_path):
    """The actual regression this fix targets: as the source recording grows
    across a long meeting, each extracted window must stay the same size --
    not grow proportionally, which is what made the old whole-file transcribe
    call do quadratically more total work over a long session."""
    out = tmp_path / "session-live-window.wav"

    short_src = tmp_path / "short.wav"
    _write_wav(short_src, seconds=45.0, framerate=16000)
    _extract_recent_audio_window(short_src, window_seconds=30.0, out_path=out)
    with wave.open(str(out), "rb") as w:
        frames_after_45s = w.getnframes()

    long_src = tmp_path / "long.wav"
    _write_wav(long_src, seconds=3600.0, framerate=16000)  # a 1-hour "seminar"
    _extract_recent_audio_window(long_src, window_seconds=30.0, out_path=out)
    with wave.open(str(out), "rb") as w:
        frames_after_1hr = w.getnframes()

    assert frames_after_45s == frames_after_1hr == int(30.0 * 16000)


def test_handles_stereo_48khz_loopback_format_correctly(tmp_path):
    """The loopback source (as opposed to mic) is 48kHz stereo, not 16kHz
    mono -- the windowing must preserve that format unchanged (re-encoding at
    the source's own rate/channels, not resampling), since faster-whisper's
    normal file-based decode path is what handles resampling further downstream."""
    src = tmp_path / "session-loop.wav"
    out = tmp_path / "session-live-window.wav"
    _write_wav(src, seconds=60.0, framerate=48000, n_channels=2, sampwidth=2)

    assert _extract_recent_audio_window(src, window_seconds=30.0, out_path=out) is True
    with wave.open(str(out), "rb") as w:
        assert w.getnchannels() == 2
        assert w.getframerate() == 48000
        assert w.getnframes() == int(30.0 * 48000)


def test_missing_file_returns_false(tmp_path):
    src = tmp_path / "does-not-exist.wav"
    out = tmp_path / "session-live-window.wav"
    assert _extract_recent_audio_window(src, window_seconds=30.0, out_path=out) is False
    assert not out.exists()


def test_empty_wav_returns_false(tmp_path):
    src = tmp_path / "empty.wav"
    out = tmp_path / "session-live-window.wav"
    _write_wav(src, seconds=0.0)
    assert _extract_recent_audio_window(src, window_seconds=30.0, out_path=out) is False
