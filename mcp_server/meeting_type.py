"""
Meeting type enumeration and auto-detection.

Three types are supported; each drives a distinct extraction system prompt
(mcp_server/tools/extraction.py) and MoM template (mcp_server/mom_writer.py).
Priority order for resolving a session's type (architecture_v2.md §4):
  1. Explicit `.type` file (written at recording start / import time, or by an
     explicit UI selection) -- read by `load_meeting_type`.
  2. Slug-prefix detection on session_id -- `detect_meeting_type`.
  3. Default: PROJECT.

Deliberately has zero dependency on mcp_server.state or any I/O beyond reading
the one `.type` file it is given, so it can be imported from cli/, transcribe/,
or mcp_server/ without pulling in the rest of the package.
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path


class MeetingType(str, Enum):
    IS_CALL = "is-call"
    PROJECT = "project-meeting"
    SEMINAR = "seminar"
    GENERAL = "general"


_SLUG_PATTERNS: list[tuple[re.Pattern[str], MeetingType]] = [
    (re.compile(r"^is-call-"), MeetingType.IS_CALL),
    (re.compile(r"^seminar-"), MeetingType.SEMINAR),
    (re.compile(r"^project-"), MeetingType.PROJECT),
]


def detect_meeting_type(session_id: str) -> MeetingType:
    """Return a MeetingType from the session_id slug prefix. Default: GENERAL --
    not every unrecognised session is a project meeting (e.g. an ad-hoc call with
    an external party who is not IS, not a consortium member, not a seminar
    speaker); GENERAL is the lowest-assumption catch-all."""
    for pattern, meeting_type in _SLUG_PATTERNS:
        if pattern.match(session_id):
            return meeting_type
    return MeetingType.GENERAL


def load_meeting_type(type_file_path: Path | str) -> MeetingType:
    """Read a `.type` file; fall back to slug-based detection if absent or unrecognised.

    Args:
        type_file_path: Path to the `<session_id>.type` file (need not exist).

    Returns:
        The resolved MeetingType.
    """
    p = Path(type_file_path)
    if p.exists():
        raw = p.read_text(encoding="utf-8").strip()
        try:
            return MeetingType(raw)
        except ValueError:
            pass
    # Fallback: detect from the filename stem (session_id), stripping any
    # trailing ".type" suffix component if present.
    stem = p.name
    if stem.endswith(".type"):
        stem = stem[: -len(".type")]
    return detect_meeting_type(stem)


def type_file_path(meetings_dir: Path | str, session_id: str) -> Path:
    """Canonical location of a session's `.type` file."""
    return Path(meetings_dir) / f"{session_id}.type"


def write_meeting_type(meetings_dir: Path | str, session_id: str, meeting_type: str) -> None:
    """Write a session's `.type` marker file atomically.

    Previously three separate call sites (cli/main.py, and twice in
    cli/web.py) each did their own plain `.write_text(...)`, duplicating the
    same two-line write and, more importantly, none of them atomic -- a crash
    mid-write leaves a truncated/empty `.type` file, which silently falls
    back to slug-based detection (load_meeting_type above) rather than
    corrupting anything critical, but there's no reason to accept even that
    small a risk when atomic_write_text is one import away and every other
    call site already uses it for this project's other artefact writers.
    """
    from concurrency.atomic import atomic_write_text

    atomic_write_text(type_file_path(meetings_dir, session_id), meeting_type)
