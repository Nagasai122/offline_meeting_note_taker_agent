"""
Best-effort Outlook mail-body context fetcher (Windows COM, local IPC -- not
network -- consistent with cli/teams_sync.py's calendar sync). Separate from
calendar concerns: this matches a mail item to a session by subject-token
overlap within a time window, not by a calendar event's own metadata.

Privacy constraint (architecture_v2.md §9.2): the fetched body is stored only
in `data/meetings/<session_id>.mail_context.txt`, subject to the same TTL/
cleanup rules as other session artefacts, and is never uploaded anywhere.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from concurrency.atomic import atomic_write_text

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _tokenise_hint(subject_hint: str) -> list[str]:
    tokens = re.split(r"[\s\-_]+", subject_hint.lower())
    return [t for t in tokens if len(t) >= 4]


def fetch_mail_context(
    session_start: datetime,
    subject_hint: str,
    search_window_hours: float = 24.0,
) -> str | None:
    """Best-effort: find the best-matching Outlook mail item near `session_start`.

    Args:
        session_start: Reference time; mail items within
            [session_start - search_window_hours, session_start + search_window_hours]
            are considered.
        subject_hint: Free text (typically the meeting title) tokenised (>=4
            chars) and matched against each candidate mail item's subject.
        search_window_hours: Half-width of the search window, in hours.

    Returns:
        The best-matching mail item's body (plain text, truncated to ~2000
        characters), or None if Outlook is unreachable, no items fall in the
        window, or the best match's token-overlap score is below 0.3.
    """
    hint_tokens = _tokenise_hint(subject_hint)
    if not hint_tokens:
        return None

    try:
        import win32com.client

        outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        inbox = outlook.GetDefaultFolder(6)  # olFolderInbox
        items = inbox.Items
        items.Sort("[ReceivedTime]", True)

        window_start = session_start - timedelta(hours=search_window_hours)
        window_end = session_start + timedelta(hours=search_window_hours)

        best_score = 0.0
        best_body: str | None = None

        for item in items:
            try:
                received = item.ReceivedTime
                # pywintypes datetime is tz-aware; compare naive-to-naive.
                received_naive = datetime(
                    received.year, received.month, received.day,
                    received.hour, received.minute, received.second,
                )
            except AttributeError:
                continue
            if received_naive < window_start:
                break  # sorted descending; nothing earlier can match either
            if received_naive > window_end:
                continue

            subject = (getattr(item, "Subject", "") or "").lower()
            if not subject:
                continue
            overlap = sum(1 for tok in hint_tokens if tok in subject)
            score = overlap / len(hint_tokens)
            if score > best_score:
                body = getattr(item, "Body", "") or ""
                if not body.strip():
                    html_body = getattr(item, "HTMLBody", "") or ""
                    body = _HTML_TAG_RE.sub(" ", html_body)
                best_score = score
                best_body = body

        if best_score >= 0.3 and best_body:
            return best_body[:2000]
        return None
    except Exception as exc:  # noqa: BLE001 - Outlook may not be installed/open; never crash the pipeline
        logger.warning("fetch_mail_context: Outlook COM unavailable or failed: %s", exc)
        return None


def save_mail_context(session_id: str, meetings_dir: Path | str, body: str) -> Path:
    """Write the matched mail body to `<session_id>.mail_context.txt`."""
    meetings_dir = Path(meetings_dir)
    meetings_dir.mkdir(parents=True, exist_ok=True)
    output_path = meetings_dir / f"{session_id}.mail_context.txt"
    atomic_write_text(output_path, body)
    return output_path
