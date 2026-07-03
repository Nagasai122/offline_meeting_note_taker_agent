"""Tests for cli/weekly_summary.py including the 2026-07 extensions:
state-based session filtering, per-week history, week-over-week trend."""

from __future__ import annotations

import json
from datetime import datetime

from cli.weekly_summary import generate_weekly_summary, load_cached_weekly_summary
from mcp_server.state import State, create_session


def _fake_llm(system_prompt: str, user_prompt: str) -> str:
    return json.dumps({
        "key_decisions": ["Adopt Redis"],
        "recurring_topics": ["caching"],
        "open_action_count": 3,
        "high_priority_open": ["Draft ADR"],
        "completed_count": 2,
        "insight": "Steady week.",
    })


def _seed_session(tmp_path, session_id: str, state: State) -> None:
    meetings = tmp_path / "meetings"
    meetings.mkdir(exist_ok=True)
    (meetings / f"{session_id}.summary.md").write_text(f"- summary of {session_id}\n", encoding="utf-8")
    (meetings / f"{session_id}.actions.json").write_text("[]", encoding="utf-8")
    create_session(tmp_path / "state", session_id, tmp_path / ".lock", 1.0, initial_state=state)


def test_digest_includes_only_reviewed_pipeline_states(tmp_path):
    _seed_session(tmp_path, "good-1", State.APPLIED)
    _seed_session(tmp_path, "good-2", State.PROPOSED)
    _seed_session(tmp_path, "failed-1", State.FAILED)
    _seed_session(tmp_path, "inflight-1", State.TRANSCRIBED)

    captured = {}

    def llm(system_prompt: str, user_prompt: str) -> str:
        captured["prompt"] = system_prompt
        return _fake_llm(system_prompt, user_prompt)

    generate_weekly_summary(tmp_path / "meetings", tmp_path / "state", llm)

    assert "good-1" in captured["prompt"]
    assert "good-2" in captured["prompt"]
    assert "failed-1" not in captured["prompt"]
    assert "inflight-1" not in captured["prompt"]


def test_digest_writes_per_week_history_and_cache(tmp_path):
    _seed_session(tmp_path, "good-1", State.APPLIED)

    generate_weekly_summary(tmp_path / "meetings", tmp_path / "state", _fake_llm)

    iso = datetime.now().isocalendar()
    week_file = tmp_path / "weekly_summaries" / f"{iso.year}-W{iso.week:02d}.json"
    assert week_file.exists()
    cached = load_cached_weekly_summary(tmp_path / "meetings")
    assert cached is not None
    assert cached["session_count"] == 1


def test_week_over_week_trend_computed_against_prior_week(tmp_path):
    _seed_session(tmp_path, "good-1", State.APPLIED)
    history = tmp_path / "weekly_summaries"
    history.mkdir()
    (history / "2026-W20.json").write_text(json.dumps({
        "iso_week": "2026-W20",
        "session_count": 4,
        "summary": {"open_action_count": 7, "completed_count": 5},
    }), encoding="utf-8")

    result = generate_weekly_summary(tmp_path / "meetings", tmp_path / "state", _fake_llm)

    trend = result["trend"]
    assert trend["vs_week"] == "2026-W20"
    assert trend["open_action_delta"] == 3 - 7
    assert trend["completed_delta"] == 2 - 5
    assert trend["session_count_delta"] == 1 - 4


def test_trend_is_none_with_no_prior_week(tmp_path):
    _seed_session(tmp_path, "good-1", State.APPLIED)
    result = generate_weekly_summary(tmp_path / "meetings", tmp_path / "state", _fake_llm)
    assert result["trend"] is None
