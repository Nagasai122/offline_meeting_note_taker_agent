"""
Read-only daily briefing: aggregates today's open tasks (data/todo.md) and
pipeline status (data/state/) into one local, zero-mutation view, intended to
be run once each morning.

Deliberately does NOT touch any network connector (e.g. a calendar service),
even though one may be available in the wider environment -- "meetings" here
means only what this offline system itself knows about: sessions awaiting
review or apply, and ones that reached a terminal state (APPLIED/FAILED)
today. This is an explicit, user-confirmed decision to keep the verified
zero-egress guarantee intact rather than silently widening it. A live-calendar
variant, if ever wanted, must be a separate, clearly network-labelled command,
analogous to how `setup` is the one network-permitted exception today.

Pure read-only logic -- no FileLock needed (nothing is mutated), no
capability token needed (nothing reaches data/todo.md as a write). Split from
cli/main.py for the same testability reason as cli/review_apply.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from mcp_server import state as state_mod
from mcp_server.todo import TodoItem, parse_todo


@dataclass
class TaskBuckets:
    overdue: list[TodoItem] = field(default_factory=list)
    due_today: list[TodoItem] = field(default_factory=list)
    due_this_week: list[TodoItem] = field(default_factory=list)
    later: list[TodoItem] = field(default_factory=list)
    no_date: list[TodoItem] = field(default_factory=list)
    unparsable_dates: list[TodoItem] = field(default_factory=list)


def _parse_due_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def bucket_open_tasks(todo_path: Path | str, today: date) -> TaskBuckets:
    """Open (not-done) items only -- a completed item is not part of 'what is
    left to do today'.

    A due_date that fails to parse as YYYY-MM-DD is deliberately NOT treated
    as fatal here, unlike todo.py's own TodoFileUnparsableError contract: this
    is a read-only display tool, and one bad date string must not block the
    whole briefing from rendering. It is bucketed separately and flagged
    rather than silently dropped or allowed to raise.
    """
    buckets = TaskBuckets()
    todo = parse_todo(todo_path)
    week_cutoff = today + timedelta(days=6)
    for item in todo.items:
        # "deleted" is a soft-delete status (architecture_v2.md §Phase 7.2's
        # DELETE endpoint) -- the record stays in todo.md for history/audit,
        # but must not appear in "what is left to do" the same way a
        # done=True item doesn't.
        if item.done or item.status == "deleted":
            continue
        if item.due_date is None:
            buckets.no_date.append(item)
            continue
        parsed = _parse_due_date(item.due_date)
        if parsed is None:
            buckets.unparsable_dates.append(item)
        elif parsed < today:
            buckets.overdue.append(item)
        elif parsed == today:
            buckets.due_today.append(item)
        elif parsed <= week_cutoff:
            buckets.due_this_week.append(item)
        else:
            buckets.later.append(item)
    return buckets


def pipeline_status(state_dir: Path | str, today: date) -> dict:
    """Sessions grouped by what they need from the human right now.

    APPLIED and FAILED sessions are only surfaced if their most recent
    history entry falls on `today` -- older terminal sessions are noise once
    they are done; this is a *daily* briefing, not a full session archive
    (that already exists via `list_sessions` over MCP / get_session_status).
    """
    awaiting_review: list[str] = []
    awaiting_apply: list[str] = []
    failed_today: list[str] = []
    applied_today: list[str] = []
    unreadable: list[str] = []
    stalled: list[str] = []

    # STOPPED/TRANSCRIBED/EXTRACTED are normally momentary (the web/CLI
    # pipeline drives straight through them), but a session can now also be
    # deliberately left parked at TRANSCRIBED/EXTRACTED by the LLM-readiness
    # gate (cli/web.py's LlmUnavailableError) instead of being failed -- this
    # is the CLI-only counterpart of the dashboard's Stalled/Resume feature
    # (GET /api/sessions/stalled), so a CLI-only user's morning briefing
    # doesn't go blind to a session an LLM outage left resumable. Unlike
    # that endpoint, this has no way to know whether a concurrent `process`/
    # `agent-run` command is live right now (no in-memory state to check from
    # a separate process) -- a session genuinely mid-CLI-pipeline can
    # transiently appear here too, same as awaiting_review/awaiting_apply
    # are not checked against concurrent activity either.
    _stalled_states = (state_mod.State.STOPPED, state_mod.State.TRANSCRIBED, state_mod.State.EXTRACTED)

    for session_id in state_mod.list_session_ids(state_dir):
        try:
            session = state_mod.load_session_state(state_dir, session_id)
        except (ValueError, KeyError, FileNotFoundError):
            # One corrupted state file (crash mid-write, hand edit) must not
            # take down the whole morning briefing; surface it instead.
            unreadable.append(session_id)
            continue
        if session.state == state_mod.State.PROPOSED:
            awaiting_review.append(session_id)
        elif session.state == state_mod.State.REVIEWED:
            awaiting_apply.append(session_id)
        elif session.state in _stalled_states:
            stalled.append(session_id)
        elif session.state in (state_mod.State.FAILED, state_mod.State.APPLIED):
            if not session.history:
                continue
            last_at = session.history[-1].get("at")
            if not last_at:
                continue
            try:
                last_date = datetime.fromisoformat(last_at).date()
            except ValueError:
                continue
            if last_date != today:
                continue
            target = failed_today if session.state == state_mod.State.FAILED else applied_today
            target.append(session_id)

    return {
        "awaiting_review": sorted(awaiting_review),
        "awaiting_apply": sorted(awaiting_apply),
        "failed_today": sorted(failed_today),
        "applied_today": sorted(applied_today),
        "unreadable": sorted(unreadable),
        "stalled": sorted(stalled),
    }


def build_daily_briefing(
    todo_path: Path | str, state_dir: Path | str, today: date | None = None
) -> dict:
    import json
    today = today or date.today()
    calendar_events = []
    calendar_path = Path(todo_path).parent / "calendar.json"
    if calendar_path.exists():
        try:
            with open(calendar_path, "r", encoding="utf-8") as f:
                calendar_events = json.load(f)
        except Exception:
            pass
            
    notes = []
    meetings_dir = Path(todo_path).parent / "meetings"
    if meetings_dir.exists():
        # `glob()` order is filesystem-dependent, not chronological -- the
        # dashboard slices this list to "top 2" for the at-a-glance widget, so
        # an unsorted list silently showed two arbitrary old meetings instead
        # of the two most recent ones. Sort by the summary file's mtime,
        # newest first, which is robust regardless of session_id naming
        # convention (slugged-and-timestamped or not).
        summary_files = sorted(
            meetings_dir.glob("*.summary.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for summary_file in summary_files:
            try:
                session_id = summary_file.name.replace(".summary.md", "")
                content = summary_file.read_text(encoding="utf-8")
                notes.append({
                    "session_id": session_id,
                    "content": content
                })
            except Exception:
                pass
            
    return {
        "today": today.isoformat(),
        "tasks": bucket_open_tasks(todo_path, today),
        "sessions": pipeline_status(state_dir, today),
        "calendar": calendar_events,
        "notes": notes,
    }


def _fmt_item(item: TodoItem) -> str:
    owner = f"[{item.owner}] " if item.owner else ""
    due = f" (due {item.due_date})" if item.due_date else ""
    return f"  - {owner}{item.description}{due}"


def render_briefing(briefing: dict) -> str:
    lines = [f"=== Daily briefing -- {briefing['today']} ==="]
    
    calendar_events = briefing.get("calendar", [])
    if calendar_events:
        lines.append(f"\nTODAY'S MEETINGS (from local Outlook: {len(calendar_events)})")
        for ev in calendar_events:
            loc = f" ({ev['location']})" if ev.get('location') else ""
            lines.append(f"  - [{ev['start']} - {ev['end']}] {ev['subject']}{loc} (Org: {ev['organizer']})")
    
    tasks: TaskBuckets = briefing["tasks"]

    def _section(title: str, items: list[TodoItem]) -> None:
        if not items:
            return
        lines.append(f"\n{title} ({len(items)})")
        lines.extend(_fmt_item(i) for i in items)

    _section("OVERDUE", tasks.overdue)
    _section("DUE TODAY", tasks.due_today)
    _section("DUE THIS WEEK", tasks.due_this_week)
    _section("NO DUE DATE", tasks.no_date)
    _section("LATER", tasks.later)
    if tasks.unparsable_dates:
        lines.append(f"\nUNPARSABLE DUE DATE ({len(tasks.unparsable_dates)}) -- check these by hand")
        lines.extend(_fmt_item(i) for i in tasks.unparsable_dates)
    if not any([
        tasks.overdue, tasks.due_today, tasks.due_this_week,
        tasks.no_date, tasks.later, tasks.unparsable_dates,
    ]):
        lines.append("\nNo open tasks. data/todo.md is empty or fully done.")

    sessions = briefing["sessions"]
    lines.append("\nPIPELINE STATUS")
    lines.append(f"  Awaiting your review (PROPOSED): {', '.join(sessions['awaiting_review']) or 'none'}")
    lines.append(f"  Awaiting apply (REVIEWED): {', '.join(sessions['awaiting_apply']) or 'none'}")
    lines.append(f"  Failed today -- needs attention: {', '.join(sessions['failed_today']) or 'none'}")
    lines.append(f"  Applied today: {', '.join(sessions['applied_today']) or 'none'}")
    if sessions.get("stalled"):
        lines.append(
            f"  Stalled (STOPPED/TRANSCRIBED/EXTRACTED) -- resume via the "
            f"dashboard or `meeting-agent agent-run`/`process`: {', '.join(sessions['stalled'])}"
        )
    if sessions.get("unreadable"):
        lines.append(
            f"  Unreadable state files -- inspect by hand: {', '.join(sessions['unreadable'])}"
        )

    return "\n".join(lines)
