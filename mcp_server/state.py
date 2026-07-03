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
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from concurrency.atomic import atomic_write_text
from concurrency.lock import FileLock

logger = logging.getLogger(__name__)


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
    payload = {
        "session_id": session.session_id,
        "state": session.state.value,
        "history": session.history,
        "metadata": session.metadata,
    }
    atomic_write_text(path, json.dumps(payload, indent=2))


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
    raw = json.loads(path.read_text(encoding="utf-8"))
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


def update_metadata(
    state_dir: Path | str,
    session_id: str,
    lock_path: Path | str,
    lock_timeout: float,
    **metadata_updates: object,
) -> SessionState:
    """Merge `metadata_updates` into a session's metadata without changing its
    state or appending a history entry.

    Exists for best-effort enrichment steps (calendar/mail/doc-context
    matching) that need to attach metadata to a session that is not itself a
    state-machine transition -- `transition()` requires `new_state` to be a
    genuinely allowed edge from the current state, so a same-state metadata
    update would otherwise need to (incorrectly) claim a real transition just
    to get metadata written. This is the one other controlled write path onto
    a session's state file, still taken under the same FileLock as
    `transition()` -- per the "state writes only through mcp_server.state"
    invariant, this is not a bypass of it, it's part of it.
    """
    with FileLock(lock_path, timeout_seconds=lock_timeout):
        session = load_session_state(state_dir, session_id)
        session.metadata.update(metadata_updates)
        _write(_state_path(state_dir, session_id), session)
    return session


def list_session_ids(state_dir: Path | str) -> list[str]:
    state_dir = Path(state_dir)
    if not state_dir.exists():
        return []
    return sorted(p.stem for p in state_dir.glob("*.json"))


def _pid_is_alive(pid: int) -> bool:
    # Used only by reap_orphaned_recordings below, to decide whether a session
    # stuck in RECORDING belongs to a process that has actually died -- this
    # is unrelated to lock staleness now (concurrency.lock's FileLock is
    # portalocker-backed and has no PID-file staleness heuristic of its own
    # to duplicate; a crashed lock-holder's OS-level lock is released
    # automatically when its handle closes, no PID check needed there).
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
        try:
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
        except InvalidTransitionError:
            # Benign race: the session legitimately finished (RECORDING -> STOPPED)
            # between our read of its state above and this transition() call --
            # not a bug, just means the reaper's snapshot was one step stale.
            # transition() itself is still the sole authority on what's allowed;
            # this is just declining to treat its rejection as reaper failure.
            logger.debug(
                "Session %s finished legitimately before the reaper could mark "
                "it FAILED; skipping.", session_id,
            )
            continue
        reaped.append(session_id)
    return reaped
