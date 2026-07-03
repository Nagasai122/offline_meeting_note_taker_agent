"""
After-the-fact (batch, non-live) transcription via faster-whisper, and the single
function that owns the privacy-critical handoff: STOPPED -> TRANSCRIBED, with the
source WAV deleted unconditionally regardless of whether transcription succeeds.

This is the module that makes good on "the agent listens and takes notes, it does
not keep a recording." Per docs/architecture.md:
- `transcribe_meeting` is the ONLY caller permitted to delete tmp/<session_id>.wav
  for a session that completed RECORDING normally (the other deletion path is the
  startup orphan sweep in audio_capture/session_buffer.py, for crashed sessions).
- Deletion happens in a `finally` block, so it runs whether transcription raises,
  diarisation raises, or everything succeeds. There is deliberately no code path
  in which a transcription failure leaves the WAV behind "just in case" — that
  would reintroduce exactly the retained-recording risk the user ruled out.
- A consequence, stated plainly rather than buried: if transcription itself fails,
  the audio is still gone. `meeting-agent process <session_id>` cannot be retried
  against the original audio, because it no longer exists. The FAILED state is
  resumable for downstream stages (extraction, proposal) once a transcript exists,
  not for transcription itself. This is a deliberate trade of recoverability for
  the privacy guarantee, consistent with the honesty caveat already documented for
  the audio-deletion path generally.
"""

from __future__ import annotations

import os
import sys

def load_cuda_dlls():
    if sys.platform != "win32":
        return
    import site
    for site_pkg in site.getsitepackages():
        nvidia_path = os.path.join(site_pkg, "nvidia")
        if os.path.exists(nvidia_path):
            for lib in os.listdir(nvidia_path):
                bin_path = os.path.join(nvidia_path, lib, "bin")
                if os.path.exists(bin_path):
                    os.add_dll_directory(bin_path)
                    os.environ["PATH"] = bin_path + os.pathsep + os.environ.get("PATH", "")

load_cuda_dlls()

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass
class TranscriptionResult:
    session_id: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str = "unknown"
    duration_seconds: float = 0.0
    model_name: str = "unknown"
    diarised: bool = False


class TranscriptionError(RuntimeError):
    """Raised when faster-whisper fails to produce a transcript. The caller
    (`transcribe_meeting`) still deletes the source audio when this is raised —
    see module docstring."""


def transcribe_audio(
    wav_path: Path,
    session_id: str,
    model_size: str,
    device: str,
    compute_type: str,
) -> TranscriptionResult:
    """Runs faster-whisper over a complete WAV file. Batch only — there is no
    streaming/partial-result path, by design (see project scope: after-the-fact
    transcription, not live transcription)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscriptionError(
            "faster-whisper is not installed. Install with: pip install faster-whisper"
        ) from exc

    try:
        # Zero-network-egress guarantee: faster-whisper checks HuggingFace Hub at
        # model load time unless told not to.  settings.toml's disable_telemetry_env
        # list applies to the LLM subprocess only; this in-process call needs the
        # same treatment applied directly.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        segments_iter, info = model.transcribe(str(wav_path), beam_size=2, vad_filter=True)
        segments = [
            TranscriptSegment(start=seg.start, end=seg.end, text=seg.text)
            for seg in segments_iter
        ]
    except Exception as exc:  # noqa: BLE001 - any backend failure becomes a typed error
        raise TranscriptionError(f"faster-whisper transcription failed: {exc}") from exc

    return TranscriptionResult(
        session_id=session_id,
        segments=segments,
        language=getattr(info, "language", "unknown"),
        duration_seconds=getattr(info, "duration", 0.0),
        model_name=model_size,
    )


def transcribe_meeting(
    session_id: str,
    tmp_dir: Path,
    meetings_dir: Path,
    model_size: str,
    device: str,
    compute_type: str,
    diarisation_enabled: bool = False,
) -> Path:
    """Orchestrates one STOPPED -> TRANSCRIBED transition. Returns the path to the
    written transcript on success. Always deletes the source WAV and its sidecar,
    success or failure — see module docstring for why this is unconditional."""
    tmp_dir = Path(tmp_dir)
    wav_path = tmp_dir / f"{session_id}.wav"
    sidecar_path = tmp_dir / f"{session_id}.json"

    if not wav_path.exists():
        raise FileNotFoundError(
            f"No audio found for session '{session_id}' at {wav_path}. "
            "It may already have been transcribed, or swept as an orphan."
        )

    try:
        result = transcribe_audio(
            wav_path, session_id, model_size=model_size, device=device, compute_type=compute_type
        )

        if diarisation_enabled:
            from transcribe.diarisation import apply_diarisation

            result = apply_diarisation(wav_path, result)

        from transcribe.postprocess import write_transcript

        return write_transcript(meetings_dir, result)
    finally:
        # Unconditional: runs on the success path above AND on any exception,
        # including FileNotFoundError raised before this block (Python still runs
        # `finally` blocks that wrap the try, but note the existence check above is
        # deliberately outside the try — there is nothing to delete if it never
        # existed, and we should not mask the FileNotFoundError's clarity).
        logger.info("Deleting transient audio for session %s (transcription complete or failed).", session_id)
        if wav_path.exists():
            wav_path.unlink()
        if sidecar_path.exists():
            sidecar_path.unlink()

def transcribe_dual_track(
    session_id: str,
    tmp_dir: Path,
    meetings_dir: Path,
    model_size: str,
    device: str,
    compute_type: str,
) -> Path:
    """Orchestrates dual-track transcription (Mic + Loopback). Returns the path to the
    written transcript on success. Always deletes the source WAVs and their sidecars."""
    
    tmp_dir = Path(tmp_dir)
    mic_wav = tmp_dir / f"{session_id}-mic.wav"
    loop_wav = tmp_dir / f"{session_id}-loop.wav"
    mic_sidecar = tmp_dir / f"{session_id}-mic.json"
    loop_sidecar = tmp_dir / f"{session_id}-loop.json"

    try:
        segments = []
        duration = 0.0

        if loop_wav.exists():
            # Guard against a 0-byte or header-only WAV produced when the record
            # subprocess was killed before its I/O buffer flushed.  44 bytes = WAV
            # header with 0 data frames -- not worth transcribing and libav rejects
            # a 0-byte file with AVERROR_INVALIDDATA.
            if loop_wav.stat().st_size <= 44:
                logger.warning(
                    "Loop WAV for session %s is %d bytes (empty/header-only); "
                    "skipping loopback track.",
                    session_id, loop_wav.stat().st_size,
                )
            else:
                res_loop = transcribe_audio(loop_wav, session_id, model_size, device, compute_type)
                for seg in res_loop.segments:
                    seg.speaker = "Others"
                    segments.append(seg)
                duration = max(duration, res_loop.duration_seconds)
            
        if mic_wav.exists():
            res_mic = transcribe_audio(mic_wav, session_id, model_size, device, compute_type)
            for seg in res_mic.segments:
                seg.speaker = "You"
                segments.append(seg)
            duration = max(duration, res_mic.duration_seconds)
            
        # Sort chronologically by start time
        segments.sort(key=lambda s: s.start)
        
        final_result = TranscriptionResult(
            session_id=session_id,
            segments=segments,
            language="en",
            duration_seconds=duration,
            model_name=model_size,
            diarised=True
        )

        from transcribe.postprocess import write_transcript
        return write_transcript(meetings_dir, final_result)

    finally:
        for p in [mic_wav, loop_wav, mic_sidecar, loop_sidecar]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
