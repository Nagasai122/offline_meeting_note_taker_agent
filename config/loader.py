"""Typed loader for config/settings.toml.

Kept deliberately dumb: this module's only job is TOML -> dataclasses. It performs
no validation beyond what the dataclass shapes give for free, and no network or
filesystem side effects beyond reading the one file it's given.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass(frozen=True)
class PathsConfig:
    data_dir: str
    tmp_dir: str
    models_dir: str


@dataclass(frozen=True)
class LLMConfig:
    backend: str
    active_profile: str
    host: str
    port: int
    startup_timeout_seconds: float
    health_check_path: str


@dataclass(frozen=True)
class WhisperConfig:
    model: str
    device: str
    compute_type: str
    diarisation_enabled: bool = False


@dataclass(frozen=True)
class PrivacyConfig:
    disable_telemetry_env: list[str]
    tmp_audio_ttl_seconds: int


@dataclass(frozen=True)
class ConcurrencyConfig:
    lock_path: str
    lock_timeout_seconds: int


@dataclass(frozen=True)
class AgentConfig:
    # Hard ceiling on ReAct turns per run (M5) -- prevents a non-converging
    # model from looping indefinitely or burning GPU time pointlessly.
    max_iterations: int = 12
    trace_dir: str = "data/traces"


@dataclass(frozen=True)
class Settings:
    paths: PathsConfig
    llm: LLMConfig
    whisper: WhisperConfig
    privacy: PrivacyConfig
    concurrency: ConcurrencyConfig
    agent: AgentConfig


def load_settings(path: Path) -> Settings:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return Settings(
        paths=PathsConfig(**raw["paths"]),
        llm=LLMConfig(**raw["llm"]),
        whisper=WhisperConfig(**raw["whisper"]),
        privacy=PrivacyConfig(**raw["privacy"]),
        concurrency=ConcurrencyConfig(**raw["concurrency"]),
        # [agent] is optional in settings.toml -- defaults above keep older
        # config files (M1-M4 era) loading without modification.
        agent=AgentConfig(**raw.get("agent", {})),
    )
