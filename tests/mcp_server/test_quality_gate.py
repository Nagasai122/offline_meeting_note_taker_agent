"""
Unit tests for mcp_server/quality_gate.py (Fix 3.1).

Tests exercise the three scoring dimensions (grounding, completeness,
action_density) and verify that the grounded/hallucinated discrimination
boundary is where the bugfix_03 spec says it should be.  No LLM, no
filesystem, no network — pure Python.
"""
from __future__ import annotations

import pytest

from mcp_server.meeting_type import MeetingType
from mcp_server.quality_gate import QualityScore, score_extraction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_items(*descriptions: str) -> list[dict]:
    return [{"description": d, "owner": None, "due_date": None, "priority": "MEDIUM"}
            for d in descriptions]


# ---------------------------------------------------------------------------
# Grounded extraction — words in descriptions appear in transcript
# ---------------------------------------------------------------------------

def test_grounded_extraction_scores_high():
    transcript = (
        "John will fix the authentication bug by Friday. "
        "Sarah will update the documentation before the release."
    )
    extracted = {
        "summary": "John fixes auth bug; Sarah updates documentation.",
        "action_items": _make_items(
            "fix the authentication bug",
            "update the documentation",
        ),
    }
    score = score_extraction(extracted, transcript, MeetingType.GENERAL)
    assert score.overall >= 0.6, f"Expected >= 0.6, got {score.overall}"
    assert score.label in ("HIGH", "MEDIUM"), f"Unexpected label: {score.label}"
    assert score.grounding >= 0.5


def test_hallucinated_extraction_scores_low():
    """Extraction whose terms have no basis in the (short) transcript."""
    transcript = "We had a quick catch-up."
    extracted = {
        "summary": (
            "Comprehensive team meeting covering all project areas "
            "with detailed discussion of quarterly financials and hiring."
        ),
        "action_items": _make_items(
            "prepare quarterly financial report",
            "schedule stakeholder interviews",
            "review architecture documentation",
        ),
    }
    score = score_extraction(extracted, transcript, MeetingType.GENERAL)
    assert score.overall < 0.6, f"Expected < 0.6, got {score.overall}"
    assert score.label == "LOW", f"Expected LOW, got {score.label}"
    assert "LOW_GROUNDING" in score.flags or score.grounding < 0.4


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_action_items_general_scores_medium_not_low():
    """General meeting with a good summary but no action items — not LOW."""
    transcript = "We discussed the roadmap in general terms, no concrete tasks assigned."
    extracted = {
        "summary": "Roadmap discussed — no concrete tasks assigned.",
        "action_items": [],
    }
    score = score_extraction(extracted, transcript, MeetingType.GENERAL)
    # No items and no IS_CALL flag → action_density defaults to 1.0 for short
    # transcripts (<60 words). Should not be LOW.
    assert score.label in ("HIGH", "MEDIUM"), f"Got {score.label} with flags {score.flags}"


def test_seminar_zero_action_items_is_not_penalised():
    """Seminars are expected to have zero action items; density score = 1.0."""
    transcript = "The speaker explained transformer architectures and self-attention."
    extracted = {
        "summary": "Transformer architectures and self-attention explained.",
        "action_items": [],
        "key_concepts": ["self-attention", "transformer"],
        "topic": "Deep learning architectures",
    }
    score = score_extraction(extracted, transcript, MeetingType.SEMINAR)
    assert "NO_ACTION_ITEMS_IS_CALL" not in score.flags
    assert score.action_density == 1.0


def test_is_call_no_action_items_gets_flag():
    """IS calls without any extracted action items receive a diagnostic flag.
    The guard only fires when word_count >= 60 (quality_gate.py line ~95),
    so the fixture must exceed that threshold to exercise the code path."""
    # Transcript must have >= 60 words so quality_gate.py's density guard
    # runs (the short-circuit at word_count < 60 would otherwise suppress the flag).
    transcript = (
        "Daily IS call standup. Yesterday I completed the CI pipeline setup "
        "and ran all the integration tests which passed successfully. "
        "Today I will work on the deployment scripts and coordinate with "
        "the DevOps team to review infrastructure changes. "
        "Blocker: still waiting on access credentials from the IT department, "
        "which is blocking the production deployment and needs urgent resolution. "
        "No new action items were explicitly assigned or raised during this call."
    )
    extracted = {
        "summary": "CI pipeline done; blocked on credentials.",
        "action_items": [],
        "progress_reported": "CI pipeline setup completed",
        "continuation_summary": "Waiting on IT credentials",
    }
    score = score_extraction(extracted, transcript, MeetingType.IS_CALL)
    assert "NO_ACTION_ITEMS_IS_CALL" in score.flags


def test_missing_required_fields_lowers_completeness():
    """A project meeting missing 'decisions' gets MISSING_FIELDS flag."""
    transcript = "We decided to postpone the launch. Alice will update the roadmap."
    extracted = {
        "summary": "Launch postponed; roadmap update assigned.",
        "action_items": _make_items("update the roadmap"),
        # 'decisions' key intentionally absent
    }
    score = score_extraction(extracted, transcript, MeetingType.PROJECT)
    assert any("MISSING_FIELDS" in f for f in score.flags), \
        f"Expected MISSING_FIELDS flag, got: {score.flags}"


def test_short_summary_is_penalised():
    """Summaries under 30 characters indicate poor extraction quality."""
    transcript = "We discussed the project timeline and budget constraints in detail."
    extracted = {
        "summary": "ok",   # too short
        "action_items": _make_items("review the timeline"),
    }
    score = score_extraction(extracted, transcript, MeetingType.GENERAL)
    assert score.completeness < 1.0  # completeness penalised by the short-summary guard


def test_score_is_deterministic():
    """Same inputs always produce the same score (no RNG)."""
    transcript = "Alice will write the test plan by Monday."
    extracted = {
        "summary": "Test plan assigned to Alice.",
        "action_items": _make_items("write the test plan"),
    }
    s1 = score_extraction(extracted, transcript, MeetingType.GENERAL)
    s2 = score_extraction(extracted, transcript, MeetingType.GENERAL)
    assert s1.overall == s2.overall
    assert s1.label == s2.label
    assert s1.flags == s2.flags