from __future__ import annotations

import json

import pytest

from mcp_server.state import State, create_session, load_session_state
from mcp_server.tools.extraction import ExtractionError, extract_action_items
from tests.mcp_server.fakes import FakeLLMClient


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
        tmp_path, [{"speaker": "Naga", "text": "I will send the report by Friday."}]
    )
    llm = FakeLLMClient(response='[{"description": "Send the report", "owner": "Naga", "due_date": "2026-07-03"}]')

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)

    assert result["state"] == "EXTRACTED"
    assert result["action_items"] == [{"description": "Send the report", "owner": "Naga", "due_date": "2026-07-03"}]
    assert load_session_state(state_dir, "s1").state == State.EXTRACTED
    assert json.loads((meetings_dir / "s1.actions.json").read_text()) == result["action_items"]
    assert len(llm.calls) == 1


def test_extract_action_items_strips_markdown_fence(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(tmp_path, [{"speaker": "A", "text": "No action needed."}])
    llm = FakeLLMClient(response="```json\n[]\n```")

    result = extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)
    assert result["action_items"] == []


def test_extract_action_items_no_transcript_raises(tmp_path):
    state_dir = tmp_path / "state"
    lock_path = tmp_path / ".lock"
    create_session(state_dir, "s1", lock_path, 1.0, initial_state=State.TRANSCRIBED)
    with pytest.raises(FileNotFoundError):
        extract_action_items("s1", tmp_path / "meetings", state_dir, lock_path, 1.0, FakeLLMClient("[]"))


def test_malformed_llm_response_raises_extraction_error_and_marks_failed(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(tmp_path, [{"speaker": "A", "text": "Hello."}])
    llm = FakeLLMClient(response="not json at all")

    with pytest.raises(ExtractionError):
        extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)

    assert load_session_state(state_dir, "s1").state == State.FAILED


def test_llm_response_missing_description_field_raises(tmp_path):
    meetings_dir, state_dir, lock_path = _setup(tmp_path, [{"speaker": "A", "text": "Hello."}])
    llm = FakeLLMClient(response='[{"owner": "X"}]')

    with pytest.raises(ExtractionError, match="missing 'description'"):
        extract_action_items("s1", meetings_dir, state_dir, lock_path, 1.0, llm)
