# meeting-agent

An offline personal meeting agent: it records a meeting (or takes an uploaded
transcript), transcribes it locally, asks a locally-hosted LLM to draft
type-aware minutes and action items, and lets you review and apply those
drafts to a plain-Markdown `todo.md` — all without any network traffic at
runtime. The only step permitted to touch the network is the explicit, one-off
`setup` command that downloads model weights.

This README is the entry point. For the reasoning behind each constraint (why
offline, why draft-only supervision, why a capability token gates `apply`), see
[`docs/architecture.md`](docs/architecture.md). For the v2 feature set (meeting
types, chunked extraction, document/mail/calendar context, transcript import,
AI reasoning enhancements, manual tasks, reminders), see
[`docs/architecture_v2.md`](docs/architecture_v2.md) and the diagram at
[`docs/target_architecture_v2.svg`](docs/target_architecture_v2.svg). For
first-time installation, see [`docs/setup-guide.md`](docs/setup-guide.md). For
the MCP tool contract consumed by the agent loop, see
[`docs/mcp-tool-reference.md`](docs/mcp-tool-reference.md). For day-to-day
operation and incident response, see [`docs/runbook.md`](docs/runbook.md).

## Why this exists

Meeting notes and action items routinely contain commercially or personally
sensitive content. This tool is built on the premise that such content should
never leave the machine it was recorded on — not to a transcription API, not to
a hosted LLM, not to telemetry. Every architectural decision in this project is
downstream of that premise; see the data-egress guarantee in
`docs/architecture.md` for the specifics of how that is verified, not merely
configured.

## Day-to-day use: the web dashboard

```
meeting-agent web                              # http://localhost:8000
```

This is the primary interface. It gives you:

- **IS Call Hub** — a one-tap "Start IS Call" button (no title prompt) for
  the highest-frequency daily use case, with yesterday's targets, today's
  progress, recurring-blocker escalation, and chained call history.
- **Project Meetings** / **Seminars** — type-filtered views, each with a
  pre-meeting context modal (drag-and-drop PDF/PPTX/DOCX/TXT, agenda notes,
  Outlook mail-context fetch) before recording starts.
- **Needs Review** — accept/edit/reject each drafted item, then apply to
  `todo.md`; every session's Minutes of Meeting is previewable before you do.
- **Tasks** — AI-extracted and manually-created tasks side by side, with
  status tracking (`todo`/`in_progress`/`done`/`blocked`), filtering, and
  local Windows Toast reminders for due/overdue items.
- **Settings** — per-default Whisper model selection, privacy notes.

The CLI (below) drives the same underlying pipeline and is useful for
scripting, headless use, or importing a transcript recorded elsewhere.

## Core CLI workflow

```
meeting-agent devices                         # find a microphone/loopback device index
meeting-agent serve                            # start the local LLM server (separate terminal)
meeting-agent record --session-id is-call-20260701-090000
meeting-agent process --session-id is-call-20260701-090000 [--whisper-model distil-large-v3]
meeting-agent mcp-serve                        # (launched automatically by agent-run; rarely run by hand)
meeting-agent agent-run --session-id is-call-20260701-090000
meeting-agent review --session-id is-call-20260701-090000
meeting-agent apply --session-id is-call-20260701-090000
meeting-agent briefing                         # run this each morning
```

To skip recording entirely and inject an already-recorded transcript
(`.txt`/`.vtt`/`.srt`/Whisper `.json`) directly at `TRANSCRIBED`:

```
meeting-agent import-transcript --session-id project-review-20260701-100000 \
    --file transcript.vtt --type project-meeting
```

Each session moves through a fixed state machine — `IDLE → RECORDING → STOPPED
→ TRANSCRIBED → EXTRACTED → PROPOSED → REVIEWED → APPLIED` (or `FAILED` from
any point), with `import-transcript` entering directly at `TRANSCRIBED` —
described in full in `docs/architecture.md`. Nothing is written to
`data/todo.md` except via the human-supervised `review`/`apply` pair (or the
equivalent web endpoints, gated the same way): the agent loop (`agent-run`)
only ever produces a draft under `data/pending_review/`.

## Meeting types

Every session is one of three types, auto-detected from the session slug
(`is-call-*`, `seminar-*`, otherwise a project meeting) or set explicitly from
the web UI. Each type gets its own extraction prompt and Minutes-of-Meeting
template — see `docs/architecture_v2.md` §5 for the exact templates.

## Daily use

Run `meeting-agent briefing` each morning. It is read-only (no lock, no
network, no mutation) and prints:

- open `data/todo.md` items, grouped into overdue / due today / due this week
  / later / no due date;
- pipeline status from `data/state/`: sessions awaiting your review
  (`PROPOSED`), awaiting apply (`REVIEWED`), and any that failed or were
  applied today.

"Meetings" in this briefing means pipeline sessions only — by deliberate
design it does not query any calendar service, to preserve the zero-egress
guarantee. See `docs/runbook.md` for the rationale and for what to do with
each pipeline state it reports. (The web dashboard's Calendar tab is a
separate, explicitly user-triggered Outlook sync — see `docs/architecture.md`.)

## Installation

See [`docs/setup-guide.md`](docs/setup-guide.md) for the full procedure,
including the one-time, network-permitted `meeting-agent setup` step and the
recommended airplane-mode verification afterwards.

```
pip install -e .
meeting-agent setup --profile qwen2_5_7b_gguf
```

## Development

```
pip install -e ".[dev]"
pytest -q
```

The test suite is the authoritative behaviour reference where this document
and the code disagree — when in doubt, read the relevant `tests/` module.

## Project layout

See the directory-structure block in `docs/architecture.md` for the canonical
tree; it is not duplicated here to avoid the two going out of sync.

## Status

The core pipeline (audio capture, transcription, LLM serving, MCP tool
server, agent orchestration, review/apply CLI) is implemented and
unit-tested. The v2 upgrade — meeting types, chunked extraction for long
recordings, document/mail/calendar context enrichment, transcript import,
AI reasoning enhancements (loop closure, recurring-blocker escalation,
weekly digest), manual task tracking with local reminders, and a full
dashboard UI redesign — is implemented; see `docs/architecture_v2.md` for
the spec and `docs/code_review_2026_07_01.md` for the resolution status of
issues found along the way. The review/apply pipeline has additionally been
stress-tested against double-apply and malformed-`todo.md` scenarios; both
are permanent regression tests (`tests/cli/test_review_apply.py`).
