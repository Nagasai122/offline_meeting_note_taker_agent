"""
Matches a recorded session to a cached Outlook calendar event by time overlap.

Reads `data/calendar.json`, produced by cli/teams_sync.py's
`fetch_outlook_calendar`, whose actual on-disk shape (confirmed against a real
synced cache on this machine) is a JSON array of objects with separate
"date" (YYYY-MM-DD) and "start"/"end" (HH:MM) fields -- not a single ISO
datetime per boundary, unlike the shorthand in architecture_v2.md §10. This
module builds full datetimes from those two fields before computing overlap.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from mcp_server import state as state_mod

logger = logging.getLogger(__name__)


def _event_bounds(event: dict) -> tuple[datetime, datetime] | None:
    date_str = event.get("date")
    start_str = event.get("start")
    end_str = event.get("end")
    if not (date_str and start_str and end_str):
        return None
    try:
        start_dt = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    if end_dt <= start_dt:
        return None
    return start_dt, end_dt


def match_calendar_event(
    session_start: datetime,
    session_end: datetime,
    calendar_cache_path: Path | str,
) -> dict | None:
    """Return the calendar event with the highest time-overlap ratio (>= 0.5), or None.

    Args:
        session_start: Recording start time.
        session_end: Recording stop time.
        calendar_cache_path: Path to `data/calendar.json`.

    Returns:
        The best-matching event dict (as stored in calendar.json), or None if
        the cache is missing/unreadable or no event reaches a 0.5 overlap
        ratio against the longer of the two durations.
    """
    calendar_cache_path = Path(calendar_cache_path)
    if not calendar_cache_path.exists():
        return None

    try:
        events = json.loads(calendar_cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("match_calendar_event: could not read %s: %s", calendar_cache_path, exc)
        return None

    if session_end <= session_start:
        return None
    session_duration = (session_end - session_start).total_seconds()

    best_ratio = 0.0
    best_event: dict | None = None
    for event in events:
        bounds = _event_bounds(event)
        if bounds is None:
            continue
        event_start, event_end = bounds
        overlap = max(0.0, (min(session_end, event_end) - max(session_start, event_start)).total_seconds())
        if overlap <= 0:
            continue
        event_duration = (event_end - event_start).total_seconds()
        ratio = overlap / max(session_duration, event_duration, 1.0)
        if ratio > best_ratio:
            best_ratio = ratio
            best_event = event

    if best_ratio >= 0.5:
        return best_event
    return None


def save_calendar_match(
    session_id: str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
    event: dict,
) -> None:
    """Persist the matched event's identifying fields onto the session's metadata,
    via mcp_server.state.update_metadata() -- never a direct state-file write,
    per the "state writes only through mcp_server.state" invariant."""
    state_mod.update_metadata(
        state_dir, session_id, lock_path, lock_timeout,
        calendar_event_subject=event.get("subject"),
        calendar_event_date=event.get("date"),
        calendar_event_start=event.get("start"),
        calendar_event_organiser=event.get("organizer") or event.get("organiser"),
    )
