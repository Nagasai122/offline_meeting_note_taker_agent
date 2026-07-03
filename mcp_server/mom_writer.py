"""
Formats a session's extracted data into a type-aware Minutes-of-Meeting (MoM)
markdown file, per architecture_v2.md §5.

Each `write_*_mom` function is tolerant of missing supplementary keys in
`extracted_data` (the LLM response only ever *guarantees* "summary" and
"action_items" -- see mcp_server/tools/extraction.py's module docstring for
why the per-type keys are additive, not required) so a partially-populated
or older-shaped extraction result still produces a readable MoM rather than
raising.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from concurrency.atomic import atomic_write_text
from mcp_server.meeting_type import MeetingType


def _bullets(items: list[str] | None, empty_text: str = "None recorded.") -> str:
    items = items or []
    if not items:
        return empty_text
    return "\n".join(f"- {item}" for item in items)


def _safe_cell(value: object) -> str:
    """Escape content for a Markdown table cell: a literal `|` would otherwise
    be read as a column separator (silently corrupting the table's column
    count), and an embedded newline would break the row onto multiple lines."""
    if value is None or value == "":
        return "—"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", "")


def _action_items_table(action_items: list[dict] | None, columns: list[tuple[str, str]]) -> str:
    """Render action_items as a markdown table.

    Args:
        action_items: list of action-item dicts (description/owner/due_date/priority/...).
        columns: ordered list of (header, dict_key) pairs, "description" is always first.
    """
    action_items = action_items or []
    header = "| # | " + " | ".join(_safe_cell(h) for h, _ in columns) + " |"
    sep = "|---|" + "|".join("---" for _ in columns) + "|"
    if not action_items:
        return f"{header}\n{sep}\n| - | " + " | ".join("None" for _ in columns) + " |"
    rows = [header, sep]
    for i, item in enumerate(action_items, start=1):
        cells = [_safe_cell(item.get(key)) for _, key in columns]
        rows.append(f"| {i} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


def write_is_call_mom(session_id: str, extracted_data: dict, output_path: Path | str) -> None:
    date_str = extracted_data.get("recording_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    duration = extracted_data.get("duration") or "unknown"

    content = f"""## Daily Progress Log — {date_str}

**Session:** {session_id}
**Duration:** {duration}
**Call type:** IS Progress Review

### Progress Reported
{_bullets(extracted_data.get("progress_reported"))}

### New Targets & Instructions
{_bullets([f"{t.get('task')} (due {t.get('due_date') or 'unspecified'})" for t in (extracted_data.get("new_targets") or []) if isinstance(t, dict)] or None)}

### Blockers & Issues Raised
{_bullets(extracted_data.get("blockers"))}

### Action Items
{_action_items_table(extracted_data.get("action_items"), [("Task", "description"), ("Owner", "owner"), ("Target Date", "due_date")])}

### Continuation Context (for next IS call)
{extracted_data.get("continuation_summary") or "No continuation summary provided."}
"""
    atomic_write_text(output_path, content)


def write_project_mom(session_id: str, extracted_data: dict, output_path: Path | str) -> None:
    date_str = extracted_data.get("recording_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    content = f"""## Minutes of Meeting

**Project / Work Package:** {extracted_data.get("project") or "Not specified"}
**Date:** {date_str}
**Attendees:** {", ".join(extracted_data.get("attendees") or []) or "Not identified"}

### Agenda
{_bullets(extracted_data.get("agenda_items"))}

### Discussion Summary
{extracted_data.get("summary") or "No summary available."}

### Decisions Made
{_bullets(extracted_data.get("decisions"))}

### Action Items
{_action_items_table(extracted_data.get("action_items"), [("Task", "description"), ("Assigned to", "owner"), ("Deadline", "due_date"), ("Priority", "priority")])}

### Next Meeting
{extracted_data.get("next_meeting") or "TBD"}

### Documents Referenced
{_bullets(extracted_data.get("documents_referenced"))}
"""
    atomic_write_text(output_path, content)


def write_seminar_mom(session_id: str, extracted_data: dict, output_path: Path | str) -> None:
    date_str = extracted_data.get("recording_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    content = f"""## Seminar Notes — {extracted_data.get("topic") or session_id}

**Date:** {date_str}
**Speaker(s):** {extracted_data.get("speaker") or "Not identified"}
**Topic / Title:** {extracted_data.get("topic") or "Not specified"}

### Abstract / Overview
{extracted_data.get("summary") or "No summary available."}

### Key Concepts Introduced
{_bullets(extracted_data.get("key_concepts"))}

### Notable Insights & Quotes
{_bullets(extracted_data.get("notable_insights"))}

### Open Questions Raised
{_bullets(extracted_data.get("open_questions"))}

### Follow-up Reading / References
{_bullets(extracted_data.get("references"))}

### Action Items (if any)
{_action_items_table(extracted_data.get("action_items"), [("Task", "description"), ("Owner", "owner"), ("Due", "due_date")]) if extracted_data.get("action_items") else "None — seminars typically have no assigned action items."}
"""
    atomic_write_text(output_path, content)


def write_general_mom(session_id: str, extracted_data: dict, output_path: Path | str) -> None:
    date_str = extracted_data.get("recording_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    content = f"""## Meeting Notes — {date_str}

**Session:** {session_id}
**Duration:** {extracted_data.get("duration") or "unknown"}
**Participants:** {", ".join(extracted_data.get("participants") or []) or "Not recorded"}

### Summary
{extracted_data.get("summary") or "No summary available."}

### Key Points
{_bullets(extracted_data.get("key_points"))}

### Decisions
{_bullets(extracted_data.get("decisions"), empty_text="None recorded.")}

### Action Items
{_action_items_table(extracted_data.get("action_items"), [("Task", "description"), ("Assigned to", "owner"), ("Due Date", "due_date"), ("Priority", "priority")])}
"""
    atomic_write_text(output_path, content)


_WRITERS = {
    MeetingType.IS_CALL: write_is_call_mom,
    MeetingType.PROJECT: write_project_mom,
    MeetingType.SEMINAR: write_seminar_mom,
    MeetingType.GENERAL: write_general_mom,
}


def write_mom(
    session_id: str,
    extracted_data: dict,
    meeting_type: MeetingType,
    meetings_dir: Path | str,
) -> Path:
    """Dispatch to the correct MoM writer for `meeting_type` and return the written path.

    Args:
        session_id: The session this MoM belongs to.
        extracted_data: The parsed extraction result (summary/action_items plus
            any type-specific supplementary keys).
        meeting_type: Which template to use.
        meetings_dir: Directory the `.mom.md` file is written into.

    Returns:
        Path to the written `.mom.md` file.
    """
    output_path = Path(meetings_dir) / f"{session_id}.mom.md"
    writer = _WRITERS[meeting_type]
    writer(session_id, extracted_data, output_path)
    return output_path
