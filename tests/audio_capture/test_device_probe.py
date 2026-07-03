from __future__ import annotations

import sys

from audio_capture.device_probe import DeviceInfo, format_devices, list_loopback_devices


def test_list_loopback_devices_empty_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert list_loopback_devices() == []


def test_format_devices_empty():
    assert format_devices([]) == "No audio devices found."


def test_format_devices_marks_loopback():
    devices = [
        DeviceInfo(index=0, name="Mic", host_api="ALSA", max_input_channels=1, is_loopback=False),
        DeviceInfo(index=1, name="Speakers (loopback)", host_api="WASAPI", max_input_channels=2, is_loopback=True),
    ]
    output = format_devices(devices)
    assert "[0] Mic" in output
    assert "[LOOPBACK]" in output
    assert output.count("[LOOPBACK]") == 1
