# MCP tool reference

`mcp_server/server.py` registers exactly eight tools over stdio (never
SSE/HTTP — stdio opens no socket at all, the strongest available form of the
data-egress guarantee for this component). This is the complete, current set;
if `mcp_server/server.py` ever lists more or fewer tools than documented here,
treat the code as authoritative and this file as stale.

`apply_reviewed_update` is deliberately **not** in this list. It is the only
function permitted to write `data/todo.md`, and per critique amendment 2 it is
implemented as a CLI-only command (`meeting-agent apply`), gated by a local
capability token minted by the CLI at process start, and is structurally
absent from `mcp_server/` — not merely registered-then-refused. The agent
loop has no code path that can reach it.

| Tool | State transition | Notes |
|---|---|---|
| `start_meeting` | `IDLE → RECORDING` | Holds the live `SessionBuffer` in server-process memory, keyed by `session_id`. If the server process dies mid-recording, the partial WAV becomes a crash-orphan, cleaned up by the startup sweep (see runbook). |
| `stop_meeting` | `RECORDING → STOPPED` | Audio remains in `tmp/`. |
| `transcribe_meeting` | `STOPPED → TRANSCRIBED` (or `→ FAILED`) | Deletes the source WAV unconditionally on return, success or failure — see `docs/architecture.md`'s honesty caveat on what "deleted" does and does not guarantee. |
| `extract_action_items` | `TRANSCRIBED → EXTRACTED` (or `→ FAILED`) | The one LLM-backed step. Writes `data/meetings/<session_id>.actions.json`. Raises `ExtractionError` if the model's response is not a valid JSON array of `{description, owner, due_date}` objects — a markdown code fence around the JSON is tolerated and stripped, nothing else is. |
| `propose_todo_update` | `EXTRACTED → PROPOSED` | Writes a draft under `data/pending_review/<session_id>.md`, in the same checklist format `todo.md` itself uses. Never touches `data/todo.md`. Re-validates that `data/todo.md` is currently parsable before writing the draft (`TODO_FILE_UNPARSEABLE → FAILED` if not) — a fail-fast check, not a guarantee that still holds by the time `apply` runs later. |
| `get_session_status` | none (read-only) | Returns `{session_id, state, history, metadata}` for one session. `history` entries carry an ISO-8601 `at` timestamp each. |
| `list_sessions` | none (read-only) | Returns `[{session_id, state}, ...]` for every known session, optionally filtered by `state_filter`. Deliberately thin — no timestamps — by design; `cli/briefing.py` calls `state_mod.list_session_ids` + `load_session_state` directly instead, for exactly this reason. |
| `get_transcript` | none (read-only) | Returns the structured transcript JSON for a transcribed session. Raises `FileNotFoundError` if the session has not reached `TRANSCRIBED`. |

## Argument and return shapes

All eight tools take a `session_id: str` first argument (validated by
`mcp_server.schemas.validate_session_id`) except `list_sessions`, which is
unfiltered by default. Full signatures live in `mcp_server/server.py`; the
table above is a summary, not a substitute for reading them — in particular,
`start_meeting`'s `source` argument is the string value of `SourceKind`
(`"microphone"` or `"loopback"`), and `transcribe_meeting`'s `diarisation`
argument, if omitted, falls back to `settings.whisper.diarisation_enabled`
rather than a hard-coded default.

## Error contract

Every tool that can fail transitions the session to `FAILED` with a
structured `metadata["error"]` string before re-raising, rather than leaving
the session state inconsistent with what actually happened on disk. The agent
loop (M5) is expected to treat a `FAILED` transition as a terminal outcome for
that run, not retry automatically — see `agent/loop.py`'s
`MaxIterationsExceededError` handling and `docs/runbook.md`'s recovery
guidance for what a human should do next.

## Why eight tools and not more

The agent loop only ever needs to drive a session forward one step at a time
and check status; it has no business reason to see `apply_reviewed_update`,
and giving it more read access than `get_session_status` / `list_sessions` /
`get_transcript` was considered and rejected as unnecessary surface area for
no corresponding capability gain.
