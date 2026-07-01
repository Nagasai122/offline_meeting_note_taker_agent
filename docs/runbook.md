# Runbook

Day-to-day operation and incident response. Assumes `docs/setup-guide.md` has
already been followed once.

## Daily routine

```
meeting-agent briefing
```

Run this first, every morning. Read-only, no lock, no network. It reports:

- **Overdue / due today / due this week / later / no due date** — open
  `data/todo.md` items, bucketed by `due_date`. A due date that fails to parse
  as `YYYY-MM-DD` is surfaced separately under "unparsable due date," flagged
  rather than dropped or allowed to crash the briefing.
- **Pipeline status** — sessions in `PROPOSED` (run `meeting-agent review`
  next), `REVIEWED` (run `meeting-agent apply` next), and any that reached
  `FAILED` or `APPLIED` **today specifically** — older terminal sessions are
  intentionally not repeated once the day they finished has passed; this is a
  daily briefing, not a session archive. (`get_session_status` over MCP, or
  `meeting-agent`'s own state files under `data/state/`, remain the source of
  truth for older sessions.)

"Meetings" here means pipeline sessions only, not calendar events — see
`docs/architecture.md`'s data-egress guarantee for why a live calendar
connector is deliberately not queried.

## Recovering a session stuck at RECORDING (orphaned by a crash)

If `briefing` or the web dashboard shows a session that has been "Recording
live..." far longer than any real meeting plausibly ran, the owning process
likely crashed or was killed without reaching `STOPPED`. Two layers now
handle this automatically rather than requiring you to delete files by hand:

1. `concurrency/lock.py`'s `FileLock` checks the PID recorded inside
   `data/state/.lock` on contention; if that PID is no longer running, the
   lock is cleared and the acquire retried immediately, instead of waiting
   out the full `lock_timeout_seconds` and raising `LockTimeoutError` against
   a lock nobody actually still holds.
2. The session record itself is not cleared by the lock fix alone -- run:

   ```
   meeting-agent reap-stale
   ```

   This finds every `RECORDING` session whose `pid` metadata points at a
   dead process and transitions it to `FAILED` (reason `ORPHANED_RECORDING`)
   via the normal, validated state transition -- not a hand-edited JSON
   file. The partial WAV under `tmp/`, if any, is left in place; recover
   audio from it manually if needed, then start a fresh session.

   Deliberately not run automatically inside `briefing` (which is documented
   as read-only, no lock, no side effects) or on a timer -- it is a explicit,
   one-shot remediation step you run when you notice the symptom, consistent
   with this project's preference for explicit over implicit state changes.

## Recovering a session stuck at FAILED

1. `meeting-agent briefing` (or `get_session_status` over MCP) tells you which
   session and, in `metadata["error"]`, why.
2. Common causes and fixes:
   - `TODO_FILE_UNPARSEABLE` — `data/todo.md` has a malformed checklist line
     or broken `meta` JSON comment. Fix the line by hand (the error message
     names the line number), then re-run whichever command failed
     (`propose_todo_update` via `agent-run`, or `apply`).
   - An LLM extraction error (`ExtractionError`) — the model did not return a
     valid JSON action-item array. Check the local LLM server is actually
     running (`meeting-agent serve`) and reachable at the configured
     `host:port`; if it is, this is usually a model/prompt issue, not an
     infrastructure one.
   - A transcription failure — check `meeting-agent serve`'s logs are
     irrelevant here (whisper runs in-process, not via the LLM server); check
     GPU/VRAM availability and `[whisper]` settings instead.
3. There is currently no automatic retry — by design, per the MCP tool
   reference's error contract. Re-run the specific command for that step once
   the underlying cause is fixed.

## tmp-audio TTL sweep and the long-lived dashboard process

`[privacy].tmp_audio_ttl_seconds` (default 3600) is enforced by
`audio_capture/session_buffer.py`'s `sweep_orphaned_audio`, which is invoked
from two places:

1. `cli/main.py`'s `_startup` Typer callback — fires once per CLI
   subprocess invocation (every `record`, `process`, `briefing`, etc.).
2. `cli/web.py`'s dashboard lifespan — fires every 10 minutes for as long as
   `meeting-agent web` keeps running, independently of whether any
   recording/processing subprocess happens to start in the meantime.

(2) exists because `meeting-agent web` is itself a single long-lived process;
relying on (1) alone meant a crashed recording's WAV in `tmp/` would only get
swept the next time a fresh CLI subprocess launched -- which could be hours
or days later if the user just leaves the dashboard open between meetings.
This is how a 140MB orphaned WAV was found to have accumulated well past its
1-hour grace period during a usage audit. Both sweeps share the same
`sweep_orphaned_audio` function and the same `tmp_audio_ttl_seconds` setting,
so there is one place to tune the grace period, not two.

The sweep only ever deletes files older than the configured TTL, so a WAV
still being actively written by an in-progress recording (its mtime keeps
advancing) is never at risk, regardless of how frequently the sweep runs.

## Recovering from a double-apply attempt

Re-running `meeting-agent apply` against an already-`APPLIED` session now
fails fast and cleanly:

```
Cannot apply session 'demo-1': current state is APPLIED, expected REVIEWED. No changes were made.
```

This was a real bug found via manual stress testing (see
`tests/cli/test_review_apply.py`'s
`test_apply_on_an_already_applied_session_is_rejected_with_no_side_effects`):
earlier, the invalid state was only discovered at the final internal state
write, by which point `data/todo.md` had already been rewritten and
committed. It is now checked before any I/O. If you see this message, the
session is already done — there is nothing to fix.

## Recovering from a `PARTIAL_APPLY_CONFLICT`

`meeting-agent apply` prints each conflicting item's existing and incoming
versions and exits non-zero, but still applies every non-conflicting item in
the same run. Reconcile manually:

1. Open `data/todo.md` and find the existing item with the printed `id`.
2. Decide whether to keep it, replace its description and edit it by hand, or
   note both manually and discard the incoming one.
3. No second CLI step is provided for this on purpose — duplicate-content
   detection (as opposed to duplicate-id detection) is a precision/recall
   trade-off the project has deliberately not taken on; see
   `cli/review_apply.py`'s docstring.

## Undoing an apply

`data/` is a git repository. `meeting-agent apply` commits before and after
every apply (and, as of the stress-testing pass, also after the trailing
state-file write — `git status --porcelain` is guaranteed clean on return).
To undo:

```
cd data
git log --oneline           # find the commit(s) for the apply you want to undo
git revert <commit>          # or: git reset --hard <earlier-commit>, if you accept losing later history
```

This undo path is independent of the CLI — it works even if the tool itself
has a bug.

## Verifying the offline guarantee still holds

After any dependency upgrade (especially `mcp`, `httpx`, `faster-whisper`, or
a change of `[llm].backend`), re-run `scripts/network_audit.py` across a full
`record → process → review → apply` cycle, and re-check the
`[privacy].disable_telemetry_env` flag names in `config/settings.toml` against
the installed library versions — `VLLM_NO_USAGE_STATS` in particular is
explicitly flagged in `docs/architecture.md` as "last verified," not eternal.

## Known limitations (not bugs)

- `briefing`'s recency window for `FAILED`/`APPLIED` sessions is a hard
  calendar-day boundary, not a rolling N-hour window — a session that
  finishes at 23:59 drops out of the briefing one minute later. Flagged as a
  deferred enhancement, not fixed pre-emptively.
- `briefing` always exits `0`, even when overdue items or today's failures
  exist — it is not yet cron/script-friendly in that sense.
- Diarisation is best-effort and requires a separate, one-time Hugging Face
  authentication step of its own, outside `meeting-agent setup`.
