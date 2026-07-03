"""
CLI entry point. The boundary statement for the data-egress guarantee lives here,
visibly: `setup` is the ONLY subcommand permitted to touch the network. Every other
subcommand (`serve`, `record`, `process`, `mcp-serve`, `agent-run`, `review`, `apply`,
`briefing`) must work with the network adapter disabled once `setup` has completed.
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path

import typer

from audio_capture.device_probe import format_devices, list_all_devices
from audio_capture.session_buffer import SessionBuffer, sweep_orphaned_audio
from audio_capture.sources import SourceKind, get_source
from config.loader import load_settings
from llm.model_profiles import get_profile
from llm.server_manager import start_server
from transcribe.whisper_runner import transcribe_meeting

app = typer.Typer(help="Offline personal meeting agent.")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("meeting-agent")

DEFAULT_SETTINGS_PATH = Path("config/settings.toml")


@app.callback()
def _startup(settings_path: Path = DEFAULT_SETTINGS_PATH) -> None:
    """
    Runs before every subcommand. Unconditionally sweeps tmp/ for orphaned audio
    left behind by a crash between RECORDING and TRANSCRIBED -- this is the binding
    amendment closing the crash-orphan gap (docs/architecture.md, amendment 1). It
    runs regardless of which command is invoked, not only on `record`.
    """
    if not settings_path.exists():
        return
    settings = load_settings(settings_path)
    removed = sweep_orphaned_audio(
        Path(settings.paths.tmp_dir), ttl_seconds=settings.privacy.tmp_audio_ttl_seconds
    )
    for path in removed:
        logger.info("Startup sweep removed orphaned audio: %s", path)


@app.command()
def setup(
    profile: str = typer.Option(..., help="Model profile name from llm/model_profiles.py"),
    models_dir: Path = typer.Option(Path("models"), help="Local directory to cache weights in"),
    skip_whisper: bool = typer.Option(False, help="Skip pre-fetching the configured Whisper model"),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Download a model profile's weights for fully offline use afterwards.

    This is the ONLY command in this CLI that is permitted to make network requests.
    Every other command must function correctly with the network adapter disabled.

    Also pre-fetches the Whisper model configured in settings.toml into the local
    HF cache (unless --skip-whisper), so the first `process` run works with the
    network adapter disabled -- previously that first run silently depended on a
    warm cache (audit 2026-07, finding A-5.3).
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        typer.echo(
            "huggingface_hub is required for `setup` only (not for runtime use). "
            "Install with: pip install huggingface_hub",
            err=True,
        )
        raise typer.Exit(code=1)

    profile_obj = get_profile(profile)
    target_dir = models_dir / profile_obj.weights_path
    target_dir.mkdir(parents=True, exist_ok=True)

    repo_parts = profile_obj.weights_path.split("/")
    if len(repo_parts) > 2:
        repo_id = f"{repo_parts[0]}/{repo_parts[1]}"
        allow_patterns = [f"*{repo_parts[2]}*"]
    else:
        repo_id = profile_obj.weights_path
        allow_patterns = None

    typer.echo(f"Downloading '{profile_obj.weights_path}' to {target_dir} ...")
    typer.echo("(This is the only step in this tool that uses the network.)")
    if profile_obj.revision:
        typer.echo(f"Pinned revision: {profile_obj.revision}")
    else:
        typer.echo(
            "WARNING: this profile has no pinned revision -- downloading the "
            "current repo head. Prefer a profile with a pinned revision."
        )
    snapshot_download(
        repo_id=repo_id, local_dir=str(target_dir),
        allow_patterns=allow_patterns, revision=profile_obj.revision,
    )

    if not skip_whisper:
        whisper_model = "base"
        if settings_path.exists():
            whisper_model = load_settings(settings_path).whisper.model
        typer.echo(f"Pre-fetching faster-whisper model '{whisper_model}' into the local cache ...")
        # Resolves the same Systran/distil-whisper repos faster-whisper would
        # otherwise fetch lazily on the first transcription (which would fail
        # offline with a cold cache).
        from faster_whisper.utils import download_model

        download_model(whisper_model)
        typer.echo(f"Whisper model '{whisper_model}' cached.")

    typer.echo("Done. You may disconnect from the network for all other commands.")


@app.command()
def serve(
    profile: str = typer.Option(None, help="Override the active_profile in settings.toml"),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Start the local LLM server and block until interrupted (Ctrl+C).

    Makes zero network requests. If weights are missing, fails with a clear pointer
    to `meeting-agent setup` rather than attempting to fetch them.
    """
    settings = load_settings(settings_path)
    profile_name = profile or settings.llm.active_profile
    profile_obj = get_profile(profile_name)
    models_dir = Path(settings.paths.models_dir)

    handle = start_server(
        profile=profile_obj,
        models_dir=models_dir,
        host=settings.llm.host,
        port=settings.llm.port,
        disable_telemetry_env=settings.privacy.disable_telemetry_env,
        health_check_path=settings.llm.health_check_path,
        startup_timeout_seconds=settings.llm.startup_timeout_seconds,
    )
    typer.echo(f"LLM server up at {handle.base_url} (pid={handle.process.pid}). Ctrl+C to stop.")

    stop_requested = {"flag": False}

    def _handle_sigint(signum, frame):
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        while handle.is_running() and not stop_requested["flag"]:
            time.sleep(0.5)
    finally:
        handle.stop()
        typer.echo("LLM server stopped.")


@app.command()
def devices() -> None:
    """List available microphone and WASAPI-loopback devices and their indices."""
    typer.echo(format_devices(list_all_devices()))


@app.command()
def record(
    session_id: str = typer.Option(..., help="Identifier for this meeting session"),
    source: SourceKind = typer.Option(SourceKind.MICROPHONE, help="microphone or loopback"),
    device_index: int = typer.Option(None, help="Device index from `meeting-agent devices`"),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Record one meeting session to a transient WAV in tmp/, until Ctrl+C.

    The WAV is NOT deleted by this command -- that happens unconditionally once
    `process` (transcription) has produced a transcript, or via the startup sweep
    above if no transcription ever runs. This command's only job is to capture
    cleanly and flag truncation honestly if the stream errors out.
    """
    settings = load_settings(settings_path)
    audio_source = get_source(source, device_index=device_index)
    buffer = SessionBuffer(Path(settings.paths.tmp_dir), session_id, audio_source)

    buffer.start()
    typer.echo(f"Recording session '{session_id}' from {source.value}. Ctrl+C to stop.")

    stop_requested = {"flag": False}

    def _handle_sigint(signum, frame):
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_sigint)
    # The web dashboard stops recording by writing a sentinel file rather than
    # sending SIGTERM/TerminateProcess — the latter kills the process before
    # Python's I/O buffer is flushed, leaving a 0-byte or header-only WAV.
    stop_file = Path(settings.paths.tmp_dir) / f"{session_id}.stop"
    try:
        while not stop_requested["flag"] and not stop_file.exists():
            time.sleep(0.1)
    finally:
        stop_file.unlink(missing_ok=True)
        result = buffer.stop()

    if result.truncated:
        typer.echo(
            f"WARNING: recording '{session_id}' was truncated after "
            f"{result.duration_seconds:.1f}s -- see {result.sidecar_path} for details.",
            err=True,
        )
    else:
        typer.echo(f"Recording saved: {result.wav_path} ({result.duration_seconds:.1f}s)")


@app.command()
def process(
    session_id: str = typer.Option(..., help="Session identifier passed to `record --session-id`"),
    diarisation: bool = typer.Option(
        None, help="Override whisper.diarisation_enabled from settings.toml for this run"
    ),
    whisper_model: str = typer.Option(
        None, help="Override whisper.model from settings.toml for this session only (e.g. base/small/large-v3)"
    ),
    skip_transcribe: bool = typer.Option(
        False, "--skip-transcribe",
        help="Skip Whisper transcription and reuse the existing data/meetings/<session_id>.md "
             "transcript. Recovery path for when transcription already succeeded but a later "
             "step (e.g. extraction) failed -- avoids re-running Whisper unnecessarily.",
    ),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Transcribe a recorded session (STOPPED -> TRANSCRIBED) and delete its audio.

    Makes zero network requests (faster-whisper runs from locally cached model
    files). The source WAV is deleted unconditionally once this command returns or
    raises -- see transcribe/whisper_runner.py for why that is not a recoverable
    step if transcription itself fails.
    """
    settings = load_settings(settings_path)
    diarisation_enabled = (
        diarisation if diarisation is not None else settings.whisper.diarisation_enabled
    )
    model_size = whisper_model or settings.whisper.model
    meetings_dir = Path(settings.paths.data_dir) / "meetings"

    if skip_transcribe:
        transcript_path = meetings_dir / f"{session_id}.md"
        if not transcript_path.exists():
            typer.echo(
                f"--skip-transcribe given but no transcript found at {transcript_path}.", err=True
            )
            raise typer.Exit(code=1)
        typer.echo(f"Skipping transcription; using existing transcript: {transcript_path}")
    else:
        tmp_dir = Path(settings.paths.tmp_dir)
        loop_wav = tmp_dir / f"{session_id}-loop.wav"

        if loop_wav.exists():
            # Dual-track recording detected
            from transcribe.whisper_runner import transcribe_dual_track
            transcript_path = transcribe_dual_track(
                session_id=session_id,
                tmp_dir=tmp_dir,
                meetings_dir=meetings_dir,
                model_size=model_size,
                device=settings.whisper.device,
                compute_type=settings.whisper.compute_type,
            )
        else:
            # Legacy single-track recording
            transcript_path = transcribe_meeting(
                session_id=session_id,
                tmp_dir=tmp_dir,
                meetings_dir=meetings_dir,
                model_size=model_size,
                device=settings.whisper.device,
                compute_type=settings.whisper.compute_type,
                diarisation_enabled=diarisation_enabled,
            )
        typer.echo(f"Transcript written: {transcript_path}")
        typer.echo("Source audio deleted.")

    # Advance the state
    from mcp_server import state as state_mod
    from mcp_server.meeting_type import load_meeting_type, type_file_path
    state_dir = Path(settings.paths.data_dir) / "state"
    lock_path = Path(settings.concurrency.lock_path)
    lock_timeout = settings.concurrency.lock_timeout_seconds
    state_dir.mkdir(parents=True, exist_ok=True)

    detected_type = load_meeting_type(type_file_path(meetings_dir, session_id))
    try:
        existing = state_mod.load_session_state(state_dir, session_id)
    except FileNotFoundError:
        existing = None
        state_mod.create_session(
            state_dir, session_id, lock_path, lock_timeout, initial_state=state_mod.State.STOPPED,
            meeting_type=detected_type.value,
        )

    if existing is not None and existing.state == state_mod.State.TRANSCRIBED:
        # Already at TRANSCRIBED (the exact --skip-transcribe recovery case) --
        # TRANSCRIBED -> TRANSCRIBED is not a valid transition() edge, and there
        # is nothing new to record, so this is a deliberate no-op rather than an
        # error.
        typer.echo(f"Session '{session_id}' is already TRANSCRIBED; nothing to advance.")
    else:
        state_mod.transition(
            state_dir, session_id, state_mod.State.TRANSCRIBED, lock_path, lock_timeout,
            transcript_path=str(transcript_path), whisper_model=model_size,
        )

@app.command(name="mcp-serve")
def mcp_serve(settings_path: Path = DEFAULT_SETTINGS_PATH) -> None:
    """
    Launch the agent-facing MCP tool server (M4) over stdio. Intended to be
    launched by the agent loop (M5), not run interactively by a human.

    Exposes exactly the 8 read/write tools documented in mcp_server/server.py.
    `apply_reviewed_update` is never registered here -- it is a CLI-only
    command (M6), gated by a local capability token, per critique amendment 2.
    """
    from mcp_server.server import main as mcp_main

    mcp_main(settings_path)


@app.command(name="agent-run")
def agent_run(
    session_id: str = typer.Option(..., help="Session identifier to drive forward"),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Run the agent orchestration loop (M5) for one session, until it reaches
    PROPOSED, FAILED, or the configured max_iterations ceiling.

    Launches `mcp-serve` itself as a subprocess over stdio -- you do not need
    to run it separately. The local LLM server (`meeting-agent serve`) must
    already be running and reachable at the configured host:port.

    Makes zero network requests beyond loopback to that already-running LLM
    server. Writes only: a JSONL trace under data/traces/, and (via the MCP
    tools it calls) a draft under data/pending_review/ -- never data/todo.md.
    """
    import asyncio
    import os

    from agent.loop import AgentLoop, MaxIterationsExceededError
    from agent.mcp_client import AgentMCPClient
    from llm.client import HttpLLMClient

    settings = load_settings(settings_path)
    llm_client = HttpLLMClient(base_url=f"http://{settings.llm.host}:{settings.llm.port}")

    # Fix 1.3: if the pipeline orchestrator already ran transcription before
    # spawning this subprocess, it sets MA_TRANSCRIPTION_DONE=1.  Hide
    # transcribe_meeting from the agent's tool catalogue so it cannot call it
    # even if the system-prompt dispatch table or the tool-level state guard
    # were somehow bypassed (defence-in-depth, three layers total).
    _filter: frozenset[str] = frozenset()
    if os.environ.get("MA_TRANSCRIPTION_DONE") == "1":
        _filter = frozenset({"transcribe_meeting"})
        logger.debug("MA_TRANSCRIPTION_DONE set — transcribe_meeting hidden from agent tool list")

    async def _main() -> None:
        async with AgentMCPClient(settings_path) as mcp_client:
            loop = AgentLoop(
                llm_client=llm_client,
                mcp_client=mcp_client,
                trace_dir=settings.agent.trace_dir,
                max_iterations=settings.agent.max_iterations,
                filter_tools=_filter,
            )
            try:
                result = await loop.run(session_id)
            except MaxIterationsExceededError as exc:
                typer.echo(f"Run did not converge: {exc}", err=True)
                raise typer.Exit(code=1)

        typer.echo(f"Run {result.run_id} finished with outcome={result.outcome}")
        if result.summary:
            typer.echo(result.summary)
        if result.outcome == "session_failed":
            raise typer.Exit(code=1)

    asyncio.run(_main())


@app.command()
def review(
    session_id: str = typer.Option(..., help="Session identifier currently in PROPOSED state"),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Interactively accept, reject, or edit each proposed action item for one
    session (PROPOSED -> REVIEWED). The only human-in-the-loop step in the
    pipeline -- nothing reaches data/todo.md without passing through this.

    Writes data/pending_review/<session_id>.reviewed.json. Does NOT touch
    data/todo.md -- that only happens via `apply`, after this command's
    decisions are recorded.
    """
    from cli.review_apply import ReviewDecision, complete_review, load_pending_items

    settings = load_settings(settings_path)
    pending_review_dir = Path(settings.paths.data_dir) / "pending_review"
    state_dir = Path(settings.paths.data_dir) / "state"
    draft_path = pending_review_dir / f"{session_id}.md"

    items = load_pending_items(draft_path)
    if not items:
        typer.echo(f"No proposed items found in {draft_path}.")
        raise typer.Exit(code=1)

    typer.echo(f"Reviewing {len(items)} proposed item(s) for session '{session_id}':\n")
    decisions: list[ReviewDecision] = []
    for item in items:
        typer.echo(f"  - {item.description}  (owner={item.owner!r}, due={item.due_date!r}, priority={item.priority!r})")
        if item.evidence:
            typer.echo(f"      \"{item.evidence}\"")
        accept = typer.confirm("    Accept this item?", default=True)
        if not accept:
            decisions.append(
                ReviewDecision(
                    id=item.id, decision="reject", description=item.description,
                    owner=item.owner, due_date=item.due_date, session_id=item.session_id,
                    priority=item.priority, evidence=item.evidence,
                )
            )
            continue
        edit = typer.confirm("    Edit description/owner/due_date before accepting?", default=False)
        description, owner, due_date = item.description, item.owner, item.due_date
        if edit:
            description = typer.prompt("    description", default=description)
            owner = typer.prompt("    owner", default=owner or "")
            owner = owner or None
            due_date = typer.prompt("    due_date", default=due_date or "")
            due_date = due_date or None
        decisions.append(
            ReviewDecision(
                id=item.id, decision="accept", description=description,
                owner=owner, due_date=due_date, session_id=item.session_id,
                priority=item.priority, evidence=item.evidence,
            )
        )

    result = complete_review(
        session_id, decisions, pending_review_dir, state_dir,
        settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
    )
    typer.echo(
        f"\nReview recorded: {result['accepted_count']} accepted, "
        f"{result['rejected_count']} rejected. State -> {result['state']}."
    )
    typer.echo(f"Run `meeting-agent apply --session-id {session_id}` next to write data/todo.md.")


@app.command()
def apply(
    session_id: str = typer.Option(..., help="Session identifier currently in REVIEWED state"),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Apply a reviewed session's accepted items to data/todo.md (REVIEWED -> APPLIED).

    The only command permitted to write data/todo.md, enforced both by a
    capability token minted here (never exposed to the agent loop) and by
    apply_reviewed_update's structural absence from mcp_server/ -- critique
    amendment 2. Commits data/ via git before and after the write (amendment 5),
    giving a `git revert`/`git log` undo path independent of this tool.

    A conflicting item (PARTIAL_APPLY_CONFLICT: its id already present in
    todo.md) is skipped, not fatal -- the rest of the apply proceeds, and both
    versions are printed for manual reconciliation (amendment 3).
    """
    from cli.capability import mint_capability_token
    from cli.review_apply import apply_reviewed_update
    from mcp_server.state import InvalidTransitionError
    from mcp_server.todo import TodoFileUnparsableError

    settings = load_settings(settings_path)
    pending_review_dir = Path(settings.paths.data_dir) / "pending_review"
    todo_path = Path(settings.paths.data_dir) / "todo.md"
    state_dir = Path(settings.paths.data_dir) / "state"
    data_dir = Path(settings.paths.data_dir)

    token = mint_capability_token()
    try:
        result = apply_reviewed_update(
            token, session_id, pending_review_dir, todo_path, data_dir, state_dir,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
        )
    except InvalidTransitionError as exc:
        # Found via manual stress testing (re-applying an already-APPLIED
        # session): surfaced here as a clean, expected outcome rather than a
        # raw traceback -- the session is most likely already done.
        typer.echo(f"Cannot apply session '{session_id}': {exc}", err=True)
        raise typer.Exit(code=1)
    except FileNotFoundError as exc:
        typer.echo(f"Cannot apply session '{session_id}': {exc}", err=True)
        raise typer.Exit(code=1)
    except TodoFileUnparsableError as exc:
        typer.echo(
            f"data/todo.md is unparsable -- session '{session_id}' has been marked "
            f"FAILED, not silently retried. Fix the file by hand, then re-run apply "
            f"once it is recreated for this session: {exc}",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Applied {result['applied_count']} item(s) to {todo_path}. State -> {result['state']}.")
    if result["conflicts"]:
        typer.echo(f"\n{len(result['conflicts'])} PARTIAL_APPLY_CONFLICT item(s) were skipped:", err=True)
        for conflict in result["conflicts"]:
            typer.echo(f"  id={conflict['id']}", err=True)
            typer.echo(f"    existing: {conflict['existing']}", err=True)
            typer.echo(f"    incoming: {conflict['incoming']}", err=True)
        typer.echo("Reconcile these by hand in data/todo.md.", err=True)
        raise typer.Exit(code=1)


@app.command()
def briefing(
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Print today's open tasks and pipeline status (data/todo.md + data/state/).

    Intended to be the first thing you run each morning. Purely read-only --
    no FileLock, no capability token, no network -- so it is always safe to
    run, regardless of what else is mid-flight. "Meetings" here means only
    what this offline system itself knows about (sessions awaiting review or
    apply, plus ones that finished today): it deliberately does NOT pull a
    live calendar, by explicit decision, to preserve the zero-egress
    guarantee that the rest of this CLI is built around.
    """
    from cli.briefing import build_daily_briefing, render_briefing

    settings = load_settings(settings_path)
    todo_path = Path(settings.paths.data_dir) / "todo.md"
    state_dir = Path(settings.paths.data_dir) / "state"

    result = build_daily_briefing(todo_path, state_dir)
    typer.echo(render_briefing(result))


@app.command(name="reap-stale")
def reap_stale(
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Find sessions stuck in RECORDING whose owning process has died, and
    transition them to FAILED via the normal state machine.

    Kept separate from `briefing` deliberately: `briefing` is documented as
    read-only with no FileLock and no side effects, and this command does
    take the lock and does write state. Run this if `briefing` or the web
    dashboard shows a session that has been "Recording live..." for far
    longer than any real meeting, which usually means a crash or a `kill -9`
    rather than an actual 3-hour meeting -- see docs/runbook.md.
    """
    from mcp_server.state import reap_orphaned_recordings

    settings = load_settings(settings_path)
    state_dir = Path(settings.paths.data_dir) / "state"
    lock_path = Path(settings.concurrency.lock_path)

    reaped = reap_orphaned_recordings(state_dir, lock_path, settings.concurrency.lock_timeout_seconds)
    if reaped:
        typer.echo(f"Reaped {len(reaped)} orphaned RECORDING session(s): {', '.join(reaped)}")
    else:
        typer.echo("No orphaned RECORDING sessions found.")


@app.command()
def sync_calendar(
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Sync today's calendar from the local Outlook Desktop app to data/state/calendar.json.
    This operates completely offline via Windows COM interop.
    """
    from cli.teams_sync import fetch_outlook_calendar
    
    settings = load_settings(settings_path)
    calendar_cache = Path(settings.paths.data_dir) / "calendar.json"
    
    typer.echo("Fetching today's meetings from local Outlook...")
    count = fetch_outlook_calendar(calendar_cache)
    typer.echo(f"Successfully synced {count} meeting(s) to {calendar_cache}")


@app.command(name="import-transcript")
def import_transcript(
    session_id: str = typer.Option(..., help="Session identifier to use (must not already exist)"),
    file: Path = typer.Option(..., help="Transcript file (.json, .vtt, .srt, .txt)"),
    meeting_type: str = typer.Option(
        "project-meeting", "--type", help="One of: is-call, project-meeting, seminar, general"
    ),
    whisper_model: str = typer.Option(
        None, help="Recorded in metadata as the transcription source; defaults to 'imported'"
    ),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """
    Import an externally-produced transcript directly at TRANSCRIBED, bypassing
    RECORDING/STOPPED entirely (architecture_v2.md §8). Supports Whisper JSON,
    WebVTT, SRT, and plain text.
    """
    from mcp_server import state as state_mod
    from mcp_server.meeting_type import MeetingType, type_file_path
    from transcribe.import_parsers import parse_transcript_file
    from transcribe.postprocess import write_transcript
    from transcribe.whisper_runner import TranscriptionResult, TranscriptSegment

    if not file.exists():
        typer.echo(f"Transcript file not found: {file}", err=True)
        raise typer.Exit(code=1)
    if file.suffix.lower() not in {".json", ".vtt", ".srt", ".txt"}:
        typer.echo(f"Unsupported transcript extension: {file.suffix!r}", err=True)
        raise typer.Exit(code=1)
    try:
        MeetingType(meeting_type)
    except ValueError:
        typer.echo(f"Invalid --type {meeting_type!r}. Must be one of: is-call, project-meeting, seminar, general.", err=True)
        raise typer.Exit(code=1)

    settings = load_settings(settings_path)
    state_dir = Path(settings.paths.data_dir) / "state"
    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    lock_path = Path(settings.concurrency.lock_path)
    lock_timeout = settings.concurrency.lock_timeout_seconds
    state_dir.mkdir(parents=True, exist_ok=True)
    meetings_dir.mkdir(parents=True, exist_ok=True)

    model_used = whisper_model or "imported"

    state_mod.create_session(
        state_dir, session_id, lock_path, lock_timeout, initial_state=state_mod.State.STOPPED,
        meeting_type=meeting_type, source="import", whisper_model=model_used,
    )
    type_file_path(meetings_dir, session_id).write_text(meeting_type, encoding="utf-8")

    segments = parse_transcript_file(file)
    result = TranscriptionResult(
        session_id=session_id,
        segments=[TranscriptSegment(**seg) for seg in segments],
        language="unknown",
        duration_seconds=segments[-1]["end"] if segments else 0.0,
        model_name=model_used,
        diarised=False,
    )
    write_transcript(meetings_dir, result)

    state_mod.transition(
        state_dir, session_id, state_mod.State.TRANSCRIBED, lock_path, lock_timeout,
        transcript_path=str(meetings_dir / f"{session_id}.md"),
    )

    typer.echo(f"Import complete. Session: {session_id}")
    typer.echo(f"Run `meeting-agent agent-run --session-id {session_id}` next to extract action items.")


def _reminder_loop(data_dir: Path, todo_path: Path, interval_seconds: int = 3600) -> None:
    """Daemon-thread body (not asyncio, so it never competes with the FastAPI
    event loop): checks for due tasks immediately on startup, then hourly.
    A single check's failure is logged and the loop continues -- one bad
    todo.md parse must not silently stop reminders forever."""
    import time

    from cli.reminders import check_and_notify

    check_and_notify(data_dir, todo_path)
    while True:
        time.sleep(interval_seconds)
        try:
            check_and_notify(data_dir, todo_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reminder check failed: %s", exc)


@app.command()
def web(
    port: int = typer.Option(8000, help="Port to run the dashboard on"),
    settings_path: Path = typer.Option(DEFAULT_SETTINGS_PATH),
) -> None:
    """Launch the Web Dashboard."""
    import threading

    import uvicorn

    from cli.web import app as web_app

    settings = load_settings(settings_path)
    reminder_thread = threading.Thread(
        target=_reminder_loop,
        args=(Path(settings.paths.data_dir), Path(settings.paths.data_dir) / "todo.md"),
        daemon=True,
    )
    reminder_thread.start()

    typer.echo(f"Starting Web Dashboard on http://localhost:{port}")
    uvicorn.run(web_app, host="127.0.0.1", port=port, log_level="info")

if __name__ == "__main__":
    app()
