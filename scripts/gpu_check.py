"""
Local, zero-network GPU diagnostic for meeting-agent.

Checks three independent things that all have to be true for "the laptop GPU
is actually being used" to hold, since each layer can silently fall back to
CPU without raising:

1. The NVIDIA driver/CUDA runtime is visible to the OS at all (`nvidia-smi`).
2. faster-whisper's backend (ctranslate2) can see a CUDA device -- this is
   what [whisper].device = "cuda" in config/settings.toml actually depends on.
3. A plain Python CUDA probe via ctranslate2, independent of whisper itself,
   to separate "no GPU" from "GPU present but whisper mis-configured."

Deliberately does NOT attempt to launch llama-server here -- that is a
heavier, longer-running check with its own startup time, and its GPU-layer
offload is verified differently (see the printed instructions at the end).

Usage:
    python scripts/gpu_check.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def _print_header(title: str) -> None:
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def check_nvidia_smi() -> bool:
    _print_header("1. nvidia-smi (driver + CUDA runtime visible to the OS)")
    exe = shutil.which("nvidia-smi")
    if not exe:
        print("FAIL: nvidia-smi not found on PATH. Either there is no NVIDIA "
              "GPU on this laptop, or the driver is not installed/visible to "
              "this shell. Everything downstream will fall back to CPU.")
        return False
    try:
        result = subprocess.run(
            [exe, "--query-gpu=name,driver_version,memory.total,memory.used",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        print(f"FAIL: nvidia-smi exists but could not be run: {exc}")
        return False
    if result.returncode != 0:
        print(f"FAIL: nvidia-smi exited {result.returncode}:\n{result.stderr}")
        return False
    print("OK. GPU(s) detected:")
    for line in result.stdout.strip().splitlines():
        print(f"  - {line}")
    return True


def check_ctranslate2_cuda() -> bool:
    _print_header("2. ctranslate2 CUDA device count (what faster-whisper uses)")
    try:
        import ctranslate2
    except ImportError as exc:
        print(f"FAIL: ctranslate2 is not importable in this environment: {exc}")
        return False
    try:
        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:
        print(f"FAIL: ctranslate2.get_cuda_device_count() raised: {exc}")
        return False
    if count < 1:
        print("FAIL: ctranslate2 reports 0 CUDA devices. This build of "
              "ctranslate2 was likely compiled/installed without CUDA "
              "support, even if nvidia-smi above succeeded -- check that "
              "faster-whisper/ctranslate2 was installed with GPU support "
              "for this CUDA version.")
        return False
    print(f"OK. ctranslate2 sees {count} CUDA device(s).")
    return True


def check_whisper_tiny_on_cuda() -> bool:
    _print_header("3. faster-whisper end-to-end load on device='cuda'")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        print(f"FAIL: faster_whisper not importable: {exc}")
        return False
    try:
        # "tiny" deliberately -- this is a load/device smoke test, not an
        # accuracy check, so the smallest model that proves the device
        # argument was honoured is the right one.
        WhisperModel("tiny", device="cuda", compute_type="int8_float16")
    except Exception as exc:
        print(f"FAIL: WhisperModel(..., device='cuda') raised: {exc}\n"
              "This is the exact call config/settings.toml's [whisper] "
              "section drives -- if this fails, transcription is silently "
              "not using the GPU (or hard-failing, depending on the error).")
        return False
    print("OK. faster-whisper loaded a model on device='cuda' successfully.")
    return True


def main() -> int:
    results = [
        check_nvidia_smi(),
        check_ctranslate2_cuda(),
        check_whisper_tiny_on_cuda(),
    ]

    _print_header("4. llama-server GPU offload (manual check, see instructions)")
    print(
        "This script does not launch llama-server (slow, and `meeting-agent "
        "serve` already does it). Instead, after running `meeting-agent "
        "serve` once, check its printed startup log for a line resembling:\n"
        "    load_tensors: offloaded 33/33 layers to GPU\n"
        "or similar 'offloaded N/N' wording. N/N (all layers) confirms the "
        "active profile's '--n-gpu-layers 99' (see llm/model_profiles.py) "
        "is actually placing every layer on the GPU, not spilling to CPU. "
        "A partial split (e.g. '20/33') means VRAM is insufficient for the "
        "active profile at its configured context size -- see "
        "llm/model_profiles.py's notes on each profile's declared_vram_gb."
    )

    _print_header("Summary")
    labels = ["nvidia-smi", "ctranslate2 CUDA", "faster-whisper on cuda"]
    for label, ok in zip(labels, results):
        print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    print(
        "  [MANUAL] llama-server GPU offload -- check `meeting-agent serve` "
        "output as described above"
    )

    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
