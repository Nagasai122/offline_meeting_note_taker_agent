from __future__ import annotations

import builtins
import json

import pytest

import transcribe.whisper_runner as whisper_runner
from transcribe.whisper_runner import (
    TranscriptionError,
    TranscriptionResult,
    TranscriptSegment,
    transcribe_audio,
    transcribe_meeting,
)


def _touch_session_audio(tmp_dir, session_id: str) -> None:
    (tmp_dir / f"{session_id}.wav").write_bytes(b"RIFF....fake-wav-bytes")
    (tmp_dir / f"{session_id}.json").write_text(json.dumps({"session_id": session_id}))


def test_transcribe_audio_missing_dependency_raises_typed_error(tmp_path, monkeypatch):
    """Forces the `from faster_whisper import WhisperModel` in transcribe_audio
    to raise ImportError regardless of whether faster-whisper is actually
    installed in the interpreter running this test -- a prior version of this
    test relied on the ambient environment not having it installed, which made
    it pass or fail based on which Python ran pytest rather than on the code
    under test."""
    wav_path = tmp_path / "s1.wav"
    wav_path.write_bytes(b"fake")

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("No module named 'faster_whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    with pytest.raises(TranscriptionError, match="faster-whisper is not installed"):
        transcribe_audio(wav_path, "s1", model_size="medium", device="cuda", compute_type="int8_float16")


def test_transcribe_meeting_raises_filenotfound_if_no_audio(tmp_path):
    with pytest.raises(FileNotFoundError):
        transcribe_meeting(
            session_id="missing",
            tmp_dir=tmp_path,
            meetings_dir=tmp_path / "meetings",
            model_size="medium",
            device="cuda",
            compute_type="int8_float16",
        )


def test_transcribe_meeting_deletes_audio_on_success(tmp_path, monkeypatch):
    session_id = "s-success"
    _touch_session_audio(tmp_path, session_id)
    meetings_dir = tmp_path / "meetings"

    fake_result = TranscriptionResult(
        session_id=session_id,
        segments=[TranscriptSegment(start=0.0, end=1.0, text="hello")],
        model_name="medium",
    )
    monkeypatch.setattr(whisper_runner, "transcribe_audio", lambda *a, **kw: fake_result)

    written_path = meetings_dir / f"{session_id}.md"

    def _fake_write_transcript(directory, result):
        directory.mkdir(parents=True, exist_ok=True)
        written_path.write_text("fake transcript")
        return written_path

    monkeypatch.setattr("transcribe.postprocess.write_transcript", _fake_write_transcript)

    result_path = transcribe_meeting(
        session_id=session_id,
        tmp_dir=tmp_path,
        meetings_dir=meetings_dir,
        model_size="medium",
        device="cuda",
        compute_type="int8_float16",
    )

    assert result_path == written_path
    assert not (tmp_path / f"{session_id}.wav").exists()
    assert not (tmp_path / f"{session_id}.json").exists()


def test_transcribe_meeting_deletes_audio_even_on_transcription_failure(tmp_path, monkeypatch):
    """The privacy-critical case: transcription itself fails, but the audio must
    still be gone afterwards -- never retained 'just in case'."""
    session_id = "s-failure"
    _touch_session_audio(tmp_path, session_id)

    def _raise(*args, **kwargs):
        raise TranscriptionError("simulated whisper crash")

    monkeypatch.setattr(whisper_runner, "transcribe_audio", _raise)

    with pytest.raises(TranscriptionError, match="simulated whisper crash"):
        transcribe_meeting(
            session_id=session_id,
            tmp_dir=tmp_path,
            meetings_dir=tmp_path / "meetings",
            model_size="medium",
            device="cuda",
            compute_type="int8_float16",
        )

    assert not (tmp_path / f"{session_id}.wav").exists()
    assert not (tmp_path / f"{session_id}.json").exists()


def test_transcribe_meeting_invokes_diarisation_when_enabled(tmp_path, monkeypatch):
    session_id = "s-diarised"
    _touch_session_audio(tmp_path, session_id)
    meetings_dir = tmp_path / "meetings"

    fake_result = TranscriptionResult(session_id=session_id, segments=[], model_name="medium")
    monkeypatch.setattr(whisper_runner, "transcribe_audio", lambda *a, **kw: fake_result)

    calls = []

    def _fake_apply_diarisation(wav_path, result):
        calls.append(wav_path)
        return result

    monkeypatch.setattr("transcribe.diarisation.apply_diarisation", _fake_apply_diarisation)
    monkeypatch.setattr(
        "transcribe.postprocess.write_transcript",
        lambda directory, result: meetings_dir / f"{session_id}.md",
    )

    transcribe_meeting(
        session_id=session_id,
        tmp_dir=tmp_path,
        meetings_dir=meetings_dir,
        model_size="medium",
        device="cuda",
        compute_type="int8_float16",
        diarisation_enabled=True,
    )

    assert len(calls) == 1
