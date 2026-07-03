"""
IS-call loop-closure: reasons over whether each target from the *previous*
IS call was addressed, partially addressed, missed, or explicitly carried
forward, using the current session's transcript summary as evidence.

This is additive, best-effort reasoning wired into extract_action_items only
for MeetingType.IS_CALL sessions (architecture_v2.md §Phase 6.4). A failure
here must never fail the extraction pipeline -- the caller wraps this in a
try/except and logs a warning, per claude_cli_implement_v2.md's cross-cutting
error-handling rule.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

from concurrency.atomic import atomic_write_text

logger = logging.getLogger(__name__)

LOOP_CLOSURE_PROMPT = """You are reviewing progress on action items from a researcher's previous daily IS call.

Previous session targets (from {prev_session_id} on {prev_date}):
{prev_action_items_json}

Current session transcript summary:
{current_summary}

For each previous target, determine its status based on what was discussed in the
current session. Respond with ONLY a JSON array, no commentary and no markdown code fence.
Each item must have:
- "id": the original action item ID (copy exactly)
- "description": the original description (copy exactly)
- "status": one of "addressed" | "partial" | "missed" | "carried_forward"
- "evidence": a 1-sentence quote or paraphrase from the current session that
  supports your status assessment. If no evidence, use null.
- "carried_to": if status is "carried_forward", copy the description into a new
  action item suggestion; otherwise null.

Definitions:
- addressed: the item was completed or explicitly confirmed as done.
- partial: progress was made but the item is not fully resolved.
- missed: the item was not mentioned and no progress is evident.
- carried_forward: the IS explicitly re-assigned or extended the deadline.
"""


def _find_prior_is_call_session(meetings_dir: Path, current_session_id: str) -> str | None:
    """Return the most recent is-call-* session (by .summary.md mtime) that is
    not the current session, or None if no prior IS call exists."""
    candidates = [
        p for p in meetings_dir.glob("is-call-*.summary.md")
        if p.stem.replace(".summary", "") != current_session_id
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0].name.replace(".summary.md", "")


def _clean_json(raw: str) -> str:
    return re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()


def close_prior_targets(
    state_dir: Path | str,
    meetings_dir: Path | str,
    session_id: str,
    current_summary: str,
    llm_call: Callable[[str, str], str],
    lock_path: Path | str,
    lock_timeout: float,
) -> dict | None:
    """Run loop-closure reasoning for IS-call sessions only.

    Args:
        state_dir: Unused directly here (kept for signature parity with other
            pipeline steps / future use), present for interface consistency.
        meetings_dir: Directory containing `.actions.json`/`.summary.md` artefacts.
        session_id: The current (IS-call) session_id.
        current_summary: This session's extracted summary text.
        llm_call: Callable(system_prompt, user_prompt) -> response text.
        lock_path: Unused directly (no state-machine write happens here).
        lock_timeout: Unused directly.

    Returns:
        None immediately if session_id does not start with "is-call-", or if no
        prior IS-call session exists. Otherwise the parsed loop-closure list
        wrapped in {"prev_session_id": ..., "items": [...]}; also writes
        `<session_id>.loop_closure.json` and appends any "carried_forward"
        items to `<session_id>.actions.json`.
    """
    if not session_id.startswith("is-call-"):
        return None

    meetings_dir = Path(meetings_dir)
    prev_session_id = _find_prior_is_call_session(meetings_dir, session_id)
    if prev_session_id is None:
        return None

    prev_actions_path = meetings_dir / f"{prev_session_id}.actions.json"
    if not prev_actions_path.exists():
        return None
    prev_action_items = json.loads(prev_actions_path.read_text(encoding="utf-8"))
    if not prev_action_items:
        return None

    prev_date = prev_session_id.split("-")[-2] if "-" in prev_session_id else "unknown"
    prompt = LOOP_CLOSURE_PROMPT.format(
        prev_session_id=prev_session_id,
        prev_date=prev_date,
        prev_action_items_json=json.dumps(prev_action_items, indent=2),
        current_summary=current_summary,
    )
    raw_response = llm_call(prompt, "Assess each previous target now.")
    parsed = json.loads(_clean_json(raw_response))
    if not isinstance(parsed, list):
        raise ValueError(f"Expected a JSON array from loop-closure LLM call, got {type(parsed).__name__}.")

    result = {"prev_session_id": prev_session_id, "items": parsed}
    loop_closure_path = meetings_dir / f"{session_id}.loop_closure.json"
    atomic_write_text(loop_closure_path, json.dumps(result, indent=2))

    carried = [item for item in parsed if item.get("status") == "carried_forward" and item.get("carried_to")]
    if carried:
        actions_path = meetings_dir / f"{session_id}.actions.json"
        current_actions = json.loads(actions_path.read_text(encoding="utf-8")) if actions_path.exists() else []
        for item in carried:
            current_actions.append({
                "description": item["carried_to"],
                "owner": None,
                "due_date": None,
                "priority": "MEDIUM",
                "carried_from": prev_session_id,
            })
        atomic_write_text(actions_path, json.dumps(current_actions, indent=2))

    return result
