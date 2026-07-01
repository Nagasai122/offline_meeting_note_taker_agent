from __future__ import annotations

import pytest

from mcp_server.state import (
    InvalidTransitionError,
    State,
    create_session,
    list_session_ids,
    load_session_state,
    transition,
)


def _lock(tmp_path):
    return tmp_path / ".lock", 1.0


def test_create_then_load_roundtrip(tmp_path):
    lock_path, timeout = _lock(tmp_path)
    state_dir = tmp_path / "state"
    create_session(state_dir, "s1", lock_path, timeout, source="microphone")

    loaded = load_session_state(state_dir, "s1")
    assert loaded.state == State.RECORDING
    assert loaded.metadata["source"] == "microphone"
    assert loaded.history[0]["state"] == "RECORDING"


def test_create_twice_raises(tmp_path):
    lock_path, timeout = _lock(tmp_path)
    state_dir = tmp_path / "state"
    create_session(state_dir, "s1", lock_path, timeout)
    with pytest.raises(FileExistsError):
        create_session(state_dir, "s1", lock_path, timeout)


def test_valid_transition_chain(tmp_path):
    lock_path, timeout = _lock(tmp_path)
    state_dir = tmp_path / "state"
    create_session(state_dir, "s1", lock_path, timeout)

    for target in (State.STOPPED, State.TRANSCRIBED, State.EXTRACTED, State.PROPOSED, State.REVIEWED, State.APPLIED):
        session = transition(state_dir, "s1", target, lock_path, timeout)
        assert session.state == target

    final = load_session_state(state_dir, "s1")
    assert [h["state"] for h in final.history] == [
        "RECORDING", "STOPPED", "TRANSCRIBED", "EXTRACTED", "PROPOSED", "REVIEWED", "APPLIED",
    ]


def test_invalid_transition_raises_and_does_not_mutate_state(tmp_path):
    lock_path, timeout = _lock(tmp_path)
    state_dir = tmp_path / "state"
    create_session(state_dir, "s1", lock_path, timeout)

    with pytest.raises(InvalidTransitionError):
        transition(state_dir, "s1", State.EXTRACTED, lock_path, timeout)

    assert load_session_state(state_dir, "s1").state == State.RECORDING


@pytest.mark.parametrize("from_state", [State.RECORDING, State.STOPPED, State.TRANSCRIBED, State.EXTRACTED, State.PROPOSED, State.REVIEWED])
def test_failed_is_reachable_from_every_non_terminal_state(tmp_path, from_state):
    lock_path, timeout = _lock(tmp_path)
    state_dir = tmp_path / "state"
    create_session(state_dir, "s1", lock_path, timeout)

    # Walk the session up to from_state via the legitimate chain, then assert
    # FAILED is allowed. RECORDING is create_session's initial state, not a
    # transition target, so it is deliberately excluded from this list.
    chain = [State.STOPPED, State.TRANSCRIBED, State.EXTRACTED, State.PROPOSED, State.REVIEWED]
    for step in chain:
        if load_session_state(state_dir, "s1").state == from_state:
            break
        transition(state_dir, "s1", step, lock_path, timeout)

    session = transition(state_dir, "s1", State.FAILED, lock_path, timeout, error="synthetic")
    assert session.state == State.FAILED
    assert session.metadata["error"] == "synthetic"


def test_failed_and_applied_are_terminal(tmp_path):
    lock_path, timeout = _lock(tmp_path)
    state_dir = tmp_path / "state"
    create_session(state_dir, "s1", lock_path, timeout)
    transition(state_dir, "s1", State.FAILED, lock_path, timeout)
    with pytest.raises(InvalidTransitionError):
        transition(state_dir, "s1", State.STOPPED, lock_path, timeout)


def test_load_missing_session_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_session_state(tmp_path / "state", "nope")


def test_list_session_ids_empty_dir_and_missing_dir(tmp_path):
    assert list_session_ids(tmp_path / "missing") == []
    lock_path, timeout = _lock(tmp_path)
    state_dir = tmp_path / "state"
    create_session(state_dir, "b", lock_path, timeout)
    create_session(state_dir, "a", lock_path, timeout)
    assert list_session_ids(state_dir) == ["a", "b"]
