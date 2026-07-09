from __future__ import annotations

import json

import pytest

from config.loader import IdentityConfig
from mcp_server.project import Project
from mcp_server.state import State, create_session, load_session_state
from mcp_server.tools.extraction import ExtractionError, extract_action_items
from tests.mcp_server.fakes import FakeLLMClient

# Fix 2.3 in mcp_server/tools/extraction.py skips the LLM entirely for
# transcripts under MIN_TRANSCRIPT_WORDS (50) words. Tests that need the fake
# LLM to actually be consulted must therefore use a transcript longer than
# that threshold; this filler sentence block is ~52 words on its own.
_FILLER = (
    "We spent the first part of the meeting walking through the deployment "
    "checklist, reviewing open incidents, and agreeing on owners for each "
    "remaining task before the release. Everyone confirmed the staging "
    "environment matched production configuration and that the rollback plan "
    "had been rehearsed end to end earlier this week without any surprises."
)


def _setup(tmp_path, segments):
    meetings_dir = tmp_path / "meetings"
    meetings_dir.mkdir()
    (meetings_dir / "s1.json").write_text(json.dumps({"segments": segments}))
    state_dir = tmp_path / "state"
    lock_path = tmp_path / ".lock"
    create_session(state_dir, "s1", lock_path, 1.0, initial_state=State.TRANSCRIBED)
    return meetings_dir, state_dir, lock_path


def test_extract_action_items_happy_path(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path,
        [
            {"speaker": "Naga", "text": _FILLER},
            {"speaker": "Naga", "text": "I will send the report by Friday."},
        ],
    )
    llm = FakeLLMClient(
        response=json.dumps({
            "summary": "- Report due Friday.",
            "action_items": [{"description": "Send the report", "owner": "Naga", "due_date": "2026-07-03"}],
        })
    )

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)

    assert result["state"] == "EXTRACTED"
    # P2: _normalize_action_item_ownership always adds owner_type/confidence
    # (defaulting "unknown"/None here since no identity/active_projects were
    # passed in) -- every action item now carries these keys, not just ones
    # the model explicitly classified.
    assert result["action_items"] == [{
        "description": "Send the report", "owner": "Naga", "due_date": "2026-07-03",
        "owner_type": "unknown", "confidence": None,
    }]
    assert load_session_state(state_dir, "s1").state == State.EXTRACTED
    assert json.loads((meetings_dir / "s1.actions.json").read_text()) == result["action_items"]
    assert len(llm.calls) == 1


def test_extract_action_items_strips_markdown_fence(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path,
        [
            {"speaker": "A", "text": _FILLER},
            {"speaker": "A", "text": "No action needed."},
        ],
    )
    llm = FakeLLMClient(response='```json\n{"summary": "- Nothing to do.", "action_items": []}\n```')

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)
    assert result["action_items"] == []
    assert len(llm.calls) == 1  # the fenced response really was parsed, not skipped


def test_extract_action_items_no_transcript_raises(tmp_path):
    state_dir = tmp_path / "state"
    lock_path = tmp_path / ".lock"
    create_session(state_dir, "s1", lock_path, 1.0, initial_state=State.TRANSCRIBED)
    with pytest.raises(FileNotFoundError):
        extract_action_items("s1", tmp_path / "meetings", state_dir, lock_path, 1.0, FakeLLMClient("[]"))


def test_malformed_llm_response_raises_extraction_error_and_marks_failed(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(tmp_path, [{"speaker": "A", "text": _FILLER}])
    llm = FakeLLMClient(response="not json at all")

    with pytest.raises(ExtractionError):
        extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)

    assert load_session_state(state_dir, "s1").state == State.FAILED


def test_llm_response_missing_description_field_raises(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(tmp_path, [{"speaker": "A", "text": _FILLER}])
    llm = FakeLLMClient(response='{"summary": "- x", "action_items": [{"owner": "X"}]}')

    with pytest.raises(ExtractionError, match="missing 'description'"):
        extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)


# ---------------------------------------------------------------------------
# P2: ownership classification (identity/active_projects context injection +
# server-side normalization of owner_type/confidence/project_id)
# ---------------------------------------------------------------------------

def test_identity_context_injected_into_system_prompt(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path, [{"speaker": "A", "text": _FILLER}, {"speaker": "A", "text": "No action needed."}],
    )
    llm = FakeLLMClient(response='{"summary": "- x", "action_items": []}')
    identity = IdentityConfig(name="Naga Sai", aliases=["Naga"], institution="UREAD")

    extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm, identity=identity)

    system_prompt = llm.calls[0][0]
    assert "USER IDENTITY" in system_prompt
    assert "Naga Sai" in system_prompt
    assert "UREAD" in system_prompt


def test_unconfigured_identity_omits_identity_block(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path, [{"speaker": "A", "text": _FILLER}, {"speaker": "A", "text": "No action needed."}],
    )
    llm = FakeLLMClient(response='{"summary": "- x", "action_items": []}')

    extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm, identity=IdentityConfig())

    # The base contract's instructions always mention "USER IDENTITY" by
    # name (it's explaining the classification rule); what must NOT appear
    # is the actual injected block header this test is really checking for.
    assert "USER IDENTITY (for owner_type classification)" not in llm.calls[0][0]


def test_active_projects_context_injected_and_archived_excluded(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path, [{"speaker": "A", "text": _FILLER}, {"speaker": "A", "text": "No action needed."}],
    )
    llm = FakeLLMClient(response='{"summary": "- x", "action_items": []}')
    projects = [
        Project(name="CyberSec Consortium", id="p1", status="active"),
        Project(name="Old Project", id="p2", status="archived"),
    ]

    extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm, active_projects=projects)

    system_prompt = llm.calls[0][0]
    assert "ACTIVE PROJECTS" in system_prompt
    assert "CyberSec Consortium" in system_prompt
    assert "Old Project" not in system_prompt


def test_valid_owner_type_and_confidence_pass_through(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path, [{"speaker": "A", "text": _FILLER}, {"speaker": "A", "text": "I will send the report."}],
    )
    llm = FakeLLMClient(response=json.dumps({
        "summary": "- x",
        "action_items": [{"description": "Send report", "owner_type": "self", "confidence": 0.9}],
    }))

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)

    item = result["action_items"][0]
    assert item["owner_type"] == "self"
    assert item["confidence"] == 0.9


def test_out_of_vocabulary_owner_type_coerced_to_unknown(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path, [{"speaker": "A", "text": _FILLER}, {"speaker": "A", "text": "Someone will do this."}],
    )
    llm = FakeLLMClient(response=json.dumps({
        "summary": "- x",
        "action_items": [{"description": "Do the thing", "owner_type": "made_up_value"}],
    }))

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)

    assert result["action_items"][0]["owner_type"] == "unknown"


def test_out_of_range_confidence_coerced_to_none(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path, [{"speaker": "A", "text": _FILLER}, {"speaker": "A", "text": "Someone will do this."}],
    )
    llm = FakeLLMClient(response=json.dumps({
        "summary": "- x",
        "action_items": [{"description": "Do the thing", "confidence": 1.7}],
    }))

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)

    assert result["action_items"][0]["confidence"] is None


def test_project_id_not_in_active_list_coerced_to_none(tmp_path):
    """Server-side backstop for 'extraction can match, never silently
    create' -- a project_id the model hallucinates (not present in the
    active list passed in) must never survive into the stored result."""
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path, [{"speaker": "A", "text": _FILLER}, {"speaker": "A", "text": "Someone will do this."}],
    )
    llm = FakeLLMClient(response=json.dumps({
        "summary": "- x",
        "action_items": [{"description": "Do the thing", "project_id": "hallucinated-id"}],
    }))
    projects = [Project(name="Real Project", id="real1", status="active")]

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm, active_projects=projects)

    assert result["action_items"][0]["project_id"] is None


def test_project_id_matching_active_project_is_preserved(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(
        tmp_path, [{"speaker": "A", "text": _FILLER}, {"speaker": "A", "text": "Someone will do this."}],
    )
    llm = FakeLLMClient(response=json.dumps({
        "summary": "- x",
        "action_items": [{"description": "Do the thing", "project_id": "real1"}],
    }))
    projects = [Project(name="Real Project", id="real1", status="active")]

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm, active_projects=projects)

    assert result["action_items"][0]["project_id"] == "real1"
