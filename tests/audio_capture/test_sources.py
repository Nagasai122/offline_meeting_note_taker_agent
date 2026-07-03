from __future__ import annotations

import sys

import pytest

from audio_capture.sources import LoopbackSource, MicrophoneSource, SourceKind, get_source


def test_loopback_source_refuses_non_windows_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows"):
        LoopbackSource()


def test_get_source_dispatches_microphone():
    source = get_source(SourceKind.MICROPHONE, device_index=3)
    assert isinstance(source, MicrophoneSource)
    assert source.device == 3


def test_get_source_dispatches_loopback_raises_on_non_windows_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows"):
        get_source(SourceKind.LOOPBACK)


@pytest.mark.skipif(sys.platform != "win32", reason="LoopbackSource is gated to win32")
def test_get_source_dispatches_loopback_on_windows():
    source = get_source(SourceKind.LOOPBACK, device_index=2)
    assert isinstance(source, LoopbackSource)
    assert source.device_index == 2


def test_microphone_source_defaults():
    source = MicrophoneSource()
    assert source.samplerate == 16000
    assert source.channels == 1
    assert source.sampwidth == 2
