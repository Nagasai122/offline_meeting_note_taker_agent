"""
Detects blockers that recur across a researcher's recent IS-call sessions
without being resolved, and suggests a concrete escalation action for each.

Additive, best-effort reasoning (architecture_v2.md §Phase 6.5) -- wired into
the extraction pipeline only for MeetingType.IS_CALL sessions, always inside a
try/except so a failure here never fails the session itself.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from concurrency.atomic import atomic_write_text

logger = logging.getLogger(__name__)

ESCALATION_PROMPT = """You are analysing daily progress call notes from a researcher over the past {n} sessions.

Session summaries (newest first):
{summaries_json}

Identify any blockers, unresolved issues, or topics that appear in 3 or more of these
sessions without being resolved. Respond with ONLY a JSON array, no commentary and no
markdown code fence. For each recurring blocker:
- "theme": a 5-10 word label describing the blocker
- "occurrences": list of session IDs where it appeared
- "first_seen": session_id of the earliest occurrence
- "suggested_action": one concrete escalation action (e.g. 'Schedule dedicated meeting',
  'Raise with supervisor', 'File support ticket', 'Reassign task')

If no recurring blockers are found, output an empty list [].
"""


def _clean_json(raw: str) -> str:
    return re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()


def detect_recurring_blockers(
    meetings_dir: Path | str,
    llm_call: Callable[[str, str], str],
    n_sessions: int = 7,
) -> list[dict]:
    """Analyse the last `n_sessions` IS-call summaries for recurring blockers.

    Args:
        meetings_dir: Directory containing `<session_id>.summary.md` artefacts.
        llm_call: Callable(system_prompt, user_prompt) -> response text.
        n_sessions: How many of the most recent is-call-* sessions to consider.

    Returns:
        Parsed list of recurring-blocker dicts (empty list if none found or
        fewer than 3 IS-call sessions exist to compare). Also writes
        `data/recurring_blockers.json` (meetings_dir.parent / "recurring_blockers.json").
    """
    meetings_dir = Path(meetings_dir)
    summary_files = sorted(
        meetings_dir.glob("is-call-*.summary.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:n_sessions]

    if len(summary_files) < 3:
        return []

    summaries = {}
    for p in summary_files:
        session_id = p.name.replace(".summary.md", "")
        summaries[session_id] = p.read_text(encoding="utf-8")

    prompt = ESCALATION_PROMPT.format(n=len(summaries), summaries_json=json.dumps(summaries, indent=2))
    raw_response = llm_call(prompt, "Identify recurring blockers now.")
    parsed = json.loads(_clean_json(raw_response))
    if not isinstance(parsed, list):
        raise ValueError(f"Expected a JSON array from blocker-escalation LLM call, got {type(parsed).__name__}.")

    output_path = meetings_dir.parent / "recurring_blockers.json"
    atomic_write_text(
        output_path,
        json.dumps({"blockers": parsed, "last_updated": datetime.now().isoformat()}, indent=2),
    )
    return parsed
