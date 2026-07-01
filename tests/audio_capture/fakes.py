"""Shared fakes for audio_capture tests -- no real audio hardware involved."""

from __future__ import annotations

from audio_capture.sources import AudioSource


class FakeAudioSource(AudioSource):
    """Emits a fixed number of synthetic PCM16 chunks on start(), synchronously.
    Lets SessionBuffer be tested against real file I/O without sounddevice or
    pyaudiowpatch, and without any actual audio hardware."""

    def __init__(
        self,
        samplerate: int = 16000,
        channels: int = 1,
        chunk_bytes: int = 320,
        num_chunks: int = 5,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.chunk_bytes = chunk_bytes
        self.num_chunks = num_chunks
        self.fail_on_start = fail_on_start
        self.fail_on_stop = fail_on_stop
        self.stopped = False

    def start(self, on_chunk) -> None:
        if self.fail_on_start:
            raise RuntimeError("simulated device failure on start")
        for _ in range(self.num_chunks):
            on_chunk(b"\x00\x01" * (self.chunk_bytes // 2))

    def stop(self) -> None:
        if self.fail_on_stop:
            raise RuntimeError("simulated stream interruption (e.g. sleep mid-call)")
        self.stopped = True


class ZeroChunkAudioSource(AudioSource):
    """Starts and stops cleanly but never produces a single frame."""

    samplerate = 16000
    channels = 1

    def start(self, on_chunk) -> None:
        pass

    def stop(self) -> None:
        pass
