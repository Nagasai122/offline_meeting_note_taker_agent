"""
Best-effort, offline speaker diarisation via pyannote.audio.

"Best-effort" is the operative phrase, stated up front rather than discovered later:
- pyannote's pretrained pipelines are gated on Hugging Face (require accepting terms
  and a HF token cached locally from a one-time `huggingface-cli login`, performed
  during `meeting-agent setup`, not at runtime).
- The optional dependency may not be installed at all (see pyproject.toml's
  [project.optional-dependencies] diarisation extra).
- The model can fail for input-specific reasons (very short clips, unusual channel
  counts) independent of the above.

In every one of those cases, this module logs a warning and returns the transcript
UNCHANGED rather than raising. Losing speaker labels is an acceptable degradation;
losing the transcript because diarisation hiccupped is not.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from transcribe.whisper_runner import TranscriptionResult, TranscriptSegment

logger = logging.getLogger(__name__)

PYANNOTE_PIPELINE_NAME = "pyannote/speaker-diarization-3.1"


def apply_diarisation(wav_path: Path, result: TranscriptionResult) -> TranscriptionResult:
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        logger.warning(
            "pyannote.audio is not installed; continuing without speaker labels. "
            "Install the 'diarisation' extra to enable this (best-effort feature)."
        )
        return result

    try:
        pipeline = Pipeline.from_pretrained(PYANNOTE_PIPELINE_NAME)
        diarization = pipeline(str(wav_path))
        turns = [
            (turn.start, turn.end, speaker)
            for turn, _, speaker in diarization.itertracks(yield_label=True)
        ]
    except Exception as exc:  # noqa: BLE001 - genuinely best-effort, any failure degrades gracefully
        logger.warning("Diarisation failed (%s); continuing without speaker labels.", exc)
        return result

    labeled_segments = [_label_segment(seg, turns) for seg in result.segments]
    return replace(result, segments=labeled_segments, diarised=True)


def _label_segment(
    segment: TranscriptSegment, turns: list[tuple[float, float, str]]
) -> TranscriptSegment:
    speaker = _best_overlap_speaker(segment.start, segment.end, turns)
    return replace(segment, speaker=speaker)


def _best_overlap_speaker(
    start: float, end: float, turns: list[tuple[float, float, str]]
) -> str | None:
    best_speaker: str | None = None
    best_overlap = 0.0
    for turn_start, turn_end, speaker in turns:
        overlap = min(end, turn_end) - max(start, turn_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = speaker
    return best_speaker
