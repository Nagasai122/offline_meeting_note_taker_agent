"""
Domain-level input validation shared by every MCP tool, on top of (not instead
of) the type-hint-derived JSON-schema validation FastMCP already performs.

The main thing earning its keep here is `validate_session_id`: session_id
flows directly into filesystem paths throughout this project (tmp/, data/
meetings/, data/state/, data/pending_review/), so an unvalidated session_id
is a path-traversal vector (e.g. "../../etc/passwd") as much as it is a
correctness concern. Centralising the check here means every tool gets it for
free rather than each tool author having to remember to add it.
"""

from __future__ import annotations

import re

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


class SchemaValidationError(ValueError):
    """Raised when a tool argument fails domain-level validation."""


def validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.match(session_id):
        raise SchemaValidationError(
            f"Invalid session_id {session_id!r}: must match {SESSION_ID_PATTERN.pattern} "
            "(letters, digits, '_' and '-' only -- this also rules out path traversal "
            "sequences such as '../', since session_id is used to build filesystem paths)."
        )
