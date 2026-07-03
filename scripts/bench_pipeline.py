"""Reproducible local performance benchmark (audit Strand D, 2026-07).

Measures, on the actual workstation, with zero network egress:
  1. faster-whisper transcription throughput per model size (the WAV is
     synthesised offline via Windows SAPI TTS -- see --make-wav);
  2. local LLM extraction latency at representative prompt sizes, through
     the project's own server_manager + HttpLLMClient.

Usage:
    python scripts/bench_pipeline.py --make-wav bench.wav   # once, Windows only
    python scripts/bench_pipeline.py --wav bench.wav
    python scripts/bench_pipeline.py --llm                  # needs models/ weights
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

_BENCH_PARAGRAPH = (
    "Sarah said the Redis cluster handles forty thousand requests per second in staging "
    "but latency spikes during failover remain. Tom will draft the architecture decision "
    "record by Thursday. Priya will schedule the finance follow-up and prepare the variance "
    "analysis before month end. James collects interview feedback by Friday. Nina prepares "
    "the vendor comparison matrix with pricing and service level commitments. "
)


def make_wav(out_path: Path) -> None:
    """Synthesise ~8 minutes of speech offline via Windows SAPI (no network)."""
    import subprocess

    script = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{out_path}', $fmt)
1..6 | ForEach-Object {{ $s.Speak(@'
{_BENCH_PARAGRAPH * 4}
'@) }}
$s.Dispose()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")


def bench_whisper(wav: Path, models: list[str]) -> None:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import transcribe.whisper_runner  # noqa: F401 - runs load_cuda_dlls() on import
    from faster_whisper import WhisperModel

    audio_seconds = (wav.stat().st_size - 44) / 32000.0  # 16kHz 16-bit mono
    for model_name in models:
        t0 = time.perf_counter()
        m = WhisperModel(model_name, device="cuda", compute_type="int8_float16")
        load_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        segments, _info = m.transcribe(str(wav), beam_size=2, vad_filter=True)
        segs = list(segments)
        tr_s = time.perf_counter() - t1
        print(
            f"{model_name:10s} load={load_s:6.1f}s transcribe={tr_s:6.1f}s "
            f"speed={audio_seconds / tr_s:5.1f}x realtime "
            f"({audio_seconds / 60:.1f} audio-min in {tr_s / 60:.2f} wall-min) "
            f"segments={len(segs)}"
        )
        del m


def bench_llm(port: int = 8091) -> None:
    from llm.client import HttpLLMClient
    from llm.model_profiles import get_profile
    from llm.server_manager import start_server
    from mcp_server.meeting_type import MeetingType
    from mcp_server.tools.extraction import _SYSTEM_PROMPTS
    from transcribe.chunker import estimate_tokens

    handle = start_server(
        profile=get_profile("qwen2_5_7b_gguf"),
        models_dir=REPO_ROOT / "models",
        host="127.0.0.1",
        port=port,
        disable_telemetry_env=["HF_HUB_OFFLINE=1", "DO_NOT_TRACK=1"],
        health_check_path="/health",
        startup_timeout_seconds=300,
    )
    try:
        client = HttpLLMClient(base_url=handle.base_url)
        sysprompt = _SYSTEM_PROMPTS[MeetingType.PROJECT].replace("{recording_date_iso}", "2026-07-03")
        for label, mult in [("short 10-min meeting", 12), ("full chunker-sized chunk", 55)]:
            transcript = "CURRENT MEETING TRANSCRIPT:\n" + _BENCH_PARAGRAPH * mult
            est = estimate_tokens(sysprompt) + estimate_tokens(transcript)
            t0 = time.perf_counter()
            client.complete(sysprompt, transcript)
            warm = time.perf_counter() - t0
            t0 = time.perf_counter()
            client.complete(sysprompt, transcript)
            timed = time.perf_counter() - t0
            print(f"{label:28s} prompt~{est:5d} est-tok  warm={warm:6.2f}s timed={timed:6.2f}s")
    finally:
        handle.stop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--make-wav", type=Path, help="Synthesise the benchmark WAV here (Windows only)")
    ap.add_argument("--wav", type=Path, help="Benchmark faster-whisper on this WAV")
    ap.add_argument("--models", default="base,medium,large-v3")
    ap.add_argument("--llm", action="store_true", help="Benchmark local LLM extraction latency")
    args = ap.parse_args()

    if args.make_wav:
        make_wav(args.make_wav)
    if args.wav:
        bench_whisper(args.wav, args.models.split(","))
    if args.llm:
        bench_llm()


if __name__ == "__main__":
    main()
