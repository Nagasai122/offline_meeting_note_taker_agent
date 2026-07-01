"""
Audio source abstractions for the two meeting formats this tool was scoped for
(per the Phase 2 clarification answers): in-person/phone via a microphone, and
video calls via WASAPI loopback capture of system audio on Windows.

Design notes:
- Both sources expose the same push-based interface (`start(on_chunk)` / `stop()`)
  so `audio_capture/session_buffer.py` does not need to know which one it is
  driving — it just writes whatever bytes arrive to a WAV file.
- LoopbackSource is hard-gated to win32. Importing pyaudiowpatch is deferred
  into the method bodies (not the module top) so this module can be imported
  and unit-tested for its platform guard on any OS, including this Linux
  sandbox, without pyaudiowpatch being installed at all.
- Sample format is fixed at 16-bit PCM throughout. This is a deliberate
  simplification: faster-whisper resamples internally regardless, so there is
  no accuracy benefit to capturing at a higher bit depth, only more disk I/O
  during the (intentionally brief) lifetime of the tmp/ buffer.
"""

from __future__ import annotations

import logging
import sys
from abc import ABC, abstractmethod
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)

SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM


class SourceKind(str, Enum):
    MICROPHONE = "microphone"
    LOOPBACK = "loopback"


class AudioSource(ABC):
    """Common interface for anything that can stream raw PCM16 audio."""

    samplerate: int
    channels: int
    sampwidth: int = SAMPLE_WIDTH_BYTES

    @abstractmethod
    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        """Begin streaming. `on_chunk` is invoked from a background thread/callback
        with raw PCM16LE bytes as they become available. Must not block for long."""

    @abstractmethod
    def stop(self) -> None:
        """Stop streaming and release the underlying device/stream."""


class MicrophoneSource(AudioSource):
    """Captures from a standard input device (in-person meetings, phone on speaker,
    etc.) via `sounddevice`, which wraps PortAudio and works cross-platform."""

    def __init__(
        self,
        device: int | None = None,
        samplerate: int = 16000,
        channels: int = 1,
        blocksize: int = 1024,
    ) -> None:
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self._stream = None

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        import sounddevice as sd  # deferred import: not needed for LoopbackSource path

        def _callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
            if status:
                logger.warning("MicrophoneSource stream status: %s", status)
            on_chunk(bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.blocksize,
            device=self.device,
            callback=_callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class LoopbackSource(AudioSource):
    """Captures system/virtual-call audio via WASAPI loopback (Windows only).

    This is the path used for video calls: it captures whatever the OS is playing
    out, which is the only reliable cross-application way to hear the other party
    without per-app integration with every conferencing tool.
    """

    def __init__(
        self,
        device_index: int | None = None,
        samplerate: int = 48000,
        channels: int = 2,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "LoopbackSource requires WASAPI (pyaudiowpatch) and only runs on "
                "Windows. Use MicrophoneSource on other platforms."
            )
        self.device_index = device_index
        self.samplerate = samplerate
        self.channels = channels
        self._pa = None
        self._stream = None

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        import pyaudiowpatch as pyaudio  # type: ignore[import-not-found]

        self._pa = pyaudio.PyAudio()
        if self.device_index is None:
            self.device_index = self._default_loopback_device_index(self._pa)

        def _callback(in_data, frame_count, time_info, status):  # noqa: ANN001
            on_chunk(in_data)
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.samplerate,
            input=True,
            input_device_index=self.device_index,
            stream_callback=_callback,
        )
        self._stream.start_stream()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    @staticmethod
    def _default_loopback_device_index(pa) -> int:  # noqa: ANN001
        for device in pa.get_loopback_device_info_generator():
            return device["index"]
        raise RuntimeError(
            "No WASAPI loopback device found. Run `meeting-agent devices` to "
            "inspect what's available, or pass --device-index explicitly."
        )


def get_source(kind: SourceKind, device_index: int | None = None, **kwargs) -> AudioSource:
    if kind == SourceKind.MICROPHONE:
        return MicrophoneSource(device=device_index, **kwargs)
    if kind == SourceKind.LOOPBACK:
        return LoopbackSource(device_index=device_index, **kwargs)
    raise ValueError(f"Unknown source kind: {kind}")  # pragma: no cover - exhaustive enum
