# Architecture — Offline Personal Meeting Agent

This document is the merged, approved plan (Phase 4) including all amendments from the
plan → critique → synthesis cycle, plus the explicit data-egress guarantee added on
final approval. It is the reference for "why" decisions were made, so implementation
does not relitigate them later. It remains the source of truth for the invariants
below (data-egress guarantee, capability token, human-in-the-loop gate) — none of
which changed in the v2 upgrade.

**v2 status:** the meeting-type system, chunked extraction, document/mail/calendar
context enrichment, transcript import, AI reasoning enhancements (loop closure,
recurring-blocker escalation, weekly digest), manual task tracking with local
reminders, and a full dashboard redesign are implemented. See
[`architecture_v2.md`](architecture_v2.md) for that spec and
[`target_architecture_v2.svg`](target_architecture_v2.svg) for the accompanying
diagram; see [`claude_cli_implement_v2.md`](claude_cli_implement_v2.md) for the
implementation log and [`code_review_2026_07_01.md`](code_review_2026_07_01.md) for
the resolution status of issues found along the way.

## Scope and non-negotiable constraints

- Fully offline at runtime. Network access is permitted only during an explicit,
  user-initiated `setup` step (model weight download). Once weights are cached locally,
  the system must run correctly with the machine's network adapter disabled.
- Windows, NVIDIA Blackwell GPU, 12–16GB VRAM. No cloud fallback, no remote inference.
- Draft-only supervision: nothing is written to `data/todo.md` or `data/projects/*.md`
  except via a human-initiated CLI apply step.
- Audio never persists past the transcription step (see honesty caveat below).

## Data-egress guarantee (added at final approval)

The user's explicit requirement: **internal data must not leak outside this machine.**
This is treated as a verified property, not a configuration promise:

1. **Process binding.** `llama-server`/vLLM and the MCP server bind to `127.0.0.1` only,
   never `0.0.0.0`. No component opens a listening socket reachable from outside the
   loopback interface.
2. **Telemetry disabled explicitly.** Environment variables set before any subprocess
   launch: `HF_HUB_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `DO_NOT_TRACK=1`, and (if
   vLLM is the chosen backend) `VLLM_NO_USAGE_STATS=1` — this last one should be
   re-verified against whatever vLLM version is actually installed, since usage-stats
   opt-out flag names have changed across versions; treat the setup guide's stated flag
   as "last verified," not eternal truth.
3. **Setup vs runtime separation.** Only `meeting-agent setup` (downloads model weights)
   is permitted to touch the network. `meeting-agent record`, `process`, `review`, and
   `apply` make zero outbound connections. This is enforced structurally — runtime code
   paths do not import any HTTP client pointed at a non-loopback host — and *verified*
   by `scripts/network_audit.py`, which inspects the live process tree's sockets
   (via `psutil`) during a full record→process→review→apply cycle and fails loudly if
   anything other than loopback traffic is observed.
4. **Recommended user-side verification.** The setup guide recommends running one full
   cycle in airplane mode (network adapter disabled) after initial setup, as the
   strongest practical proof that runtime has no external dependency.

### Honesty caveat on audio deletion

"Audio never persists" means: removed from the active filesystem immediately after
transcription, or on next startup if a crash orphaned it (see Risk table). It is **not**
a forensic-erasure guarantee — SSD wear-levelling/TRIM means a single-pass delete is not
literally unrecoverable at the hardware level. This is stated plainly rather than
oversold. Residual exposure (OS swap, crash dumps) is acknowledged as low-likelihood and
not actively engineered around, which is a proportionate choice for a single-user
personal tool.

## Directory structure

```
meeting-agent/
├── README.md
├── pyproject.toml
├── config/{settings.toml, loader.py}
├── concurrency/{lock.py, atomic.py}   # FileLock (amendment 4) + atomic writes
├── audio_capture/{sources.py, session_buffer.py, device_probe.py}
├── transcribe/
│   ├── whisper_runner.py, diarisation.py, postprocess.py
│   ├── chunker.py            # v2: long-transcript chunked extraction
│   └── import_parsers.py     # v2: VTT/SRT/Whisper-JSON/TXT transcript import
├── llm/{server_manager.py, model_profiles.py, client.py, http_probe.py}
├── mcp_server/
│   ├── server.py, state.py, schemas.py, todo.py
│   ├── quality_gate.py       # extraction-quality scoring for the review UI
│   ├── meeting_type.py       # v2: MeetingType enum + slug/.type detection
│   ├── mom_writer.py         # v2: type-aware Minutes of Meeting templates
│   └── tools/
│       ├── recording.py, transcription.py, extraction.py, review.py
│       ├── loop_closure.py       # v2: IS-call prior-target reasoning
│       └── blocker_escalation.py # v2: recurring-blocker detection
├── agent/{loop.py, mcp_client.py, protocol.py, trace.py, prompts/}
├── cli/
│   ├── main.py, web.py, capability.py, review_apply.py, briefing.py
│   ├── search.py, git_backup.py, teams_sync.py, feedback.py, mail_import.py
│   ├── doc_ingest.py         # v2: PDF/PPTX/DOCX -> summarised context
│   ├── mail_sync.py          # v2: Outlook mail-body context matching
│   ├── calendar_matcher.py   # v2: session <-> calendar event matching
│   ├── weekly_summary.py     # v2: cross-meeting weekly digest
│   └── reminders.py          # v2: local Windows Toast due-task reminders
├── static/{index.html, index.css, app.js}   # web dashboard
├── data/{meetings/, todo.md, pending_review/, state/, calendar.json}
├── tmp/                # transient audio only, swept on every CLI invocation
├── scripts/{network_audit.py, gpu_check.py}
├── tests/              # incl. tests/security/ -- CI zero-egress gates
└── docs/
```

## State machine

```
IDLE -> (start_meeting) -> RECORDING
RECORDING -> (stop_meeting) -> STOPPED            [audio still in tmp/]
STOPPED -> (transcribe_meeting) -> TRANSCRIBED    [audio deleted, unconditionally]
                ^
    (v2) `import-transcript` / POST /api/upload/transcript creates the session
    directly at STOPPED (no audio, nothing to delete) then transitions it to
    TRANSCRIBED the same way -- it does not bypass transition(), it just never
    passes through RECORDING.
TRANSCRIBED -> (extract_action_items) -> EXTRACTED
EXTRACTED -> (propose_todo_update) -> PROPOSED
PROPOSED -> (human review, outside agent loop) -> REVIEWED
REVIEWED -> (apply_reviewed_update) -> APPLIED    [terminal, archived]
Any non-terminal state -> FAILED. FAILED is terminal for that session_id
(retry under a fresh session id -- see mcp_server/state.py); APPLIED is
likewise terminal/archived. `meeting-agent process` can re-drive a session
that stopped *before* FAILED (e.g. re-run extraction on a TRANSCRIBED
session), but it does not resurrect a FAILED one.
```

## Amendments from critique (binding)

1. Startup sweep deletes any `tmp/*.wav` regardless of session outcome — closes the
   crash-orphan gap.
2. `apply_reviewed_update` enforced two ways: (a) a local-only capability token minted
   by the CLI at process start, never exposed to the agent loop's tool-calling context,
   and (b) the tool is structurally absent from the agent loop's toolset, not merely
   refused at runtime.
3. `PARTIAL_APPLY_CONFLICT` aborts only the conflicting item; the rest of the apply
   proceeds; both versions are shown for manual reconciliation.
4. File lock (`data/state/.lock`) held during any write to `todo.md`/`projects/*.md`;
   session-exclusivity check extended to apply operations.
5. `data/` is a git repository; `apply_reviewed_update` commits before and after each
   apply, giving a free `git revert` undo path.
6. M4 includes a fake-LLM end-to-end smoke test of the tool-call schema round-trip,
   pulling integration risk forward from M5.
7. M3's VRAM baseline is measured against a worst-case ~90-minute transcript fixture,
   not just idle/short-prompt load.
8. M4 includes a deliberately malformed/hand-edited `todo.md` test case, asserting
   `TODO_FILE_UNPARSEABLE` rather than silent corruption.

## Daily briefing (M7-adjacent addendum)

`meeting-agent briefing` (read-only; `cli/briefing.py`) was added after M6 to
give a single morning entry point onto open tasks and pipeline status. Its
one architecturally-relevant decision, made explicitly rather than assumed:
"meetings" in this briefing are sourced **exclusively** from local pipeline
state (`data/state/`) — never from any calendar connector that may be present
in the wider environment — to preserve the data-egress guarantee above
without exception or special-casing. A live-calendar variant, if ever wanted,
must be a separate, clearly network-labelled command, analogous to how
`setup` is the one carved-out network-permitted exception today; it must not
be folded into `briefing` itself.

## Risk register

See prior planning discussion for the full table; the two risks with the most direct
bearing on data-egress are NVFP4/driver immaturity (mitigated by a parallel-tested
GGUF fallback profile) and the audio-deletion-on-failure path (mitigated by the
unconditional `finally`-block delete plus the startup sweep above).

## Recent Enhancements (Post-V1)

1. **Dual-Track Audio Recording:** `meeting-agent record` now spawns two processes to capture both the `microphone` and `loopback` (speaker) streams concurrently. `cli/main.py process` merges the outputs (using `transcribe_dual_track`), tags speaker segments automatically ("You" vs "Others"), interleaves them by timestamp, and formally advances the session state to `TRANSCRIBED`.
2. **Smart Context Extraction:** The AI agent no longer relies on hardcoded rules for assigning tasks or inferring meeting context. During the `extract_action_items` phase, it dynamically builds the LLM prompt by:
   - Injecting the active `todo.md` file, allowing it to infer ownership based on past actions.
   - Searching historical sessions (`data/meetings/`) for the last 3 meetings with the same slug across *any* day, chaining previous meeting summaries seamlessly.

## v2: Meeting types, context enrichment, AI reasoning (superseding the ad-hoc "Pinned: IS Sync" button above)

The earlier "Pinned: IS Sync" quick-action generated a fixed `is-sync` slug that
did not match the `is-call-*` chaining convention `extract_action_items`
actually looks for — a real bug (found in `code_review_2026_07_01.md`,
Persona 1). It has been replaced entirely by the **IS Call Hub**: a one-tap
"Start IS Call" button that generates a proper `is-call-{timestamp}` slug
server-side (`POST /api/record/start` with `meeting_type: "is-call"`), so
chaining and loop-closure reasoning always work without relying on a title
string matching by coincidence.

Full v2 spec: [`architecture_v2.md`](architecture_v2.md). Summary of what
landed:

- **Meeting types** (`mcp_server/meeting_type.py`): every session is
  `is-call` / `project-meeting` / `seminar`, resolved from an explicit UI
  selection, a `.type` file, or slug-prefix detection (in that priority
  order). Each type gets its own extraction prompt and Minutes-of-Meeting
  template (`mcp_server/mom_writer.py`).
- **Chunked extraction** (`transcribe/chunker.py`): transcripts over ~5000
  estimated tokens (~35 min of speech) are split into overlapping chunks,
  extracted sequentially against the single local LLM instance, merged
  (deduplicating action items by fuzzy description match), and passed
  through one synthesis pass — closing the silent-truncation gap on long
  meetings identified in the code review (Persona 2).
- **Context enrichment**: `cli/doc_ingest.py` (PDF/PPTX/DOCX/TXT → LLM
  summary, ≤1000 tokens), `cli/mail_sync.py` (Outlook COM mail-body
  matching), `cli/calendar_matcher.py` (session ↔ calendar event matching by
  time overlap) — all best-effort, never fail the pipeline.
- **Transcript import** (`transcribe/import_parsers.py`,
  `meeting-agent import-transcript`, `POST /api/upload/transcript`): inject
  an externally-produced transcript (VTT/SRT/Whisper-JSON/TXT) directly at
  `TRANSCRIBED`.
- **AI reasoning** (`mcp_server/tools/loop_closure.py`,
  `mcp_server/tools/blocker_escalation.py`, `cli/weekly_summary.py`):
  IS-call loop closure (did the prior session's targets get addressed?),
  recurring-blocker detection across the last 7 IS calls, and a
  user-initiated weekly cross-meeting digest.
- **Manual tasks and reminders** (`cli/reminders.py`, extended `TodoItem` in
  `mcp_server/todo.py` with `priority`/`status`/`source`/`progress_note`/`tag`):
  user-created tasks alongside AI-extracted ones, status tracking, and local
  Windows Toast notifications for due/overdue items.
- **Dashboard redesign** (`static/index.html`, `static/index.css`,
  `static/app.js`): IS Call Hub, pre-meeting context modal, highlight-with-note,
  MoM preview, Settings panel, manual-task UI — currently the "Field Notebook"
  visual theme (see `static/index.css`'s header comment for the design-token
  system).
