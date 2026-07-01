"""
Per-session state machine, persisted as one JSON file per session under
`data/state/<session_id>.json` (plain-file storage, consistent with the rest
of this project -- no database).

The transition graph below is the executable form of the state machine in
docs/architecture.md. `transition()` is the single place that enforces it, so
no tool wrapper can silently skip a step or invent a new edge.

Every write (create_session / transition) is taken under a FileLock keyed on
`concurrency.lock_path` from settings.toml -- this is amendment 4 from the
critique cycle, extended here to per-session state writes in addition to its
original scope of todo.md/projects writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from concurrency.lock import FileLock


class State(str, Enum):
    IDLE = "IDLE"
    RECORDING = "RECORDING"
    STOPPED = "STOPPED"
    TRANSCRIBED = "TRANSCRIBED"
    EXTRACTED = "EXTRACTED"
    PROPOSED = "PROPOSED"
    REVIEWED = "REVIEWED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


# "Any state -> FAILED" (docs/architecture.md) is expressed by adding FAILED to
# every non-terminal state's allowed set below, rather than special-casing it
# in transition(); this keeps the graph itself the single source of truth.
ALLOWED_TRANSITIONS: dict[State, set[State]] = {
    State.IDLE: {State.RECORDING},
    State.RECORDING: {State.STOPPED, State.FAILED},
    State.STOPPED: {State.TRANSCRIBED, State.FAILED},
    State.TRANSCRIBED: {State.EXTRACTED, State.FAILED},
    State.EXTRACTED: {State.PROPOSED, State.FAILED},
    State.PROPOSED: {State.REVIEWED, State.FAILED},
    State.REVIEWED: {State.APPLIED, State.FAILED},
    State.APPLIED: set(),  # terminal, archived
    State.FAILED: set(),  # terminal for this session_id; retry uses a fresh id
}


class InvalidTransitionError(RuntimeError):
    """Raised when a transition is not permitted by ALLOWED_TRANSITIONS."""


@dataclass
class SessionState:
    session_id: str
    state: State
    history: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def _state_path(state_dir: Path | str, session_id: str) -> Path:
    return Path(state_dir) / f"{session_id}.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, session: SessionState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session.session_id,
        "state": session.state.value,
        "history": session.history,
        "metadata": session.metadata,
    }
    path.write_text(json.dumps(payload, indent=2))


def create_session(
    state_dir: Path | str,
    session_id: str,
    lock_path: Path | str,
    lock_timeout: float,
    initial_state: State = State.RECORDING,
    **metadata: object,
) -> SessionState:
    path = _state_path(state_dir, session_id)
    with FileLock(lock_path, timeout_seconds=lock_timeout):
        if path.exists():
            raise FileExistsError(f"Session '{session_id}' already exists at {path}.")
        session = SessionState(
            session_id=session_id,
            state=initial_state,
            history=[{"state": initial_state.value, "at": _now()}],
            metadata=dict(metadata),
        )
        _write(path, session)
    return session


def load_session_state(state_dir: Path | str, session_id: str) -> SessionState:
    path = _state_path(state_dir, session_id)
    if not path.exists():
        raise FileNotFoundError(f"No session state found for '{session_id}' at {path}.")
    raw = json.loads(path.read_text())
    return SessionState(
        session_id=raw["session_id"],
        state=State(raw["state"]),
        history=raw.get("history", []),
        metadata=raw.get("metadata", {}),
    )


def transition(
    state_dir: Path | str,
    session_id: str,
    new_state: State,
    lock_path: Path | str,
    lock_timeout: float,
    **metadata_updates: object,
) -> SessionState:
    with FileLock(lock_path, timeout_seconds=lock_timeout):
        session = load_session_state(state_dir, session_id)
        allowed = ALLOWED_TRANSITIONS.get(session.state, set())
        if new_state not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition session '{session_id}' from {session.state.value} "
                f"to {new_state.value}. Allowed: {sorted(s.value for s in allowed)}"
            )
        session.state = new_state
        session.metadata.update(metadata_updates)
        session.history.append({"state": new_state.value, "at": _now()})
        _write(_state_path(state_dir, session_id), session)
    return session


def list_session_ids(state_dir: Path | str) -> list[str]:
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return []
    return sorted(p.stem for p in state_dir.glob("*.json"))


def _pid_is_alive(pid: int) -> bool:
    # Mirrors concurrency.lock._pid_is_alive -- duplicated rather than
    # imported to keep this module's only dependency on concurrency.lock
    # being FileLock itself, not an internal helper.
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover
        import os
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True


def reap_orphaned_recordings(
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> list[str]:
    """Find sessions stuck in RECORDING whose owning process has died (crash,
    kill -9, machine sleep/resume) and transition them to FAILED so they stop
    blocking the lock and stop showing as "in progress" forever.

    A session only ends up here if `start_meeting` wrote a `pid` into its
    metadata (recording.py does this); sessions created before that field
    existed, or without a `pid` for any other reason, are left untouched
    rather than guessed at -- a missing PID is not evidence of death, just
    evidence we didn't ask the question we should have.

    Returns the list of session_ids that were reaped.
    """
    reaped: list[str] = []
    for session_id in list_session_ids(state_dir):
        try:
            session = load_session_state(state_dir, session_id)
        except (FileNotFoundError, ValueError, KeyError):
            continue
        if session.state != State.RECORDING:
            continue
        pid = session.metadata.get("pid")
        if pid is None or _pid_is_alive(int(pid)):
            continue
        transition(
            state_dir, session_id, State.FAILED, lock_path, lock_timeout,
            error="ORPHANED_RECORDING",
            error_detail=(
                f"Session was left in RECORDING by pid={pid}, which is no "
                "longer running (crash, kill, or unclean shutdown). Reaped "
                "automatically; the partial audio under tmp/, if any, is "
                "left in place for manual recovery -- see docs/runbook.md."
            ),
        )
        reaped.append(session_id)
    return reaped
