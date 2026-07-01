"""
start_meeting / stop_meeting.

Live `AudioSource`/`SessionBuffer` instances cannot be returned across an MCP
tool call (each call is a stateless request/response), so the server process
holds them in `_ACTIVE_BUFFERS`, keyed by session_id, for the lifetime of one
recording. This is in-memory and process-local by design: if the MCP server
process dies mid-recording, the capture thread dies with it -- the resulting
partial WAV is exactly the crash-orphan case M1's `sweep_orphaned_audio`
already exists to clean up, so no separate recovery path is needed here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from audio_capture.session_buffer import SessionBuffer
from audio_capture.sources import AudioSource, SourceKind, get_source
from mcp_server import state as state_mod
from mcp_server.schemas import validate_session_id

_ACTIVE_BUFFERS: dict[str, SessionBuffer] = {}


def start_meeting(
    session_id: str,
    source: str,
    tmp_dir: Path | str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
    device_index: int | None = None,
    source_factory: Callable[[], AudioSource] | None = None,
) -> dict:
    validate_session_id(session_id)
    if session_id in _ACTIVE_BUFFERS:
        raise RuntimeError(f"Session '{session_id}' is already recording.")

    audio_source = (source_factory or (lambda: get_source(SourceKind(source), device_index=device_index)))()
    buffer = SessionBuffer(Path(tmp_dir), session_id, audio_source)
    buffer.start()
    _ACTIVE_BUFFERS[session_id] = buffer

    try:
        state_mod.create_session(
            state_dir, session_id, lock_path, lock_timeout,
            initial_state=state_mod.State.RECORDING, source=source,
            pid=os.getpid(),
        )
    except Exception:
        # Don't leave a dangling live stream if we can't persist the state record.
        buffer.stop()
        del _ACTIVE_BUFFERS[session_id]
        raise

    return {"session_id": session_id, "state": state_mod.State.RECORDING.value}


def stop_meeting(
    session_id: str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> dict:
    validate_session_id(session_id)
    buffer = _ACTIVE_BUFFERS.pop(session_id, None)
    if buffer is None:
        raise RuntimeError(f"No active recording for session '{session_id}'.")

    result = buffer.stop()
    session = state_mod.transition(
        state_dir, session_id, state_mod.State.STOPPED, lock_path, lock_timeout,
        truncated=result.truncated, duration_seconds=result.duration_seconds,
    )
    return {
        "session_id": session_id,
        "state": session.state.value,
        "truncated": result.truncated,
        "duration_seconds": result.duration_seconds,
    }
