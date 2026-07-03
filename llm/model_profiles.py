"""
Registry of local LLM model profiles.

Design intent (per docs/architecture.md):
- The NVFP4 profile is the primary target on Blackwell, but the GGUF fallback is a
  first-class, equally-tested profile — not an emergency rewrite if NVFP4 has rough
  edges (KV-cache quantisation / prefix-caching are named weak spots as of mid-2026).
- VRAM figures here are *declared budgets* used for pre-flight checks, not measured
  truth. M3's acceptance criteria require replacing these with measured numbers
  (idle load AND a worst-case ~90-minute-transcript prompt) before M4/M5 build on top.
- Nothing in this file performs network I/O. Model paths must already exist locally
  (populated by the separate `meeting-agent setup` command) — this registry only
  describes how to *launch* an already-downloaded model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Backend(str, Enum):
    LLAMA_SERVER = "llama-server"
    VLLM = "vllm"


class Quantisation(str, Enum):
    NVFP4 = "nvfp4"
    GGUF_Q4_K_M = "gguf_q4_k_m"


@dataclass(frozen=True)
class ModelProfile:
    name: str
    backend: Backend
    quantisation: Quantisation
    # Path is relative to [paths].models_dir in settings.toml, resolved by the caller.
    weights_path: str
    declared_vram_gb: float
    supports_tool_calling: bool
    notes: str = ""
    extra_launch_args: list[str] = field(default_factory=list)
    # Pinned HF revision (commit SHA) used by `meeting-agent setup` -- the one
    # network step trusts a specific, reviewed snapshot rather than whatever
    # the repo head happens to be at download time (bandit B615 /
    # audit 2026-07 supply-chain recommendation). None = repo head, accepted
    # only for placeholder/comparison profiles that are not the default.
    revision: str | None = None

    def is_within_budget(self, available_vram_gb: float, headroom_gb: float = 2.0) -> bool:
        """
        Pre-flight check only. This is NOT a substitute for the measured baseline
        required by the M3 acceptance criteria (see docs/architecture.md) — it exists
        to fail fast and obviously before attempting to launch a profile that was
        never going to fit, not to certify that one will.
        """
        return self.declared_vram_gb + headroom_gb <= available_vram_gb


# --- Registry -----------------------------------------------------------------

PROFILES: dict[str, ModelProfile] = {
    "nemotron_nvfp4": ModelProfile(
        name="nemotron_nvfp4",
        backend=Backend.LLAMA_SERVER,
        quantisation=Quantisation.NVFP4,
        weights_path="nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4",
        declared_vram_gb=9.0,
        supports_tool_calling=True,
        notes=(
            "Heavyweight profile, retained for comparison only — NOT the default. "
            "MoE, 30B total / ~3B active params, but 'active params' is a per-token "
            "routing fact, not a VRAM fact: ALL expert weights must be resident on "
            "GPU regardless of how few are used per token, so this still demands the "
            "full 30B-class footprint in VRAM/RAM. Declared VRAM figure is an "
            "unmeasured placeholder; treat 9.0 GB as optimistic. Prefer "
            "'qwen2_5_7b_gguf' unless you have specifically verified this profile's "
            "real VRAM usage on your card."
        ),
    ),
    "qwen3_32b_nvfp4": ModelProfile(
        name="qwen3_32b_nvfp4",
        backend=Backend.VLLM,
        quantisation=Quantisation.NVFP4,
        weights_path="Qwen/Qwen3-32B-NVFP4",  # placeholder path, confirm at setup time
        declared_vram_gb=15.0,
        supports_tool_calling=True,
        notes=(
            "Alternate profile, dense 32B — stronger raw reasoning but leaves very "
            "little VRAM headroom on a 12-16GB card. Treat as a smoke-tested "
            "alternative, not the default."
        ),
    ),
    "gguf_fallback": ModelProfile(
        name="gguf_fallback",
        backend=Backend.LLAMA_SERVER,
        quantisation=Quantisation.GGUF_Q4_K_M,
        weights_path="unsloth/NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning-GGUF/Q4_K_M",
        declared_vram_gb=10.0,
        supports_tool_calling=True,
        notes=(
            "Same caveat as nemotron_nvfp4: this is still the 30B-class MoE family, "
            "just re-quantised — quantisation shrinks the file on disk, it does not "
            "shrink the resident expert set. '--n-gpu-layers 99' below forces every "
            "layer (hence every expert) onto the GPU regardless of routing, which is "
            "the most likely cause of VRAM overcommit/CPU spillover (and the severe "
            "per-token latency that produces) on a 12-16GB card. Diagnosed root cause "
            "of the original performance complaint — do not set this as "
            "active_profile without first measuring real VRAM headroom. Retained for "
            "side-by-side comparison against the dense profile below, not as default."
        ),
        # SB-1.2: --parallel 1 for single-user KV cache isolation.
        extra_launch_args=["--ctx-size", "4096", "--n-gpu-layers", "99", "--parallel", "1"],
    ),
    "qwen2_5_7b_gguf": ModelProfile(
        name="qwen2_5_7b_gguf",
        backend=Backend.LLAMA_SERVER,
        quantisation=Quantisation.GGUF_Q4_K_M,
        weights_path="Qwen/Qwen2.5-7B-Instruct-GGUF/Q4_K_M",
        declared_vram_gb=6.0,
        supports_tool_calling=True,
        notes=(
            "Recommended default for 12-16GB cards. Dense (not MoE) 7B model, so "
            "'--n-gpu-layers 99' below is safe here — every layer genuinely is used "
            "for every token, and the full Q4_K_M quantised footprint is ~4.5GB, "
            "comfortably inside budget with real headroom for KV cache and a "
            "long-transcript context. Weaker raw reasoning than the 30B-class "
            "profiles above, but reliably fast and fully GPU-resident, which the "
            "trace evidence from the 30B profiles showed mattered more in practice "
            "for this tool's short, structured ReAct turns. Run "
            "`meeting-agent setup --profile qwen2_5_7b_gguf` to fetch it; weights "
            "are not bundled."
        ),
        # SB-1.2: --parallel 1 enforces single-request processing so no two
        # sessions share KV cache slots (single-user tool; throughput is irrelevant).
        extra_launch_args=["--ctx-size", "8192", "--n-gpu-layers", "99", "--parallel", "1"],
        revision="bb5d59e06d9551d752d08b292a50eb208b07ab1f",  # verified 2026-07-03
    ),
    "qwen2_5_3b_gguf": ModelProfile(
        name="qwen2_5_3b_gguf",
        backend=Backend.LLAMA_SERVER,
        quantisation=Quantisation.GGUF_Q4_K_M,
        weights_path="Qwen/Qwen2.5-3B-Instruct-GGUF/Q4_K_M",
        declared_vram_gb=2.5,
        supports_tool_calling=False,
        notes=(
            "Fast 3B extraction-only profile (Model M.1). No tool calling — not "
            "suitable for the ReAct agent loop. Intended for per-chunk extraction "
            "and context summarisation tasks where the 7B model's latency is a "
            "bottleneck. ~2GB footprint, fits alongside the main 7B on a 12GB card "
            "if loaded sequentially. Use select_profile_for_task() to route. "
            "Run `meeting-agent setup --profile qwen2_5_3b_gguf` to fetch."
        ),
        extra_launch_args=["--ctx-size", "4096", "--n-gpu-layers", "99", "--parallel", "1"],
        revision="7dabda4d13d513e3e842b20f0d435c732f172cbe",  # verified 2026-07-03
    ),
}


FAST_TASKS = {"extraction", "context_summary"}


def select_profile_for_task(task: str, default_profile: str, models_dir: "Path | None" = None) -> str:
    """Model M.3: route fast, non-tool-calling tasks to the smaller 3B profile
    when its weights are already on disk, falling back to default_profile otherwise.

    Args:
        task: one of FAST_TASKS ("extraction", "context_summary") or any other string.
        default_profile: profile name to use when 3B is not applicable or missing.
        models_dir: if provided, the 3B weights existence is verified before routing.

    Returns:
        The profile name that should be used for this task.
    """
    if task not in FAST_TASKS:
        return default_profile
    fast_profile_name = "qwen2_5_3b_gguf"
    if fast_profile_name not in PROFILES:
        return default_profile
    if models_dir is not None:
        from pathlib import Path as _Path
        weights = _Path(models_dir) / PROFILES[fast_profile_name].weights_path
        if not weights.exists():
            return default_profile
    return fast_profile_name


def get_profile(name: str) -> ModelProfile:
    try:
        return PROFILES[name]
    except KeyError as exc:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unknown model profile '{name}'. Known profiles: {known}") from exc


def resolve_weights_path(profile: ModelProfile, models_dir: Path) -> Path:
    """Resolve a profile's weights_path against the local models directory.

    Raises FileNotFoundError if the weights have not been downloaded yet — this is
    the explicit boundary between `setup` (network-permitted) and runtime
    (network-forbidden): runtime never reaches out to fetch what's missing here.
    """
    resolved = models_dir / profile.weights_path
    if not resolved.exists():
        raise FileNotFoundError(
            f"Weights for profile '{profile.name}' not found at {resolved}. "
            "Run `meeting-agent setup --profile "
            f"{profile.name}` first (this is the only command allowed to use the network)."
        )
    if resolved.is_dir() and profile.backend == Backend.LLAMA_SERVER:
        ggufs = list(resolved.glob("*.gguf"))
        if ggufs:
            return ggufs[0]
    return resolved
