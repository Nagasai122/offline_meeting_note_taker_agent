"""
Parsers for externally-produced transcripts (Whisper JSON, WebVTT, SRT, plain
text) into this project's segment shape: list[{"start": float, "end": float,
"text": str}]. Feeds `import-transcript` (cli/main.py) and
`POST /api/upload/transcript` (cli/web.py), which inject a transcript at
TRANSCRIBED, bypassing RECORDING/STOPPED entirely (architecture_v2.md §8).

Pure stdlib (re/json), zero network, consistent with the rest of this project.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_SPEAKER_LABEL_RE = re.compile(r"^\s*SPEAKER_\w+\s*:\s*", re.IGNORECASE)
_VTT_TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
)
_SRT_TIMESTAMP_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


def _hms_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_whisper_json(path: Path) -> list[dict]:
    """Load a native Whisper-JSON transcript ({"segments": [...]} or a bare list)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Not valid JSON ({exc}) in {path}.") from exc
    if isinstance(raw, dict) and "segments" in raw:
        segments = raw["segments"]
    elif isinstance(raw, list):
        segments = raw
    else:
        raise ValueError(f"Unrecognised Whisper JSON shape in {path}: expected dict with 'segments' or a list.")
    try:
        return [
            {"start": float(seg["start"]), "end": float(seg["end"]), "text": str(seg["text"]).strip()}
            for seg in segments
        ]
    except (KeyError, TypeError, ValueError) as exc:
        # Documented contract: malformed transcript structure surfaces as
        # ValueError, never a bare KeyError/TypeError that callers (the web
        # upload endpoint returns 400 on ValueError) would turn into a 500.
        raise ValueError(f"Malformed Whisper JSON segment in {path}: {exc}") from exc


def parse_vtt(path: Path) -> list[dict]:
    """Parse a WebVTT file into segments, stripping any leading SPEAKER_XX: label."""
    text = path.read_text(encoding="utf-8")
    segments: list[dict] = []
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        m = _VTT_TIMESTAMP_RE.search(block)
        if not m:
            continue
        start = _hms_to_seconds(*m.groups()[0:4])
        end = _hms_to_seconds(*m.groups()[4:8])
        # Everything after the timestamp line is the cue text (may span lines).
        after_timestamp = block[m.end():].strip()
        cue_text = " ".join(line.strip() for line in after_timestamp.splitlines() if line.strip())
        cue_text = _SPEAKER_LABEL_RE.sub("", cue_text).strip()
        if cue_text:
            segments.append({"start": start, "end": end, "text": cue_text})
    return segments


def parse_srt(path: Path) -> list[dict]:
    """Parse an SRT file into segments."""
    text = path.read_text(encoding="utf-8")
    segments: list[dict] = []
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        m = _SRT_TIMESTAMP_RE.search(block)
        if not m:
            continue
        start = _hms_to_seconds(*m.groups()[0:4])
        end = _hms_to_seconds(*m.groups()[4:8])
        after_timestamp = block[m.end():].strip()
        cue_text = " ".join(line.strip() for line in after_timestamp.splitlines() if line.strip())
        cue_text = _SPEAKER_LABEL_RE.sub("", cue_text).strip()
        if cue_text:
            segments.append({"start": start, "end": end, "text": cue_text})
    return segments


def parse_plain_text(path: Path) -> list[dict]:
    """Split plain text into paragraphs, assigning synthetic 30s-wide timestamps."""
    text = path.read_text(encoding="utf-8")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    segments = []
    for i, para in enumerate(paragraphs):
        start = i * 30.0
        segments.append({"start": start, "end": start + 30.0, "text": para})
    return segments


def parse_transcript_file(path: Path) -> list[dict]:
    """Dispatch to the right parser based on file suffix.

    Args:
        path: Path to the transcript file (.json/.vtt/.srt/.txt).

    Returns:
        List of {"start", "end", "text"} segment dicts.

    Raises:
        ValueError: for an unsupported suffix.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_whisper_json(path)
    if suffix == ".vtt":
        return parse_vtt(path)
    if suffix == ".srt":
        return parse_srt(path)
    if suffix == ".txt":
        return parse_plain_text(path)
    raise ValueError(f"Unsupported transcript extension: {suffix!r}")


def segments_to_text(segments: list[dict]) -> str:
    """Join all segment texts with newlines, for writing the `.md` artefact."""
    return "\n".join(seg["text"] for seg in segments)
