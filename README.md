# meeting-agent

An offline personal meeting agent: it records a meeting, transcribes it locally,
asks a locally-hosted LLM to draft action items, and lets you review and apply
those drafts to a plain-Markdown `todo.md` — all without any network traffic at
runtime. The only step permitted to touch the network is the explicit, one-off
`setup` command that downloads model weights.

This README is the entry point. For the reasoning behind each constraint (why
offline, why draft-only supervision, why a capability token gates `apply`), see
[`docs/architecture.md`](docs/architecture.md). For first-time installation, see
[`docs/setup-guide.md`](docs/setup-guide.md). For the MCP tool contract consumed
by the agent loop, see [`docs/mcp-tool-reference.md`](docs/mcp-tool-reference.md).
For day-to-day operation and incident response, see
[`docs/runbook.md`](docs/runbook.md).

## Why this exists

Meeting notes and action items routinely contain commercially or personally
sensitive content. This tool is built on the premise that such content should
never leave the machine it was recorded on — not to a transcription API, not to
a hosted LLM, not to telemetry. Every architectural decision in this project is
downstream of that premise; see the data-egress guarantee in
`docs/architecture.md` for the specifics of how that is verified, not merely
configured.

## Core workflow

```
meeting-agent devices                         # find a microphone/loopback device index
meeting-agent serve                            # start the local LLM server (separate terminal)
meeting-agent record --session-id standup-2026-06-30
meeting-agent process --session-id standup-2026-06-30
meeting-agent mcp-serve                        # (launched automatically by agent-run; rarely run by hand)
meeting-agent agent-run --session-id standup-2026-06-30
meeting-agent review --session-id standup-2026-06-30
meeting-agent apply --session-id standup-2026-06-30
meeting-agent briefing                         # run this each morning
```

Each session moves through a fixed state machine — `IDLE → RECORDING → STOPPED
→ TRANSCRIBED → EXTRACTED → PROPOSED → REVIEWED → APPLIED` (or `FAILED` from any
point) — described in full in `docs/architecture.md`. Nothing is written to
`data/todo.md` except via the human-supervised `review`/`apply` pair: the agent
loop (`agent-run`) only ever produces a draft under `data/pending_review/`.

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
each pipeline state it reports.

## Installation

See [`docs/setup-guide.md`](docs/setup-guide.md) for the full procedure,
including the one-time, network-permitted `meeting-agent setup` step and the
recommended airplane-mode verification afterwards.

```
pip install -e .
meeting-agent setup --profile nemotron_nvfp4
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

Milestones M1–M6 (audio capture, transcription, LLM serving, MCP tool server,
agent orchestration, review/apply CLI) are implemented and unit-tested. The
review/apply pipeline has additionally been stress-tested against double-apply
and malformed-`todo.md` scenarios; both are now permanent regression tests
(`tests/cli/test_review_apply.py`). M7 (this documentation pass) is current.
