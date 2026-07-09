# Setup guide

Target environment per `docs/architecture.md`: Windows, NVIDIA Blackwell GPU,
12–16GB VRAM. The instructions below generalise to other CUDA GPUs, but the
declared VRAM budgets in `llm/model_profiles.py` were sized for that class of
card — re-check `is_within_budget` against your own hardware before trusting
a profile to fit.

## 1. Install the package

```
python -m venv .venv
.venv\Scripts\activate          # PowerShell: .venv\Scripts\Activate.ps1
pip install -e .
```

`pyaudiowpatch` (WASAPI loopback capture) installs only on `win32`; on other
platforms, loopback recording is unavailable and only `--source microphone`
will work.

Diarisation is optional and not installed by default (`pyannote.audio` carries
heavy dependencies and needs a Hugging Face token for its own model weights):

```
pip install -e ".[diarisation]"
```

v2 features and their dependencies (all installed by the plain `pip install -e .`
above — listed here for awareness, not as extra steps):

| Feature | Dependency | Notes |
|---|---|---|
| Document context (PDF/PPTX/DOCX upload) | `pdfplumber`, `python-pptx`, `python-docx` | Pure Python, no network at runtime. |
| Spreadsheet context (.xlsx upload) | `openpyxl` | Pure Python, no network at runtime. |
| Screenshot/image context (OCR) | `pytesseract`, `Pillow` | `pytesseract` is a thin wrapper around the separate **Tesseract OCR** binary, which is NOT installed by `pip`. Download the Windows installer from the [UB-Mannheim Tesseract build](https://github.com/UB-Mannheim/tesseract/wiki) and ensure `tesseract.exe` is on your `PATH` (or set `pytesseract.pytesseract.tesseract_cmd` to its install location) — otherwise image uploads fail with a clear "OCR unavailable" error rather than a silent no-op. Fully local; no cloud OCR API is ever involved. |
| Email context (drag-and-drop .eml/.msg upload) | `extract-msg` | Core dependency (parses classic-Outlook `.msg` OLE files); `.eml` uses only the stdlib `email` module. |
| Transcript/document upload endpoints | `python-multipart` | Required by FastAPI for multipart form uploads. |
| Past Meetings / Project Meetings / Seminars search | `rank-bm25` | Pure Python. |
| Local due-task reminders | `winotify` | Windows only; `cli/reminders.py` degrades to a no-op (logged warning) if not installed. |
| Mail/calendar context enrichment | `pywin32` (already required) | Needs the local Outlook desktop app open — not a separate install, but genuinely optional at runtime: `cli/mail_sync.py`/`cli/calendar_matcher.py` fail closed (log + continue) if Outlook isn't reachable. |

## 2. Download a model profile (the one network-permitted step)

This is the **only** command in this tool permitted to make network requests.
Confirm a profile name and its declared VRAM budget in
`llm/model_profiles.py` first, then:

```
meeting-agent setup --profile qwen2_5_7b_gguf
```

This downloads weights into `models/` (or wherever `[paths].models_dir` points
in `config/settings.toml`) via `huggingface_hub.snapshot_download`. Internet
access is required for this one command only.

`qwen2_5_7b_gguf` is the recommended default: a dense 7B model with a real,
verifiable VRAM footprint (~4.5GB quantised). The two `nemotron_*` profiles
(`nemotron_nvfp4`, `gguf_fallback`) are 30B-class MoE models retained for
comparison only — their "active params" figure describes per-token routing,
not VRAM usage, since every expert must still be GPU-resident. On a 12-16GB
card these are prone to VRAM overcommit and CPU/RAM spillover, which manifests
as severe per-token latency (confirmed in this project's own run traces:
8-37 seconds for a trivial structured-output turn). Do not set either as
`active_profile` without first measuring real VRAM headroom for your card.

## 3. Review `config/settings.toml`

Defaults are sane for a single-user local setup; the fields worth checking
before first use:

| Setting | Why it matters |
|---|---|
| `[llm].host` | Must stay `127.0.0.1`. Never change to `0.0.0.0` — this is the loopback-only half of the data-egress guarantee. |
| `[llm].active_profile` | Must match a key in `llm/model_profiles.py` and a profile you have actually run `setup` for. |
| `[whisper].device` / `compute_type` | `cuda` / `int8_float16` is the declared VRAM/accuracy balance for a 12–16GB card; drop to `cpu` only as a correctness fallback — expect it to be slow. |
| `[whisper].diarisation_enabled` | Leave `false` unless you installed the `[diarisation]` extra and have a Hugging Face token configured for `pyannote.audio`'s own weights — that, too, is a one-time network step, separate from and not covered by `meeting-agent setup`. |
| `[privacy].disable_telemetry_env` | Do not remove entries from this list. The `VLLM_NO_USAGE_STATS` flag name is explicitly marked in `docs/architecture.md` as "last verified," not eternal — re-check it against whatever vLLM version is actually installed if you switch `[llm].backend` to `vllm`. |

## 4. Verify the offline guarantee yourself

Recommended, not optional, after first setup:

1. Run `scripts/network_audit.py` during a full
   `record → process → review → apply` cycle. It inspects the live process
   tree's sockets (via `psutil`) and fails loudly if anything beyond loopback
   traffic is observed.
2. Disable your machine's network adapter (airplane mode) and repeat that same
   cycle end to end. This is the strongest practical proof available that
   runtime has no hidden external dependency — stronger than reading the code,
   since it cannot be fooled by an import you missed.

## 5. First run

```
meeting-agent devices                          # confirm a device index
meeting-agent serve                             # separate terminal; leave running
meeting-agent record --session-id demo-1
# ... speak, then Ctrl+C ...
meeting-agent process --session-id demo-1
meeting-agent agent-run --session-id demo-1
meeting-agent review --session-id demo-1
meeting-agent apply --session-id demo-1
meeting-agent briefing
```

If anything fails, `docs/runbook.md` covers the failure modes already known
from manual stress testing of this pipeline (double-apply, malformed
`todo.md`, a session stuck in a non-terminal state).
