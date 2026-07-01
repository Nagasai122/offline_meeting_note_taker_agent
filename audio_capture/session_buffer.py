"""
Transient audio buffering for a single meeting session.

Privacy-critical contract (per docs/architecture.md):
- Audio lives only in `tmp/` for the brief window between RECORDING and
  TRANSCRIBED. This module does not delete the WAV itself on success — that is
  M2's job (transcribe/whisper_runner.py), performed unconditionally in a
  `finally` block once a transcript exists. What THIS module guarantees is the
  other half: `sweep_orphaned_audio` closes the gap where a crash leaves a WAV
  in tmp/ with no transcription ever having run. It is called unconditionally
  at CLI startup (see cli/main.py), not only when something looks wrong.
- A sidecar JSON file alongside each WAV records whether the recording
  completed cleanly or was truncated (e.g. by the source erroring out, or the
  machine sleeping mid-call). Truncation is surfaced as a typed, visible state
  rather than silently handing a partial WAV to the transcription stage.
"""

from __future__ import annotations

import json
import logging
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from audio_capture.sources import AudioSource

logger = logging.getLogger(__name__)


@dataclass
class RecordingResult:
    wav_path: Path
    sidecar_path: Path
    truncated: bool
    duration_seconds: float
    frames_written: int


class SessionBuffer:
    """Drives one AudioSource and writes its output to `<tmp_dir>/<session_id>.wav`."""

    def __init__(self, tmp_dir: Path, session_id: str, source: AudioSource) -> None:
        self.tmp_dir = Path(tmp_dir)
        self.session_id = session_id
        self.source = source
        self.wav_path = self.tmp_dir / f"{session_id}.wav"
        self.sidecar_path = self.tmp_dir / f"{session_id}.json"
        self._wave_writer: wave.Wave_write | None = None
        self._frames_written = 0
        self._stream_error: Exception | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("SessionBuffer.start() called twice for the same session.")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self._wave_writer = wave.open(str(self.wav_path), "wb")
        self._wave_writer.setnchannels(self.source.channels)
        self._wave_writer.setsampwidth(self.source.sampwidth)
        self._wave_writer.setframerate(self.source.samplerate)

        self._write_sidecar(status="recording", started_at=time.time())
        try:
            self.source.start(self._on_chunk)
        except Exception:
            # Never leave a half-open WAV writer if the source fails to start.
            self._wave_writer.close()
            self._wave_writer = None
            raise
        self._started = True

    def _on_chunk(self, data: bytes) -> None:
        if self._wave_writer is None:
            return
        try:
            self._wave_writer.writeframes(data)
            self._frames_written += len(data)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
            logger.error("Error writing audio chunk for session %s: %s", self.session_id, exc)
            self._stream_error = exc

    def stop(self) -> RecordingResult:
        if not self._started:
            raise RuntimeError("SessionBuffer.stop() called before start().")

        stop_error: Exception | None = None
        try:
            self.source.stop()
        except Exception as exc:  # noqa: BLE001
            logger.error("Error stopping audio source for session %s: %s", self.session_id, exc)
            stop_error = exc
        finally:
            if self._wave_writer is not None:
                self._wave_writer.close()
                self._wave_writer = None

        truncated = bool(self._stream_error or stop_error or self._frames_written == 0)
        bytes_per_second = self.source.samplerate * self.source.channels * self.source.sampwidth
        duration_seconds = self._frames_written / bytes_per_second if bytes_per_second else 0.0

        self._write_sidecar(
            status="truncated" if truncated else "stopped",
            stopped_at=time.time(),
            duration_seconds=duration_seconds,
            frames_written=self._frames_written,
            error=str(stop_error or self._stream_error) if truncated else None,
        )

        return RecordingResult(
            wav_path=self.wav_path,
            sidecar_path=self.sidecar_path,
            truncated=truncated,
            duration_seconds=duration_seconds,
            frames_written=self._frames_written,
        )

    def _write_sidecar(self, **fields) -> None:
        existing: dict = {}
        if self.sidecar_path.exists():
            try:
                existing = json.loads(self.sidecar_path.read_text())
            except json.JSONDecodeError:
                existing = {}
        existing.update({k: v for k, v in fields.items() if v is not None or k == "error"})
        existing["session_id"] = self.session_id
        self.sidecar_path.write_text(json.dumps(existing, indent=2))


def sweep_orphaned_audio(tmp_dir: Path, ttl_seconds: float = 0.0) -> list[Path]:
    """Delete any `*.wav` (and matching `.json` sidecar) in `tmp_dir` older than
    `ttl_seconds`. The default of 0 means "delete unconditionally, regardless of
    age" — per the binding amendment closing the crash-orphan gap (see
    docs/architecture.md, amendment 1). This is intended to run on every CLI
    invocation, not just at explicit cleanup time.

    Returns the list of WAV paths that were removed, for logging/testing.
    """
    removed: list[Path] = []
    tmp_dir = Path(tmp_dir)
    if not tmp_dir.exists():
        return removed

    now = time.time()
    for wav_path in sorted(tmp_dir.glob("*.wav")):
        age_seconds = now - wav_path.stat().st_mtime
        if age_seconds >= ttl_seconds:
            sidecar_path = wav_path.with_suffix(".json")
            logger.info("Sweeping orphaned audio: %s (age=%.1fs)", wav_path, age_seconds)
            wav_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            removed.append(wav_path)
    return removed
