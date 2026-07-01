from __future__ import annotations

from transcribe.diarisation import _best_overlap_speaker, apply_diarisation
from transcribe.whisper_runner import TranscriptionResult, TranscriptSegment


def test_apply_diarisation_falls_back_gracefully_without_pyannote(tmp_path):
    """pyannote.audio is NOT installed in this sandbox -- this is a real test of
    the ImportError fallback, not a mocked one."""
    wav_path = tmp_path / "session.wav"
    result = TranscriptionResult(
        session_id="s1",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="hello")],
    )
    output = apply_diarisation(wav_path, result)

    assert output.diarised is False
    assert output.segments[0].speaker is None
    assert output is result or output.segments[0].text == "hello"


def test_best_overlap_speaker_picks_largest_overlap():
    turns = [(0.0, 1.0, "SPEAKER_00"), (1.0, 3.0, "SPEAKER_01")]
    assert _best_overlap_speaker(0.5, 2.5, turns) == "SPEAKER_01"


def test_best_overlap_speaker_returns_none_when_no_overlap():
    turns = [(5.0, 6.0, "SPEAKER_00")]
    assert _best_overlap_speaker(0.0, 1.0, turns) is None
