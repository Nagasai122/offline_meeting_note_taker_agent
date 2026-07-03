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
