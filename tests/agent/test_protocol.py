from __future__ import annotations

import pytest

from agent.protocol import ProtocolError, parse_decision


def test_parses_tool_call_decision():
    raw = '{"thought": "I should check status first", "action": "get_session_status", "arguments": {"session_id": "s1"}}'
    decision = parse_decision(raw)
    assert decision.thought == "I should check status first"
    assert decision.action == "get_session_status"
    assert decision.arguments == {"session_id": "s1"}
    assert not decision.is_final


def test_parses_final_decision_without_arguments():
    raw = '{"thought": "done", "action": "final", "summary": "Reached PROPOSED."}'
    decision = parse_decision(raw)
    assert decision.is_final
    assert decision.summary == "Reached PROPOSED."
    assert decision.arguments == {}


def test_strips_markdown_code_fence():
    raw = '```json\n{"thought": "x", "action": "final", "summary": "done"}\n```'
    decision = parse_decision(raw)
    assert decision.is_final


def test_rejects_invalid_json():
    with pytest.raises(ProtocolError):
        parse_decision("not json at all")


def test_rejects_non_object_json():
    with pytest.raises(ProtocolError):
        parse_decision('["thought", "action"]')


def test_rejects_missing_thought():
    with pytest.raises(ProtocolError):
        parse_decision('{"action": "final", "summary": "x"}')


def test_rejects_missing_action():
    with pytest.raises(ProtocolError):
        parse_decision('{"thought": "x"}')


def test_rejects_non_object_arguments():
    with pytest.raises(ProtocolError):
        parse_decision('{"thought": "x", "action": "stop_meeting", "arguments": "s1"}')


def test_rejects_non_string_summary():
    with pytest.raises(ProtocolError):
        parse_decision('{"thought": "x", "action": "final", "summary": 123}')


def test_prose_around_json_is_rejected():
    # The protocol requires exactly one JSON object and nothing else --
    # surrounding prose must not be silently stripped away.
    with pytest.raises(ProtocolError):
        parse_decision('Sure, here is my decision: {"thought": "x", "action": "final"}')
