"""transcribe_meeting: wraps M2's transcribe.whisper_runner with the STOPPED ->
TRANSCRIBED (or -> FAILED) state transition."""

from __future__ import annotations

from pathlib import Path

from mcp_server import state as state_mod
from mcp_server.schemas import validate_session_id
from transcribe.whisper_runner import transcribe_meeting as _transcribe_meeting


def transcribe_meeting(
    session_id: str,
    tmp_dir: Path | str,
    meetings_dir: Path | str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
    model_size: str,
    device: str,
    compute_type: str,
    diarisation_enabled: bool = False,
) -> dict:
    validate_session_id(session_id)
    # Fix 1.1: guard against re-transcription if session has already advanced
    # past STOPPED (e.g. agent retried transcribe_meeting on an already-TRANSCRIBED session).
    try:
        current = state_mod.load_session_state(state_dir, session_id)
        if current.state != state_mod.State.STOPPED:
            return {
                "status": "skipped",
                "reason": (
                    f"Session already in state {current.state.value}; "
                    "transcription is only valid from STOPPED."
                ),
                "current_state": current.state.value,
            }
    except FileNotFoundError:
        pass  # no state file yet — proceed (create_session is in _transcribe_meeting)
    try:
        transcript_path = _transcribe_meeting(
            session_id=session_id,
            tmp_dir=Path(tmp_dir),
            meetings_dir=Path(meetings_dir),
            model_size=model_size,
            device=device,
            compute_type=compute_type,
            diarisation_enabled=diarisation_enabled,
        )
    except Exception as exc:
        state_mod.transition(
            state_dir, session_id, state_mod.State.FAILED, lock_path, lock_timeout,
            error=str(exc),
        )
        raise

    session = state_mod.transition(
        state_dir, session_id, state_mod.State.TRANSCRIBED, lock_path, lock_timeout,
        transcript_path=str(transcript_path),
    )
    return {"session_id": session_id, "state": session.state.value, "transcript_path": str(transcript_path)}
