from __future__ import annotations

import json
from pathlib import Path

from transcribe.postprocess import format_timestamp, format_transcript_markdown, write_transcript
from transcribe.whisper_runner import TranscriptionResult, TranscriptSegment


def test_format_timestamp_under_an_hour():
    assert format_timestamp(65) == "01:05"


def test_format_timestamp_over_an_hour():
    assert format_timestamp(3725) == "01:02:05"


def test_format_transcript_markdown_includes_speaker_and_text():
    result = TranscriptionResult(
        session_id="s1",
        segments=[TranscriptSegment(start=0.0, end=2.5, text=" hello team ", speaker="SPEAKER_00")],
        language="en",
        duration_seconds=2.5,
        model_name="medium",
        diarised=True,
    )
    md = format_transcript_markdown(result)
    assert "SPEAKER_00" in md
    assert "hello team" in md
    assert "yes (best-effort)" in md


def test_format_transcript_markdown_no_speaker_label_when_not_diarised():
    result = TranscriptionResult(
        session_id="s2",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="hi")],
        diarised=False,
    )
    md = format_transcript_markdown(result)
    assert "Speaker labels: no" in md
    assert "**Speaker**" in md  # falls back to generic label, not a speaker id


def test_write_transcript_produces_md_and_json(tmp_path):
    result = TranscriptionResult(
        session_id="s3",
        segments=[TranscriptSegment(start=0.0, end=1.0, text="hi")],
        language="en",
        duration_seconds=1.0,
        model_name="medium",
    )
    md_path = write_transcript(tmp_path, result)
    json_path = tmp_path / "s3.json"

    assert md_path == tmp_path / "s3.md"
    assert md_path.exists()
    assert json_path.exists()

    data = json.loads(json_path.read_text())
    assert data["session_id"] == "s3"
    assert data["segments"][0]["text"] == "hi"
