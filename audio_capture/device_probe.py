"""
Lists available audio input and WASAPI loopback devices, so a user can pick the
right --device-index for `meeting-agent record` rather than guessing. Device
enumeration for loopback devices on Windows is notoriously fiddly (the loopback
"device" is a virtual mirror of an output device, not a true input device), so
surfacing this explicitly is worth the small amount of code.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceInfo:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    is_loopback: bool


def list_microphone_devices() -> list[DeviceInfo]:
    import sounddevice as sd

    devices = []
    hostapis = sd.query_hostapis()
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            devices.append(
                DeviceInfo(
                    index=idx,
                    name=dev["name"],
                    host_api=hostapis[dev["hostapi"]]["name"],
                    max_input_channels=dev["max_input_channels"],
                    is_loopback=False,
                )
            )
    return devices


def list_loopback_devices() -> list[DeviceInfo]:
    if sys.platform != "win32":
        return []  # pyaudiowpatch is Windows-only; nothing to enumerate elsewhere.

    import pyaudiowpatch as pyaudio  # type: ignore[import-not-found]

    pa = pyaudio.PyAudio()
    devices = []
    try:
        for dev in pa.get_loopback_device_info_generator():
            devices.append(
                DeviceInfo(
                    index=dev["index"],
                    name=dev["name"],
                    host_api="WASAPI (loopback)",
                    max_input_channels=dev["maxInputChannels"],
                    is_loopback=True,
                )
            )
    finally:
        pa.terminate()
    return devices


def list_all_devices() -> list[DeviceInfo]:
    return list_microphone_devices() + list_loopback_devices()


def format_devices(devices: list[DeviceInfo]) -> str:
    if not devices:
        return "No audio devices found."
    lines = []
    for d in devices:
        tag = " [LOOPBACK]" if d.is_loopback else ""
        lines.append(f"  [{d.index}] {d.name} ({d.host_api}, {d.max_input_channels} ch){tag}")
    return "\n".join(lines)
