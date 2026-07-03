# Audit report — meeting-agent, July 2026

**Auditor:** Claude Fable 5 (Claude Code CLI), autonomous audit per the
2026-07 brief. Full working log with per-strand plans and evidence:
[`audit-log.md`](audit-log.md).

**End state: 228 tests passing (196 at start, 1 of which failed), 0
failing. All Strand A–D and F acceptance criteria met. Strand E delivered
as `docs/feature-proposal-2026-07.md`. Five items await [HUMAN DECISION]
sign-off (§4). Nothing was deferred without justification (§5).**

---

## 1. Summary of findings per strand

### Strand A — Security / zero-egress: claim CONFIRMED

- No actual egress path and no capability-token bypass exists — the
  [HUMAN DECISION] stop condition was never triggered.
- The guarantee is now verified by a permanent, CI-runnable suite,
  `tests/security/test_zero_egress.py` (8 tests): a network-import
  allowlist gate, a `trust_env=False` AST gate on all httpx usage, a
  loopback-literal gate on raw sockets, and a runtime socket guard over a
  full review→apply cycle. This closes the gap that the documented
  verifier (`scripts/network_audit.py`) is a manual, live-process tool.
- MCP write scope enumerated and bounded: 8 tools exactly as documented;
  writes confined to `data/{meetings,pending_review,state}/` via
  traversal-proof session ids; `data/todo.md` unreachable from the agent
  (structural absence re-confirmed). Note: the brief's phrasing
  ("nothing outside `data/pending_review/`") is narrower than the
  *documented* tool contract, which declares meetings/state writes; the
  bounded-write property that matters holds.
- Token audit: type-based enforcement (unforgeable from JSON) is sound
  for the stated threat model; CLI and web enforce identically (same
  in-process function). One docstring divergence (5 vs actual 6 mint
  sites) fixed.
- Hardening notes (theoretical only, reported not patched): `setdefault`
  on `HF_HUB_OFFLINE`; diarisation's reliance on call-order for that env
  var; `setup` not pre-fetching Whisper/pyannote weights.

### Strand B — Code quality

- **Two real runtime crashes fixed:** `cli/doc_ingest.py` and
  `cli/mail_sync.py` used `atomic_write_text` without importing it —
  every web document upload 500'd after paying the LLM cost, and mail
  context never persisted. Both modules were entirely untested; now
  covered (7 tests).
- Static baseline recorded (ruff/mypy/bandit were not previously
  installed): ruff now clean except 3 deliberate E402; mypy 37 errors all
  triaged LOW (annotation looseness + missing stubs; `--strict`
  additionally blocked by a mypy 2.1.0 internal error); bandit clean
  after one `usedforsecurity=False` fix, with one MEDIUM recommendation
  (pin `revision=` in setup's `snapshot_download` — supply-chain).
- State machine: table matches the docs except (a) `IDLE` is never
  instantiated (dead start state), (b) "Any state → FAILED" is not
  literal, and (c) **docs claim FAILED is resumable but the
  implementation makes it terminal** — flagged for [HUMAN DECISION].
- Directory-structure drift: `concurrency/`, `cli/feedback.py`,
  `mcp_server/quality_gate.py`, `llm/http_probe.py` missing from
  architecture.md's canonical tree (doc fix queued, §5).

### Strand C — Functional and stress testing

- **Windows encoding bug (HIGH), found by hypothesis on its first run:**
  writers emit UTF-8 but ten read sites used locale-default
  `read_text()` — on the project's own target platform any non-ASCII
  character (names, em-dashes) corrupted on read-back. All 17
  read/write sites normalised to explicit UTF-8.
- Malformed transcript uploads were unhandled 500s → now 400 with the
  parser honouring its documented ValueError contract.
- One corrupted state file crashed the whole briefing → now skipped and
  surfaced as an `unreadable` bucket.
- Suites added: CLI/web parity (3), fault injection (15), hypothesis
  properties (4 properties, 100–300 examples each), plus doc/mail
  regression tests. 100% of behavioural fixes have regression coverage.

### Strand D — Performance (RTX 5090 Laptop, shipped config; reproducible via `scripts/bench_pipeline.py`)

| Measurement | Result |
|---|---|
| faster-whisper base (shipped default) | 45.9x realtime |
| faster-whisper medium | 26.1x realtime |
| faster-whisper large-v3 | 8.6x realtime (60-min meeting ≈ 7 min) |
| LLM extraction, 1.6k-token prompt | ~2.9s |
| LLM extraction, full 5.1k-token chunk | ~3.5s |

- vs the market baseline: the "~4x over stock Whisper.cpp" class of gain
  is already banked — the project already runs faster-whisper
  (CTranslate2). **Recommendation: no backend swap** (details §4.2).
- Chunking: overlap overhead is a proportionate ~8% + one synthesis call;
  the real inefficiencies are (a) full context re-prefilled per chunk
  (`cache_prompt: False` is a deliberate privacy choice), and (b) a
  **context bomb**: session chaining injects up to 3 FULL prior
  transcripts where the architecture doc says *summaries* — can exceed
  the model context alone and silently starve the current transcript
  (§4.3).

### Strand F — Frontend and containerisation

- All 38 dashboard fetch sites audited: error/empty/loading handling is
  comprehensive (substantially hardened since the 2026-07-01 review).
  One systemic gap fixed: a dead backend left every widget shimmering
  forever; the dashboard now raises the error banner and shows explicit
  "Backend unreachable" states, recovering automatically.
- UI parity: the dashboard drives exactly the endpoints the parity suite
  proved equivalent to the CLI; no third write path exists in app.js.
- `deploy/` added: CUDA llama.cpp + app compose with healthchecks on both
  services, GPU reservations, loopback-only publish, and an
  `internal: true` network as an app-independent egress wall, with a
  documented verification procedure. One-off egress-capable `setup`
  profile (also pre-fetches Whisper weights). Honest limitations in
  `deploy/README.md`: host-side audio capture; two-service app↔llm wiring
  needs a maintainer-approved change (§4.5).

### Strand E — Feature proposal (design only)

`docs/feature-proposal-2026-07.md`: five candidates specified with
effort, zero-egress statements (all five compliant), and token-gate
interactions (none weakened). Suggested order: Obsidian export → semantic
search → digest extensions → diarisation UX → Zotero (defer).

## 2. Fixes applied, with regression-test references

| Fix (commit order) | Regression coverage |
|---|---|
| Zero-egress test suite added | `tests/security/test_zero_egress.py` (is the coverage) |
| `LLAMA_SERVER_EXE` leak in test | the fixed test itself |
| Capability docstring 5→6 sites | n/a (doc); INV-2 gate re-verified |
| Missing `atomic_write_text` imports (doc_ingest, mail_sync) | `tests/cli/test_doc_ingest.py`, `tests/cli/test_mail_sync.py` |
| ruff/bandit mechanical cleanups (unused imports, sha1 flag) | ruff/bandit gates re-run clean |
| Malformed upload 500→400 + parser ValueError contract | `tests/faults/` (7 tests) |
| UTF-8 everywhere (17 sites) | `tests/property` round-trip (200 examples/run) |
| Briefing survives corrupt state | `tests/faults/` (2 tests) |
| Dashboard backend-unreachable state | JS-only; manual procedure in audit-log F-2 |

All commits are small and single-purpose (`git log --oneline
155ac97..HEAD`).

## 3. Performance vs market baseline

See table above. Headline: transcription dominates wall time; extraction
is a non-issue on this hardware. Even `large-v3` at 8.6x realtime means a
one-hour meeting is fully transcribed in ~7 minutes *after the meeting
ends* — batch latency the user never sits in front of. The shipped `base`
default trades accuracy for a 5.3x speed-up that this hardware does not
need; that is an accuracy conversation, not a throughput one.

## 4. Outstanding [HUMAN DECISION] items

1. **FAILED-state semantics (B-5):** docs say "resumable via
   `meeting-agent process`"; implementation makes FAILED terminal
   ("retry uses a fresh id") and `process` on a FAILED session raises.
   Decide: fix the three doc sites, or add a retry edge to
   `ALLOWED_TRANSITIONS` (deliberately not touched by this audit).
2. **Transcription backend/model (D-4):** recommendation is *no backend
   swap*; optionally adopt `distil-large-v3` as the accuracy/speed
   default (config value + one-time weight fetch) and have `setup`
   pre-fetch Whisper weights (also closes A-5.3).
3. **Session-chaining context bomb (D-3.3):** switch chaining to
   `.summary.md` (matching the documented design) — behavioural change to
   extraction quality; one-line-ish fix once approved.
4. **Supply-chain pin (B-4):** choose and pin `revision=` values for
   `snapshot_download` in the model profiles.
5. **Container app↔llm wiring (F-5):** approve honouring
   `settings.llm.host` for outbound client connections (listen binds stay
   loopback) to make the two-service compose layout fully live.
6. **Strand E go/no-go** per candidate (proposal doc, priority table
   included).

## 5. Deferred items, with justification

- **mypy strict adoption:** blocked by a mypy 2.1.0 internal error and
  by codebase-wide annotation looseness with zero identified runtime
  defects; recommended path is incremental `check-untyped-defs`. Not a
  fix this audit could make unambiguously.
- **Known-open UX items from the 2026-07-01 review** (native `alert()`s,
  Apply confirmation, bulk accept/reject, diarisation UI toggle):
  pre-existing, documented as open there; product decisions rather than
  defects, and partially superseded by Strand E candidates.
- **architecture.md doc updates** (canonical tree additions, IDLE/FAILED
  wording, README `large-v3` vs shipped `base` default): batched here
  rather than editing the architecture document piecemeal mid-audit —
  and the FAILED wording depends on decision §4.1, so the batch should
  land after it.
- **Live-hardware integration suites** (`test_server_manager_integration`,
  `test_smoke_fake_llm`): excluded from CI runs by long-standing project
  convention (need a live llama-server); the Strand D benchmark exercised
  the real server path successfully on this machine.

*End of report.*
