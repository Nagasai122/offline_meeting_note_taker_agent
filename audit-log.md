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
