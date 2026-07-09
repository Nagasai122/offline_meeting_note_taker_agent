"""Typed loader for config/settings.toml.

Kept deliberately dumb: this module's only job is TOML -> dataclasses. It performs
no validation beyond what the dataclass shapes give for free, and no network or
filesystem side effects beyond reading the one file it's given.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
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
class ExportConfig:
    # Obsidian/Markdown-vault export target. Empty string = not configured;
    # the export command/endpoint then requires an explicit --vault path.
    # Deliberately the ONLY place this tool writes outside data/ -- the path
    # is user-chosen configuration, never derived from content, and the MCP
    # layer has no export tool (audit 2026-07 Strand E candidate 4).
    vault_dir: str = ""


@dataclass(frozen=True)
class IdentityConfig:
    # Who "self" means for ownership classification (P2) -- the extraction
    # pipeline injects this into the LLM's context so it can tell "I will
    # send the report" (owner_type="self") apart from "Maria will send the
    # report" (owner_type="institution"/"partner"/etc.) in a transcript.
    # Empty name = not configured; ownership classification falls back to
    # "unknown" rather than guessing, same fail-closed posture as everywhere
    # else the LLM is asked to classify rather than invent.
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    institution: str = ""


@dataclass(frozen=True)
class Settings:
    paths: PathsConfig
    llm: LLMConfig
    whisper: WhisperConfig
    privacy: PrivacyConfig
    concurrency: ConcurrencyConfig
    agent: AgentConfig
    export: ExportConfig = ExportConfig()
    identity: IdentityConfig = IdentityConfig()


def load_settings(path: Path) -> Settings:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return Settings(
        paths=PathsConfig(**raw["paths"]),
        llm=LLMConfig(**raw["llm"]),
        whisper=WhisperConfig(**raw["whisper"]),
        privacy=PrivacyConfig(**raw["privacy"]),
        concurrency=ConcurrencyConfig(**raw["concurrency"]),
        # [agent], [export], and [identity] are optional in settings.toml --
        # defaults above keep older config files (M1-M4 era) loading without
        # modification.
        agent=AgentConfig(**raw.get("agent", {})),
        export=ExportConfig(**raw.get("export", {})),
        identity=IdentityConfig(**raw.get("identity", {})),
    )
