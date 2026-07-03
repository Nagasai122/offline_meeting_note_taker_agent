"""
Fix 3.4 / SB-5.1: Record human feedback (rejections and edits) from the review UI.

Each record is appended as a JSONL line to data/feedback/rejections.jsonl or
data/feedback/edits.jsonl. The meeting_type field (SB-5.1) allows
_load_negative_examples() in extraction.py to filter to same-type examples
only, so a rejection from an IS call never becomes a negative example for a
project meeting's extraction prompt.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def record_rejection(
    session_id: str,
    item_id: str,
    item_description: str,
    rejection_reason: str,
    feedback_dir: Path,
    meeting_type: str = "",
) -> None:
    """Append one rejection record to data/feedback/rejections.jsonl."""
    feedback_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": str(uuid.uuid4()),
        "at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "item_id": item_id,
        "item_description": item_description,
        "rejection_reason": rejection_reason,
        "meeting_type": meeting_type,
    }
    path = feedback_dir / "rejections.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    logger.debug("Recorded rejection for item %s in session %s", item_id, session_id)


def record_edit(
    session_id: str,
    item_id: str,
    original: str,
    corrected: str,
    feedback_dir: Path,
    meeting_type: str = "",
) -> None:
    """Append one edit record to data/feedback/edits.jsonl."""
    feedback_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": str(uuid.uuid4()),
        "at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "item_id": item_id,
        "original": original,
        "corrected": corrected,
        "meeting_type": meeting_type,
    }
    path = feedback_dir / "edits.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    logger.debug("Recorded edit for item %s in session %s", item_id, session_id)
