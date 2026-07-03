"""
Cross-meeting weekly digest: aggregates the past `since_days` days of sessions
(all meeting types, not just IS calls) into one structured pattern summary.

Deliberately not auto-run on any timer or page load -- it triggers a real LLM
call, so it is only ever produced on explicit user request (the dashboard's
"Weekly Digest" button), per architecture_v2.md §Phase 6.6. Cached for 6
hours so repeated clicks within that window don't re-run the LLM call.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from concurrency.atomic import atomic_write_text

WEEKLY_PATTERN_PROMPT = """You are analysing a researcher's meeting notes from the past week.

Sessions this week:
{sessions_json}

Produce a structured weekly summary. Respond with ONLY a JSON object, no commentary and
no markdown code fence, with these keys:
- "key_decisions": list of significant decisions made across all meetings
- "recurring_topics": list of topics that appeared in multiple sessions
- "open_action_count": total number of open (unresolved) action items
- "high_priority_open": list of HIGH priority action item description strings not yet marked done
- "completed_count": total action items marked as done or addressed this week
- "insight": 2-3 sentence narrative observation about the week's work pattern
"""

_CACHE_MAX_AGE_HOURS = 6


def _clean_json(raw: str) -> str:
    return re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()


def generate_weekly_summary(
    meetings_dir: Path | str,
    state_dir: Path | str,
    llm_call: Callable[[str, str], str],
    since_days: int = 7,
) -> dict:
    """Aggregate the last `since_days` of sessions into a cross-meeting summary.

    Args:
        meetings_dir: Directory containing `.summary.md`/`.actions.json` artefacts.
        state_dir: Unused directly (kept for interface symmetry with other
            pipeline-adjacent tools); session filtering here is purely by
            artefact mtime, not state-machine status.
        llm_call: Callable(system_prompt, user_prompt) -> response text.
        since_days: Look-back window in days.

    Returns:
        Parsed summary dict. Also writes `data/weekly_summary.json`
        (meetings_dir.parent / "weekly_summary.json").
    """
    meetings_dir = Path(meetings_dir)
    cutoff = datetime.now() - timedelta(days=since_days)

    sessions: dict[str, dict] = {}
    for summary_path in meetings_dir.glob("*.summary.md"):
        if datetime.fromtimestamp(summary_path.stat().st_mtime) < cutoff:
            continue
        session_id = summary_path.name.replace(".summary.md", "")
        # Reviewed sessions only (same SB-6 indexability rule as BM25 search):
        # a FAILED or still-in-flight session's artefacts must not leak into
        # the digest -- previously filtering was by artefact mtime alone
        # (audit 2026-07, Strand E candidate-5 assessment).
        if not _session_is_digestible(session_id, state_dir):
            continue
        actions_path = meetings_dir / f"{session_id}.actions.json"
        actions = json.loads(actions_path.read_text(encoding="utf-8")) if actions_path.exists() else []
        sessions[session_id] = {
            "summary": summary_path.read_text(encoding="utf-8"),
            "action_items": actions,
        }

    prompt = WEEKLY_PATTERN_PROMPT.format(sessions_json=json.dumps(sessions, indent=2))
    raw_response = llm_call(prompt, "Produce the weekly summary now.")
    parsed = json.loads(_clean_json(raw_response))
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object from weekly-summary LLM call, got {type(parsed).__name__}.")

    now = datetime.now()
    record = {
        "summary": parsed,
        "generated_at": now.isoformat(),
        "session_count": len(sessions),
        "iso_week": f"{now.isocalendar().year}-W{now.isocalendar().week:02d}",
    }
    record["trend"] = _week_over_week_trend(meetings_dir.parent, record)

    output_path = meetings_dir.parent / "weekly_summary.json"
    atomic_write_text(output_path, json.dumps(record, indent=2))
    # Per-week history file (never overwritten by later weeks): the GTD-style
    # weekly-review ritual needs last week to still exist next week.
    history_dir = meetings_dir.parent / "weekly_summaries"
    atomic_write_text(history_dir / f"{record['iso_week']}.json", json.dumps(record, indent=2))
    parsed["trend"] = record["trend"]
    return parsed


def _session_is_digestible(session_id: str, state_dir: Path | str) -> bool:
    """PROPOSED/REVIEWED/APPLIED sessions only; unknown/corrupt state -> excluded."""
    from mcp_server import state as state_mod

    try:
        session = state_mod.load_session_state(state_dir, session_id)
    except (FileNotFoundError, ValueError, KeyError):
        return False
    return session.state in (
        state_mod.State.PROPOSED, state_mod.State.REVIEWED, state_mod.State.APPLIED,
    )


def _week_over_week_trend(data_dir: Path, current: dict) -> dict | None:
    """Compare this week's open/completed counts against the most recent
    *previous* week's stored digest. Returns None when no prior week exists."""
    history_dir = data_dir / "weekly_summaries"
    if not history_dir.exists():
        return None
    prior_files = sorted(
        p for p in history_dir.glob("*.json") if p.stem != current["iso_week"]
    )
    if not prior_files:
        return None
    try:
        prior = json.loads(prior_files[-1].read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None

    def _count(record: dict, key: str) -> int:
        value = record.get("summary", {}).get(key, 0)
        return value if isinstance(value, int) else 0

    return {
        "vs_week": prior.get("iso_week", prior_files[-1].stem),
        "open_action_delta": _count(current, "open_action_count") - _count(prior, "open_action_count"),
        "completed_delta": _count(current, "completed_count") - _count(prior, "completed_count"),
        "session_count_delta": current["session_count"] - prior.get("session_count", 0),
    }


def load_cached_weekly_summary(meetings_dir: Path | str) -> dict | None:
    """Return the cached weekly summary if it exists and is younger than
    `_CACHE_MAX_AGE_HOURS`, else None."""
    output_path = Path(meetings_dir).parent / "weekly_summary.json"
    if not output_path.exists():
        return None
    cached = json.loads(output_path.read_text(encoding="utf-8"))
    generated_at = datetime.fromisoformat(cached["generated_at"])
    if datetime.now() - generated_at > timedelta(hours=_CACHE_MAX_AGE_HOURS):
        return None
    return cached
