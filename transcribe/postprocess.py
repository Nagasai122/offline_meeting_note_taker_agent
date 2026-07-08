"""
Formats a TranscriptionResult into the two on-disk artefacts the rest of the
system reads:
- `data/meetings/<session_id>.md`  — human-readable, for the user to skim/correct.
- `data/meetings/<session_id>.json` — structured, for M4's extract_action_items
  tool to parse without re-tokenising Markdown.

Plain-file storage throughout, per docs/architecture.md (no database).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from concurrency.atomic import atomic_write_text
from transcribe.whisper_runner import TranscriptionResult


def format_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_transcript_markdown(result: TranscriptionResult) -> str:
    lines = [
        f"# Meeting transcript — {result.session_id}",
        "",
        f"- Model: {result.model_name}",
        f"- Language: {result.language}",
        f"- Duration: {format_timestamp(result.duration_seconds)}",
        f"- Speaker labels: {'yes (best-effort)' if result.diarised else 'no'}",
        "",
    ]
    for seg in result.segments:
        speaker = seg.speaker or "Speaker"
        timestamp = f"[{format_timestamp(seg.start)}–{format_timestamp(seg.end)}]"
        lines.append(f"**{speaker}** {timestamp}: {seg.text.strip()}")
    return "\n".join(lines) + "\n"


def write_transcript(meetings_dir: Path, result: TranscriptionResult) -> Path:
    """Writes both the Markdown and JSON artefacts. Returns the Markdown path
    (the human-facing one) as the primary return value; the JSON sidecar sits
    alongside it at the same stem with a .json suffix."""
    meetings_dir = Path(meetings_dir)
    meetings_dir.mkdir(parents=True, exist_ok=True)

    md_path = meetings_dir / f"{result.session_id}.md"
    json_path = meetings_dir / f"{result.session_id}.json"

    # atomic_write_text rather than a plain write_text: these two files are
    # the durable record of a whole recording's transcription -- everything
    # downstream (extraction, review, resume-from-stall) reads them, so a
    # crash mid-write truncating either one is exactly the kind of silent
    # data loss this project's other artefact writers already guard against.
    atomic_write_text(md_path, format_transcript_markdown(result))
    atomic_write_text(json_path, json.dumps(asdict(result), indent=2))

    return md_path
