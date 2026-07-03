# Audit Log — meeting-agent autonomous audit, 2026-07

Running log per the audit brief. Appended after each strand; never overwritten.

Executor: Claude Fable 5 (Claude Code CLI).
Prior art consulted before any fresh work:
- `docs/code_review_2026_07_01.md` — issue exclusion list built from its
  "Resolution status" section (C1, C2, S1, S2, S3, S5, R5, R8, M1 and the
  persona-driven fixes are RESOLVED; R1–R4, R6, R7, M2–M5 and several UX
  gaps are KNOWN-OPEN and are excluded from fresh findings unless regressed).
- `docs/fable_audit_report.md` (2026-07-02) — prior repair cycle. Its five
  invariants (INV-1..INV-5) and six quality gates are treated as the
  existing verification mechanism to be re-confirmed, not re-invented.

---

## Strand A — Security / zero-egress audit

### Plan (written before execution)

1. Extract the exact stated invariant from `docs/architecture.md`
   (§ Data-egress guarantee, items 1–4) and its verification mechanism
   (`scripts/network_audit.py`, runtime psutil egress monitor).
2. Static sweep: every import of a network-capable library (`httpx`,
   `socket`, `urllib`, `requests`, `win32com`, `subprocess` with network
   binaries) across production code; classify each as loopback-only,
   setup-only, or a finding.
3. Audit the capability token (`cli/capability.py`): entropy, storage,
   lifetime, and CLI-vs-web enforcement parity (`cli/main.py apply` vs the
   web apply endpoints in `cli/web.py`).
4. Enumerate the 8 MCP tools' write scopes from `mcp_server/server.py` +
   `mcp_server/tools/*`; verify no path escapes `data/` and that drafts
   only land under `data/pending_review/` (writes to `data/meetings/` and
   `data/state/` are in-scope per the tool reference and are checked for
   path-traversal safety via `validate_session_id`).
5. Add an automated, repeatable zero-egress pytest module (socket
   monkey-patch guard) covering the runtime commands — the acceptance
   criterion the current `scripts/network_audit.py` (manual, live-process)
   does not satisfy on its own.
6. Any live egress path or token bypass → STOP at [HUMAN DECISION], no
   silent patching.

Risk notes: `cli/mail_sync.py` / `cli/calendar_matcher.py` (Outlook COM)
and `cli/teams_sync.py` are the most likely places for accidental egress;
COM talks to a local Outlook process but Outlook itself may hit the
network — need to verify these are user-triggered only and documented.

### Findings (2026-07-03)

**Stated invariant (docs/architecture.md § Data-egress guarantee):** loopback-only
process binding; telemetry env vars set before subprocess launch; `setup` is the
only network-permitted command, "enforced structurally … and verified by
`scripts/network_audit.py`" (a runtime psutil egress monitor watching a live
process tree — a manual tool, not CI-repeatable).

**A-1. Zero-egress claim CONFIRMED for all runtime commands (no actual egress
path found — [HUMAN DECISION] stop condition not triggered).** Evidence:
- Static sweep of every production import of a network-capable library:
  `httpx` only in `llm/client.py`, `llm/http_probe.py`, `llm/server_manager.py`
  — every construction/request passes `trust_env=False`; all target the
  configured local llama-server base URL.
- `huggingface_hub` imported lazily *inside* `cli/main.py::setup()` only —
  unreachable from any other command's import graph (verified by AST test).
- Raw `socket` use confined to `cli/web.py`, all three sites
  `create_connection(("127.0.0.1", llm_port))` port probes.
- Web dashboard binds `uvicorn.run(..., host="127.0.0.1")` (cli/main.py:672);
  MCP server is stdio-only (no socket at all).
- `cli/git_backup.py` commits locally only — no `git push` anywhere.
- `cli/mail_sync.py` / `cli/teams_sync.py` use Outlook COM (local IPC, not
  network sockets from this process). Caveat documented: Outlook itself is a
  network application; these are explicitly user-triggered and the runbook/
  README already label the Calendar tab as a user-triggered Outlook sync.

**A-2. Automated, repeatable egress test added (acceptance criterion 1):**
`tests/security/test_zero_egress.py` — 8 tests, 3 layers:
  (a) static import gate with justified per-file allowlist (fails on any new
      network-capable import, and on stale allowlist entries);
  (b) AST gate asserting `trust_env=False` on every production httpx call
      (regression guard for the S2 bug class) + loopback-literal gate on
      `cli/web.py` socket probes;
  (c) runtime socket guard (monkey-patched `socket.connect`/`create_connection`
      raising on any non-loopback address) over a full PROPOSED→REVIEWED→APPLIED
      cycle incl. git snapshot commits, transcript-import parsing, and briefing
      build. All pass.
`scripts/network_audit.py` remains the live whole-process check; the two are
complementary and both are now referenced from this log.

**A-3. MCP tool write scope ENUMERATED AND BOUNDED (acceptance criterion 2).**
`mcp_server/server.py` registers exactly 8 tools (matches
docs/mcp-tool-reference.md). Write sites, all via `concurrency.atomic
.atomic_write_text` on paths derived from `validate_session_id`-checked ids
(`^[A-Za-z0-9_\-]{1,128}$` — no `/`, no `.`, no traversal):
  - `extract_action_items` → `data/meetings/<sid>.actions.json`, `.summary.md`,
    `.mom.md` (+ loop-closure/blocker sidecars)
  - `propose_todo_update` → `data/pending_review/<sid>.md`
  - state transitions → `data/state/<sid>.json` (via `state.py` only)
  - `transcribe_meeting` → `data/meetings/<sid>.json` + deletes `tmp/<sid>.wav`
Note: the brief's literal criterion "no tool can write outside
`data/pending_review/`" is narrower than the *documented* contract
(mcp-tool-reference.md declares meetings/ and state/ writes); the verified
property is: writes bounded to `data/{meetings,pending_review,state}/` +
`tmp/` deletion, `data/todo.md` unreachable (INV-5: `apply_reviewed_update`
absent from mcp_server/agent/transcribe import graphs — re-confirmed by grep).
Adversarial extraction output cannot influence paths (paths built from
session_id only; LLM output lands in file *content*).

**A-4. Capability token audit (acceptance criterion 3).**
- Generation: `secrets.token_hex(16)` (128-bit CSPRNG) — but by design the
  nonce is decorative; enforcement is `isinstance(token, CapabilityToken)`,
  i.e. "this type cannot be constructed from untrusted JSON". Sound for the
  stated threat model (agent-loop tool-calling), NOT a cross-process/network
  credential — correctly so, since apply runs in-process.
- Storage: never persisted, never logged, minted per-operation, lifetime =
  one call. No expiry/rotation needed at this design point.
- CLI/web parity: CONFIRMED — both `cli/main.py::apply` and `cli/web.py`'s
  `/api/review/apply` + auto-accept path call the *same* in-process
  `apply_reviewed_update`, which itself enforces the token, session-id
  validation, and the REVIEWED-state fail-fast. No divergent enforcement.
- Divergence found (documentation only): `cli/capability.py` docstring says
  "exactly five places" mint tokens; actual production sites are SIX
  (cli/main.py:456; cli/web.py:983 auto-accept, 1236 review/apply, 1646,
  1679, 1700 task endpoints). File-level invariant (only main.py/web.py)
  HOLDS. → fix queued (doc correction, not security-relevant).

**A-5. Hardening notes (theoretical, not actual egress — reported, not
silently patched):**
  1. `transcribe/whisper_runner.py:97` uses `os.environ.setdefault(
     "HF_HUB_OFFLINE", "1")` — a user with `HF_HUB_OFFLINE=0` exported wins,
     and a missing model cache would then reach the Hub at first `process`.
     Under default config this cannot fire. Recommend: hard-set within the
     transcription call, or document the override.
  2. `transcribe/diarisation.py` relies on whisper_runner having set
     `HF_HUB_OFFLINE` earlier in the same process before
     `Pipeline.from_pretrained` — true today (transcribe→diarise ordering)
     but fragile; recommend setting it locally too.
  3. Whisper/pyannote weights are NOT downloaded by `meeting-agent setup`
     (which fetches only the LLM profile) — first `process` run needs either
     a pre-populated HF cache or will fail offline. Setup-guide already
     recommends the airplane-mode verification, which would catch this, but
     `setup` doing the whisper pre-fetch would make the "only network step"
     claim complete in practice. → recommendation only.

**A-6. Incidental fix:** `tests/llm/test_server_manager.py::
test_build_launch_command_llama_server` failed on this workstation because
the developer's `LLAMA_SERVER_EXE` env var leaked into the test; now isolated
with `monkeypatch.delenv`. (Unambiguous test-hygiene fix, executed.)

**Strand A acceptance criteria:** all three MET. No [HUMAN DECISION] stop
condition reached (no actual egress path, no token bypass).

---

## Strand B — Code-quality audit

### Plan (written before execution)

1. Exclusion list from docs/code_review_2026_07_01.md (see header) — fresh
   findings only.
2. ruff / mypy / bandit across all nine production packages; triage; fix
   unambiguous defects (with regression tests); record the rest as baseline.
3. State-machine review: ALLOWED_TRANSITIONS vs docs/architecture.md graph,
   import-transcript's alternate entry, orphan states.
4. Directory-structure drift vs the canonical tree in docs/architecture.md.

### Findings (2026-07-03)

**Static-analysis baseline (tool versions: ruff 0.15.20, mypy 2.1.0,
bandit latest; all newly added to the dev environment — none were installed
or configured before this audit):**

**B-1 (HIGH, fixed): two real runtime crashes from missing imports.**
`cli/doc_ingest.py::ingest_document` and `cli/mail_sync.py::save_mail_context`
both called `atomic_write_text` without importing it → guaranteed NameError
at the persistence step (ruff F821). Impact: every document upload through
`POST /api/context/doc` 500'd *after* paying the full LLM summarisation cost
(endpoint catches only ValueError/NotImplementedError), and mail-context
persistence failed likewise. Not in the 2026-07-01 review (modules are
post-review v2 additions with zero test coverage). Fixed (imports added);
regression tests added: `tests/cli/test_doc_ingest.py` (4 tests, exercises
the write path end-to-end with a fake LLM), `tests/cli/test_mail_sync.py`
(3 tests, COM-free persistence + tokeniser). This is also the explanation
for why nothing caught it: both modules had no tests at all.

**B-2 (LOW, fixed):** ruff cleanups — unused imports (`resolve_weights_path`
in cli/main.py, `numpy`/`shutil` in cli/web.py, `sys` in scripts/gpu_check.py,
`json`/`dataclasses` in transcribe/whisper_runner.py), unused local
(`tomorrow` in cli/teams_sync.py), `collections` annotation referenced before
its function-local import in cli/web.py (import moved to module level),
import-order E402s in cli/web.py (constant moved below imports).
Remaining baseline: 3 deliberate E402 in transcribe/whisper_runner.py
(`load_cuda_dlls()` must run before heavy imports — correct as-is).

**B-3 (baseline, recorded): mypy.** `--strict` is not currently meaningful
(mypy 2.1.0 INTERNAL ERROR at end of run, plus the codebase predates strict
typing). Default mode: 37 errors = 16 missing-stub noise (pywin32, psutil,
faster_whisper, rank_bm25) + 21 annotation-looseness findings. Spot-checked
the plausible-defect candidates (cli/web.py:361 float-into-inferred-str-dict;
audio_capture lazy-None attributes) — none are runtime defects. Severity: all
LOW. Recommendation: adopt `check-untyped-defs` incrementally before ever
attempting `--strict`.

**B-4 (baseline + 1 fix + 1 recommendation): bandit.**
- Fixed: B324 (HIGH-severity flag, benign use) — `hashlib.sha1` in
  cli/web.py `_calendar_event_id` is a stable-id hash, not a security hash;
  marked `usedforsecurity=False`.
- Recommendation (MEDIUM, deliberate non-fix): B615 — `setup`'s
  `snapshot_download` has no `revision=` pin, so the one network step trusts
  the HF repo head at download time. Pinning a known-good revision per model
  profile in llm/model_profiles.py would close a supply-chain gap. Requires
  choosing the revisions → queued for [HUMAN DECISION] alongside Strand D's
  backend recommendation rather than guessed here.
- Accepted as-is (LOW): B101 asserts in agent/mcp_client.py (internal
  protocol invariants), B110 try/except/pass in best-effort enrichment
  paths (deliberate design), B404/B603 subprocess with fixed argv lists
  (git/serve/process children — no shell, no untrusted argv).

**B-5. State machine — transition table validated, two documentation
divergences and one dead state found:**
- Every edge in `ALLOWED_TRANSITIONS` matches the documented graph;
  `import-transcript` (CLI + web) enters via `create_session(initial_state=
  STOPPED)` then a legal STOPPED→TRANSCRIBED `transition()` — no bypass,
  exactly as architecture.md describes.
- **State `IDLE` is unreachable in practice**: no code path ever creates a
  session at IDLE (recording tool creates at RECORDING; import at STOPPED),
  so the IDLE→RECORDING edge is dead. Harmless, but the docs present IDLE
  as the real start state. Doc-vs-code drift, not a defect.
- **"Any state → FAILED" is not literal**: IDLE and APPLIED have no FAILED
  edge (IDLE moot per above; APPLIED is deliberately terminal/archived).
- **[HUMAN DECISION — flagged] "FAILED (resumable via `meeting-agent
  process`)" is contradicted by the implementation.** FAILED has an empty
  allowed-transition set ("terminal for this session_id; retry uses a fresh
  id", state.py:55), and `process` on a FAILED session raises
  InvalidTransitionError at the final transition. Three docs claim
  resumability (architecture.md state graph, whisper_runner.py docstring,
  mcp-tool-reference error contract pointing at runbook recovery). Either
  (a) the docs are stale and should say "retry under a fresh session id",
  or (b) a FAILED→<retry> edge is intended and missing. Changing
  ALLOWED_TRANSITIONS is a design decision (the prior audit explicitly
  treated it as frozen) → not changed; awaiting owner call.

**B-6. Directory-structure drift (flagged, doc-side):** actual tree has,
undocumented in architecture.md's canonical block: `concurrency/{lock.py,
atomic.py}` (whole package absent from the tree diagram despite being
central to amendment 4), `cli/feedback.py`, `mcp_server/quality_gate.py`,
`llm/http_probe.py`, `tests/security/` (new, this audit). Everything the
diagram *does* list exists. Recommendation: one doc edit adding the five
entries; deferred to the final report's doc-fix batch rather than editing
architecture.md piecemeal mid-audit.

**Strand B acceptance criteria:** baseline recorded (this section) — MET;
state-machine table validated with divergences flagged — MET (one item to
[HUMAN DECISION]); structure reconciled — MET (drift flagged, doc fix
queued).

---

## Strand C — Functional and stress testing

### Plan (written before execution)

1. Parity suite (`tests/parity/`): CLI in-process path vs web endpoints for
   review→apply, double-apply refusal, malformed-todo.md failure — assert
   *identical* semantic outcomes, not merely "both pass their own tests".
2. Fault-injection suite (`tests/faults/`): malformed transcripts in all 4
   import formats, corrupted `data/state/` files (incl. the orphan reaper
   and briefing walking past them), concurrent capability-gated todo.md
   writers, and the web upload endpoint under garbage input.
3. Property suite (`tests/property/`, hypothesis): todo.md round-trip
   losslessness; `_parse_extraction_result` totality (ExtractionError or a
   contract-satisfying dict — nothing else) over arbitrary text; extraction
   items surviving the pending-review draft rendering.
4. Regression coverage confirmed for every Strand A/B fix.

### Findings (2026-07-03)

**C-1 (MEDIUM, fixed): malformed transcript upload was an unhandled 500.**
`POST /api/upload/transcript`'s try-block had only a `finally:` — a
truncated/invalid `.json` transcript raised JSONDecodeError/KeyError
straight through FastAPI. Fixed on two levels: `parse_whisper_json` now
normalises structural garbage (bad JSON, missing keys, non-numeric times)
to its *documented* `ValueError` contract, and the endpoint returns 400
with a parse message. Regression tests: 4 in tests/faults (parser) + 3
endpoint tests.

**C-2 (HIGH, fixed — found by hypothesis on the first property run):
Windows encoding corruption on every non-ASCII character.** All artefact
writers emit UTF-8 (`atomic_write_text(encoding="utf-8")`), but ten
production read sites used locale-default `Path.read_text()` — on this
project's own target platform (Windows, cp1252) any non-ASCII character in
an action item, transcript, summary, or state metadata (a name like "José",
a "±", an em-dash from an LLM summary) round-trips as mojibake or raises
UnicodeDecodeError. Falsifying example: description `'¡'` → parsed back as
`'Â¡'`. Fixed: explicit `encoding="utf-8"` added to all 10 read sites
(todo.py, state.py, review.py×2, briefing.py, loop.py, review_apply.py,
web.py×2, session_buffer.py) and 7 locale-default write sites (web.py×5,
main.py, session_buffer.py). Regression: the hypothesis round-trip test
itself (200 examples/run) now passes; full suite green.

**C-3 (MEDIUM, fixed): one corrupted state file killed the whole briefing.**
`pipeline_status` crashed with JSONDecodeError if any `data/state/*.json`
was truncated/hand-mangled — taking down both `meeting-agent briefing` and
the dashboard's briefing widget. Now skips unreadable files and surfaces
them as a new `unreadable` bucket (rendered as "inspect by hand" in the
text briefing). Regression: `test_briefing_survives_corrupted_state_file`,
`test_reaper_skips_corrupted_state_files_and_reaps_the_rest` (the reaper
already handled this correctly — now pinned by test).

**C-4: parity confirmed (acceptance criterion 1).** 3 tests: identical
todo.md semantic content from CLI and web apply; double-apply refused on
both paths (InvalidTransitionError / 409) with todo.md untouched;
malformed todo.md → both paths fail the session with TODO_FILE_UNPARSEABLE,
neither rewrites the corrupt file (web status pinned at 422).

**C-5: concurrency.** 12 threads racing `write_manual_task` under the
FileLock: zero lost items, zero torn writes, file stays parsable.

**Strand A/B fix coverage check (acceptance criterion 4):** A-6 (env-var
isolation) is itself a test; B-1 → tests/cli/test_doc_ingest.py +
test_mail_sync.py; B-2/B-4 are non-behavioural (ruff/bandit gates would
re-flag); C-1/C-2/C-3 as above. 100% of behavioural fixes have regression
tests.

**Test count: 196 → 228 passing (32 added), 0 failing.** `hypothesis`
added to the dev extra in pyproject.toml.

**Strand C acceptance criteria: all four MET.**

---

## Strand D — Performance benchmarking

### Plan (written before execution)

1. Empirical baselines on the actual workstation (RTX 5090 Laptop 24GB,
   driver 592.02): faster-whisper throughput per model size on real speech
   audio (synthesised offline via Windows SAPI TTS — zero egress), and LLM
   extraction latency through the project's own server_manager +
   HttpLLMClient against the shipped qwen2_5_7b_gguf profile.
2. Static + quantitative analysis of the chunked-extraction path.
3. Written backend recommendation only — no dependency change.

### Findings (2026-07-03)

**D-1. Transcription baseline (8.6 min of 16kHz mono speech, beam_size=2,
vad_filter=True, device=cuda, compute_type=int8_float16 — the shipped
config; reproducible via `scripts/bench_pipeline.py`):**

| model | load | transcribe | speed | 60-min meeting costs |
|---|---|---|---|---|
| base (shipped default) | 6.8s | 11.3s | 45.9x realtime | ~1.3 min |
| medium | 2.7s | 19.8s | 26.1x realtime | ~2.3 min |
| large-v3 | 5.1s | 60.4s | 8.6x realtime | ~7.0 min |

Config drift noted: README presents `large-v3` as the reference model;
`config/settings.toml` ships `model = "base"` ("changed from medium for
faster execution"). Doc fix queued.

**D-2. Extraction latency baseline (llama-server + Qwen2.5-7B GGUF, the
shipped profile; real prompts built from the project's own project-meeting
system prompt):**

| prompt size | latency (warm) |
|---|---|
| ~1.6k est tokens (short meeting) | ~2.9s |
| ~5.1k est tokens (full chunker-sized chunk) | ~3.5s |

Plus one real production datapoint from data/state/: session
news-20260702-145949, TRANSCRIBED→EXTRACTED in 8.2s (single-chunk path
with chaining context). Extraction is NOT the pipeline bottleneck at any
plausible meeting length: a 3-hour seminar ≈ 28k tokens ≈ 7 chunks ≈
~25s of chunk calls + ~3s synthesis. Transcription dominates wall time.

**D-3. Chunking-efficiency finding (documented, not changed):**
1. No redundant *transcript* processing: overlap is 400/5000 tokens ≈ 8%
   extra per chunk after the first, plus exactly one synthesis call over
   the (small) merged summaries — both deliberate and proportionate.
2. The real inefficiency is **context re-prefill**: `_extract_chunked`
   sends the entire `full_system_prompt` (base prompt ~600 tok + todo.md
   context + negative examples + chained prior sessions) with *every*
   chunk, and `cache_prompt: False` (a deliberate SB-1.1 privacy choice in
   llm/client.py) forces llama-server to re-prefill it each time. Cost
   today: ~1–2s × n_chunks on this hardware — real but tolerable.
3. **Bigger finding — context bomb in session chaining (flagged):**
   `_load_prior_sessions` injects up to 3 prior sessions' FULL TRANSCRIPTS
   (`<sid>.md`), not their summaries — architecture.md explicitly says
   "chaining previous meeting summaries". Three 30-min prior meetings ≈
   15k tokens of context, which can exceed the model context by itself and
   silently starve the *current* transcript — the exact silent-truncation
   bug class chunking was built to fix (Persona 2). Recommendation: read
   `<sid>.summary.md` (fall back to truncated transcript) — behavioural
   change to extraction quality, so queued for [HUMAN DECISION], not
   applied.

**D-4. Backend-swap recommendation (for [HUMAN DECISION]):**
The market-survey premise is already banked: this project ALREADY runs
faster-whisper (CTranslate2) — the "~4x faster than stock Whisper.cpp"
class of gain is what D-1's numbers show. A Parakeet/NeMo swap would add a
heavy new dependency tree with weak Windows support for, at best, low
single-digit-x on a step that already runs 8.6–46x realtime *batch,
after the meeting ends* — latency the user never sits in front of.
**Recommendation: do not swap backends.** The two levers that matter:
  1. Model choice: shipped `base` is fast but weak on technical/medical
     vocabulary (code_review Personas 2/7). `distil-large-v3` (not
     currently cached locally; would need a one-time network fetch, i.e. a
     `setup` extension) gives near-large-v3 accuracy at roughly 2x its
     speed and is the best accuracy/speed default for this hardware if
     accuracy complaints recur. Zero code change — it's a config value.
  2. Have `setup` pre-fetch the configured whisper model (closes A-5.3's
     "first `process` run needs a warm HF cache" gap at the same time).

**Strand D acceptance criteria:** baselines recorded — MET;
chunking-efficiency finding documented — MET; backend recommendation
produced, not implemented — MET (awaiting [HUMAN DECISION]).

---

## Strand F — Frontend/UX audit and containerisation

### Plan (written before execution)

1. Sweep every fetch/render path in static/app.js for loading/empty/error
   handling across IS Call Hub, Project Meetings/Seminars, Needs Review,
   Tasks, Calendar, Settings, Import.
2. UI-layer parity: confirm the dashboard drives the same endpoints the
   Strand C parity suite pinned against the CLI.
3. Produce Dockerfile + compose with GPU passthrough, healthchecks, and an
   independently-enforced egress-denying network policy.

### Findings (2026-07-03)

**F-1. Frontend failure-state sweep — materially better than the 2026-07-01
review's snapshot.** All 38 fetch sites audited: review queue, search,
meeting detail, IS hub history, weekly digest, tasks, settings-save, import
and system views all render explicit error/empty states (`showFetchError`
banner or inline red text; empty-state copy present for calendar/tasks/
review lists). Deliberate silent catches are justified where found
(status poll during server restart; best-effort doc-context upload;
optional recurring-blockers card hiding itself).

**F-2 (MEDIUM, fixed): the one systemic gap — a dead backend left the
whole dashboard shimmering forever.** `fetchBriefing` (the 5s poll feeding
Dashboard/Calendar/Tasks) caught failures to console only; on a backend
that is down at page-load, every widget kept its initial loading shimmer
with no error. Now: after 2 consecutive failures the error banner is
raised, still-shimmering widgets get an explicit "Backend unreachable"
state, and both clear on recovery. (JS-only change; no endpoint changes.)

**F-3. Known-open UX items re-confirmed as open, not regressed (excluded
from fresh findings per Strand B rule):** native `alert()`s
(already-recording guard), no Apply confirmation dialog, no bulk
accept/reject, diarisation still settings-only.

**F-4. UI-layer parity:** the dashboard's review/apply flow posts to
`/api/review/decide` + `/api/review/apply` — the exact endpoints the
Strand C parity suite proved equivalent to the CLI path, including
refusal behaviour. No UI-side divergence possible (no third write path
exists in app.js — verified by grep for fetch targets).

**F-5. Containerisation delivered (`deploy/`):** Dockerfile.app (CUDA
runtime, healthcheck on /api/briefing), docker-compose.yml (llama.cpp
CUDA server with /health healthcheck + GPU reservation; app service
publishing 127.0.0.1:8000 only; one-off `setup` profile service that is
the sole egress-capable path and also pre-fetches the Whisper model —
closing A-5.3 in container mode), settings.container.toml, README.md.
**Egress policy independent of the app:** `agent-net` is `internal: true`
(no default route off the bridge) with a documented verification
procedure. `docker compose config` validates clean.
Honest limitations documented: live WASAPI capture stays on the host;
the two-service app↔llm wiring needs a maintainer-approved change
(`start_server`'s loopback guard correctly rejects the `llm` service
hostname today) — single-host mode works with zero code changes.

**Strand F acceptance criteria:** no unhandled loading/empty/error state —
MET (F-1/F-2); container egress policy independently enforced — MET
(internal network + verification steps); healthchecks present for both
LLM and app/transcription containers — MET.
