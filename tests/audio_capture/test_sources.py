from __future__ import annotations

import sys

import pytest

from audio_capture.sources import LoopbackSource, MicrophoneSource, SourceKind, get_source


def test_loopback_source_refuses_non_windows_platform():
    """This sandbox is Linux, so this is a REAL assertion of the guard, not a
    mocked one: LoopbackSource must refuse to construct here."""
    assert sys.platform != "win32"
    with pytest.raises(RuntimeError, match="Windows"):
        LoopbackSource()


def test_get_source_dispatches_microphone():
    source = get_source(SourceKind.MICROPHONE, device_index=3)
    assert isinstance(source, MicrophoneSource)
    assert source.device == 3


def test_get_source_dispatches_loopback_raises_on_this_platform():
    with pytest.raises(RuntimeError, match="Windows"):
        get_source(SourceKind.LOOPBACK)


def test_microphone_source_defaults():
    source = MicrophoneSource()
    assert source.samplerate == 16000
    assert source.channels == 1
    assert source.sampwidth == 2
