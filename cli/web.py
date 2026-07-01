import subprocess
import os
import json
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from datetime import datetime
from sse_starlette.sse import EventSourceResponse
import threading
import time
import numpy as np
import httpx

from config.loader import load_settings
DEFAULT_SETTINGS_PATH = Path("config/settings.toml")
from cli.briefing import build_daily_briefing
from audio_capture.session_buffer import sweep_orphaned_audio
import sys

from contextlib import asynccontextmanager
import re

logger = logging.getLogger(__name__)

# Calendar sync is intentionally NOT run automatically on startup or on a
# background timer. Per docs/architecture.md, any local-integration feature
# (Outlook COM included) must be a separate, explicitly user-triggered action,
# never silently folded into another command's lifecycle -- see /api/calendar/sync
# below, which the dashboard calls only when the user presses "Sync calendar".
#
# The tmp-audio TTL sweep is a different matter: it is purely local filesystem
# hygiene (no network, no state-machine mutation, no todo.md write), so running
# it on a timer does not touch either invariant this project guards. It is
# wired here, rather than relied upon via cli/main.py's `_startup` Typer
# callback alone, because `meeting-agent web` is a long-lived process -- the
# callback fires once, at dashboard launch, and never again unless a fresh
# `record`/`process`/etc. subprocess happens to be spawned. A crashed or
# abandoned recording's WAV would then sit in tmp/ until the next meeting,
# which may be hours or days away, well past `tmp_audio_ttl_seconds`. This is
# precisely how the 140MB orphaned WAV from the usage-mining audit accumulated.
_TMP_SWEEP_INTERVAL_SECONDS = 600  # 10 minutes; well under the 1hr default TTL grace period


async def _periodic_tmp_sweep() -> None:
    while True:
        try:
            removed = sweep_orphaned_audio(
                Path(settings.paths.tmp_dir), ttl_seconds=settings.privacy.tmp_audio_ttl_seconds
            )
            for path in removed:
                logger.info("Periodic dashboard sweep removed orphaned audio: %s", path)
        except Exception as exc:  # noqa: BLE001 - a sweep failure must never kill the dashboard
            logger.warning("Periodic tmp-audio sweep failed: %s", exc)
        await asyncio.sleep(_TMP_SWEEP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sweep_task = asyncio.create_task(_periodic_tmp_sweep())
    try:
        yield
    finally:
        sweep_task.cancel()

app = FastAPI(lifespan=lifespan)
settings = load_settings(DEFAULT_SETTINGS_PATH)

static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Keep track of active recording processes.
# NOTE: these are plain module-level globals with no cross-process sharing.
# Do not run this app with uvicorn --workers > 1 (or any multi-process
# server) -- each worker would get its own independent copy and silently
# diverge (e.g. one worker thinks a session is recording, another doesn't).
recording_processes = []
active_session_id = None
processing = False
live_transcript = ""
pipeline_error = None

# Track startup time for uptime display
_server_start_time = time.monotonic()
# Track any persistently-running LLM server process (separate from pipeline runs)
_llm_server_proc: subprocess.Popen | None = None

def _probe_loopback_available() -> bool:
    """Return True if a WASAPI loopback device can be opened (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        import pyaudiowpatch as pyaudio  # type: ignore[import-not-found]
        pa = pyaudio.PyAudio()
        try:
            devices = list(pa.get_loopback_device_info_generator())
            return len(devices) > 0
        finally:
            pa.terminate()
    except Exception:
        return False


def live_transcription_worker(session_id):
    global live_transcript
    # Signal the UI immediately so the SSE panel shows something other than the
    # static "Connecting..." placeholder that is baked into the HTML.
    live_transcript = "[Loading Whisper tiny on CPU...]"
    try:
        from faster_whisper import WhisperModel
        # Always offline — the model must already be in the cache; we never
        # download at runtime.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        # Always use CPU for live preview. This keeps VRAM free for llama-server
        # (the 7B GGUF takes ~4.5GB; running Whisper tiny on the same GPU risks
        # OOM). Tiny on CPU transcribes a 5-second chunk in well under 1 second,
        # fast enough for a 3-second polling loop.
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        live_transcript = ""  # model ready; clear the loading message

        loop_wav_path = Path(settings.paths.tmp_dir) / f"{session_id}-loop.wav"
        mic_wav_path  = Path(settings.paths.tmp_dir) / f"{session_id}-mic.wav"

        last_size = 0
        while active_session_id == session_id:
            time.sleep(3)
            # Prefer loopback (captures the other party) but fall back to mic.
            wav_path = loop_wav_path if loop_wav_path.exists() else mic_wav_path
            if not wav_path.exists():
                continue

            current_size = wav_path.stat().st_size
            if current_size <= 44:  # header-only or empty WAV — nothing to transcribe yet
                continue
            if current_size > last_size + 160000:  # ~5s of audio accumulated
                last_size = current_size
                try:
                    segments, _ = model.transcribe(str(wav_path), beam_size=1, vad_filter=True)
                    text = " ".join([s.text for s in segments])
                    if text.strip():
                        live_transcript = text
                except Exception as e:
                    logger.debug("Live transcription chunk failed for %s: %s", session_id, e)
    except ImportError:
        live_transcript = "[Live transcription unavailable: faster-whisper not installed]"
        logger.warning("Live transcriber: faster-whisper not installed")
    except Exception as e:
        live_transcript = f"[Live transcription error: {e}]"
        logger.warning("Live transcriber failed for %s: %s", session_id, e)

@app.get("/", response_class=HTMLResponse)
async def read_index():
    with open(static_dir / "index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/calendar/sync")
async def sync_calendar_endpoint():
    """Explicit, user-triggered local Outlook calendar sync (COM, not network).
    Deliberately the only place this runs from inside the web dashboard --
    no startup hook, no background timer."""
    try:
        from cli.teams_sync import fetch_outlook_calendar
        calendar_cache = Path(settings.paths.data_dir) / "calendar.json"
        count = fetch_outlook_calendar(calendar_cache)
        return {"status": "synced", "count": count}
    except Exception as e:
        logger.warning("Calendar sync failed: %s", e)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=502)

@app.get("/api/briefing")
async def get_briefing():
    from fastapi.encoders import jsonable_encoder
    todo_path = Path(settings.paths.data_dir) / "todo.md"
    state_dir = Path(settings.paths.data_dir) / "state"
    briefing = build_daily_briefing(todo_path, state_dir)
    # Add our global processing state
    global processing, pipeline_error
    briefing["processing"] = processing
    briefing["recording"] = len(recording_processes) > 0
    briefing["error"] = pipeline_error
    return JSONResponse(jsonable_encoder(briefing))

class StartRecordRequest(BaseModel):
    context: str | None = None
    title: str | None = None

@app.post("/api/record/start")
async def start_recording(req: StartRecordRequest | None = None):
    global recording_processes, active_session_id
    if recording_processes and all(p.poll() is None for p in recording_processes):
        return {"status": "already recording", "session_id": active_session_id}
        
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    
    if req and req.title:
        # Slugify the title
        slug = req.title.lower()
        slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
        slug = slug[:40] # cap length
        active_session_id = f"{slug}-{timestamp}"
    else:
        active_session_id = f"meeting-{timestamp}"
    
    # Save context if provided
    if req and req.context:
        context_path = Path(settings.paths.data_dir) / "meetings" / f"{active_session_id}.context.txt"
        context_path.parent.mkdir(exist_ok=True, parents=True)
        context_path.write_text(req.context)
    
    global live_transcript
    live_transcript = ""
    threading.Thread(target=live_transcription_worker, args=(active_session_id,), daemon=True).start()

    cmd_mic = [sys.executable, "-m", "cli.main", "record", "--session-id", f"{active_session_id}-mic", "--source", "microphone"]
    p_mic = subprocess.Popen(cmd_mic)

    # Probe before spawning the loopback subprocess so we can warn the user
    # immediately if no WASAPI loopback device is available, rather than having
    # the subprocess crash silently and leave a 0-byte WAV.
    loopback_available = _probe_loopback_available()
    if loopback_available:
        cmd_loop = [sys.executable, "-m", "cli.main", "record", "--session-id", f"{active_session_id}-loop", "--source", "loopback"]
        p_loop = subprocess.Popen(cmd_loop)
        recording_processes = [p_loop, p_mic]
    else:
        logger.warning("No WASAPI loopback device found — recording microphone track only for session %s", active_session_id)
        recording_processes = [p_mic]

    return {"status": "started", "session_id": active_session_id, "loopback": loopback_available}

@app.post("/api/record/highlight")
async def log_highlight():
    global active_session_id
    if not active_session_id:
        return {"status": "not recording"}
        
    highlight_path = Path(settings.paths.data_dir) / "meetings" / f"{active_session_id}.highlights.json"
    highlights = []
    if highlight_path.exists():
        highlights = json.loads(highlight_path.read_text())
    
    highlights.append({"timestamp": datetime.now().isoformat()})
    highlight_path.write_text(json.dumps(highlights))
    return {"status": "highlight_logged"}

@app.post("/api/record/stop")
async def stop_recording(auto_accept: bool = False):
    """`auto_accept=False` (default) stops the pipeline after agent-run and
    leaves extracted items in data/pending_review/ for a human to review via
    the normal `meeting-agent review`/`apply` commands -- preserving the
    project's human-in-the-loop guarantee. Pass `auto_accept=true` only if you
    explicitly want the web dashboard to accept every extracted item with no
    review step; this is an opt-in, not the default."""
    global recording_processes, active_session_id, processing
    if not recording_processes:
        return {"status": "not recording"}

    sess_id = active_session_id

    # Signal both subprocesses to stop gracefully via sentinel files.  On Windows,
    # p.terminate() = TerminateProcess() which kills instantly before Python's
    # I/O buffer is flushed — the WAV writer's close() never runs, leaving a
    # 0-byte or header-only WAV that libav can't open.  The sentinel file lets the
    # record subprocess exit its poll loop normally so buffer.stop() / wave.close()
    # runs and all data is safely on disk.
    tmp_dir = Path(settings.paths.tmp_dir)
    for sub_id in [f"{sess_id}-mic", f"{sess_id}-loop"]:
        (tmp_dir / f"{sub_id}.stop").touch()

    # Wait up to 4 s for processes to exit cleanly; fall back to terminate()
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if all(p.poll() is not None for p in recording_processes):
            break
        time.sleep(0.1)

    for p in recording_processes:
        if p.poll() is None:
            logger.warning("Record subprocess did not exit within grace period; force-killing.")
            p.terminate()
            p.wait(timeout=2)

    recording_processes = []
    active_session_id = None
    processing = True

    # Run the pipeline synchronously but offload to a background task so we don't block the UI
    asyncio.create_task(run_pipeline(sess_id, auto_accept=auto_accept))
    return {"status": "processing_started", "session_id": sess_id, "auto_accept": auto_accept}

@app.get("/api/record/live")
async def get_live_transcript(request: Request):
    async def event_generator():
        last_sent = ""
        while True:
            if await request.is_disconnected():
                break
            if active_session_id is None:
                break
            if live_transcript != last_sent:
                last_sent = live_transcript
                yield {"data": json.dumps({"text": last_sent})}
            await asyncio.sleep(1)
            
    return EventSourceResponse(event_generator())

async def _wait_for_llm_ready(timeout_seconds: float) -> None:
    """Poll the LLM server's own health endpoint instead of guessing a fixed
    sleep -- a 30B-class model can take well over 15s to cold-load, and a
    small model is ready in a couple of seconds, so a fixed sleep was either
    too short (raw connection-refused failures downstream) or wastefully long."""
    health_url = f"http://{settings.llm.host}:{settings.llm.port}{settings.llm.health_check_path}"
    deadline = time.monotonic() + timeout_seconds
    async with httpx.AsyncClient(trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(health_url, timeout=2.0)
                if resp.status_code == 200:
                    return
            except httpx.RequestError:
                pass
            await asyncio.sleep(1)
    raise TimeoutError(f"LLM server did not become healthy within {timeout_seconds:.0f}s at {health_url}")


async def _run_subprocess(args: list[str]) -> tuple[int, str]:
    """Run a subprocess without blocking the event loop.  `subprocess.run()`
    is synchronous I/O; called directly inside a coroutine it would freeze
    every other request (SSE live-transcript, /api/briefing polls, highlight
    clicks) for the full duration of the child process."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, (stdout or b"").decode(errors="replace")


async def run_pipeline(session_id: str, auto_accept: bool = False):
    global processing, pipeline_error
    pipeline_error = None
    """Orchestrate serve -> process -> agent-run -> (optional) auto-apply -> shutdown serve"""
    # Check if an LLM server is already listening on the configured port.
    # If so, reuse it (and don't shut it down when we're done -- we didn't start it).
    # This prevents zombie accumulation when the user pre-warms the LLM via System tab
    # or when a previous pipeline run left a process behind.
    llm_port = settings.llm.port
    llm_already_running = False
    try:
        import socket as _socket
        with _socket.create_connection(("127.0.0.1", llm_port), timeout=0.5):
            llm_already_running = True
    except OSError:
        pass

    serve_proc = None
    if llm_already_running:
        logger.info("[%s] LLM server already running on port %s — reusing.", session_id, llm_port)
    else:
        logger.info("[%s] Starting LLM Server...", session_id)
        serve_proc = subprocess.Popen([sys.executable, "-m", "cli.main", "serve"])

    try:
        await _wait_for_llm_ready(settings.llm.startup_timeout_seconds)

        # 2. Process (transcribe)
        logger.info("[%s] Transcribing audio...", session_id)
        code, out = await _run_subprocess([sys.executable, "-m", "cli.main", "process", "--session-id", session_id])
        if code != 0:
            raise Exception(f"Transcription failed: {out}")

        # 3. Agent Run
        logger.info("[%s] Running agent...", session_id)
        code, out = await _run_subprocess([sys.executable, "-m", "cli.main", "agent-run", "--session-id", session_id])
        if code != 0:
            raise Exception(f"Agent processing failed: {out}")

        # 4. Auto-apply -- opt-in only. By default the pipeline stops here and
        # leaves the session in PROPOSED state with its draft in
        # data/pending_review/, exactly as the CLI `agent-run` -> `review` ->
        # `apply` flow does -- the human-in-the-loop guarantee this project is
        # built around is preserved unless the caller explicitly asked to skip it.
        if auto_accept:
            logger.info("[%s] Auto-accepting all extracted items (explicit opt-in)...", session_id)
            from cli.capability import mint_capability_token
            from cli.review_apply import load_pending_items, complete_review, apply_reviewed_update, ReviewDecision
            from mcp_server.state import InvalidTransitionError
            from mcp_server.todo import TodoFileUnparsableError

            pending_dir = Path(settings.paths.data_dir) / "pending_review"
            draft = pending_dir / f"{session_id}.md"
            if draft.exists():
                pending_items = load_pending_items(draft)
                decisions = [
                    ReviewDecision(
                        id=item.id, decision="accept", description=item.description,
                        owner=item.owner, due_date=item.due_date, session_id=item.session_id,
                    )
                    for item in pending_items
                ]
                if decisions:
                    state_dir = Path(settings.paths.data_dir) / "state"
                    try:
                        complete_review(
                            session_id, decisions, pending_dir, state_dir,
                            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
                        )
                    except InvalidTransitionError as exc:
                        raise Exception(f"Auto-accept could not transition session state: {exc}")

                    # In-process, exactly as /api/review/apply does -- avoids a
                    # subprocess picking up a different venv/cwd and silently
                    # reading a different settings.toml (a real bug reported
                    # against the previous subprocess-based apply here).
                    token = mint_capability_token()
                    todo_path = Path(settings.paths.data_dir) / "todo.md"
                    data_dir = Path(settings.paths.data_dir)
                    try:
                        apply_reviewed_update(
                            token, session_id, pending_dir, todo_path, data_dir, state_dir,
                            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
                        )
                    except (InvalidTransitionError, TodoFileUnparsableError) as exc:
                        raise Exception(f"Apply failed: {exc}")
        else:
            logger.info("[%s] Extraction complete -- items awaiting human review in data/pending_review/.", session_id)
    except Exception as e:
        logger.error("[%s] Error in pipeline: %s", session_id, e)
        pipeline_error = str(e)
    finally:
        if serve_proc is not None:
            # Only stop the server if we started it; if it was already running
            # we leave it alone so the user's pre-warmed instance stays up.
            logger.info("[%s] Stopping LLM Server (started by this pipeline run)...", session_id)
            serve_proc.terminate()
            try:
                serve_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
        else:
            logger.info("[%s] LLM server was pre-existing — left running.", session_id)
        processing = False
        logger.info("[%s] Pipeline complete.", session_id)

# ── Review / Apply endpoints ──────────────────────────────────────────────────
# All three live in cli/ (the trusted surface), never in agent/ or mcp_server/.
# /api/review/apply mints a CapabilityToken here — see cli/capability.py's
# module docstring for why this call site is safe and listed there explicitly.

@app.get("/api/review/pending")
async def get_review_pending():
    """Return pending items for every session currently awaiting review (PROPOSED).

    For each session in pipeline_status()'s awaiting_review list, loads the
    draft written by propose_todo_update and returns its parsed TodoItems so
    the dashboard can render editable per-item fields without re-implementing
    the pending_review/ file format."""
    from cli.briefing import pipeline_status
    from cli.review_apply import load_pending_items
    from datetime import date
    from fastapi.encoders import jsonable_encoder

    state_dir = Path(settings.paths.data_dir) / "state"
    pending_review_dir = Path(settings.paths.data_dir) / "pending_review"
    status = pipeline_status(state_dir, date.today())

    result = []
    for session_id in status["awaiting_review"]:
        draft_path = pending_review_dir / f"{session_id}.md"
        try:
            items = load_pending_items(draft_path)
        except FileNotFoundError:
            items = []
        result.append({
            "session_id": session_id,
            "items": [
                {
                    "id": item.id,
                    "description": item.description,
                    "owner": item.owner,
                    "due_date": item.due_date,
                }
                for item in items
            ],
        })

    # Also include sessions awaiting apply so the UI knows what's in each bucket
    awaiting_apply = status["awaiting_apply"]
    return JSONResponse(jsonable_encoder({
        "awaiting_review": result,
        "awaiting_apply": awaiting_apply,
    }))


class ReviewItemDecision(BaseModel):
    id: str
    decision: str          # "accept" | "reject"
    description: str
    owner: str | None = None
    due_date: str | None = None


class ReviewDecideRequest(BaseModel):
    session_id: str
    decisions: list[ReviewItemDecision]


@app.post("/api/review/decide")
async def post_review_decide(req: ReviewDecideRequest):
    """Accept/reject (with optional edits) each proposed item for one session.

    Calls complete_review() exactly as cli/main.py's `review` command does —
    no transition logic is reimplemented here."""
    from cli.review_apply import ReviewDecision, complete_review
    from mcp_server.state import InvalidTransitionError

    state_dir = Path(settings.paths.data_dir) / "state"
    pending_review_dir = Path(settings.paths.data_dir) / "pending_review"

    decisions = [
        ReviewDecision(
            id=d.id,
            decision=d.decision,
            description=d.description,
            owner=d.owner,
            due_date=d.due_date,
            session_id=req.session_id,
        )
        for d in req.decisions
    ]

    try:
        result = complete_review(
            req.session_id,
            decisions,
            pending_review_dir,
            state_dir,
            settings.concurrency.lock_path,
            settings.concurrency.lock_timeout_seconds,
        )
    except InvalidTransitionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    return JSONResponse({
        "session_id": req.session_id,
        "state": result["state"],
        "accepted_count": result["accepted_count"],
        "rejected_count": result["rejected_count"],
    })


class ReviewApplyRequest(BaseModel):
    session_id: str


@app.post("/api/review/apply")
async def post_review_apply(req: ReviewApplyRequest):
    """Apply a reviewed session's accepted items to data/todo.md.

    Mints a CapabilityToken here (cli/ trusted surface — see cli/capability.py
    docstring) and calls apply_reviewed_update() exactly as cli/main.py's
    `apply` command does. Conflicts are surfaced in the response, not swallowed."""
    from cli.capability import mint_capability_token
    from cli.review_apply import apply_reviewed_update
    from mcp_server.state import InvalidTransitionError
    from mcp_server.todo import TodoFileUnparsableError

    state_dir = Path(settings.paths.data_dir) / "state"
    pending_review_dir = Path(settings.paths.data_dir) / "pending_review"
    todo_path = Path(settings.paths.data_dir) / "todo.md"
    data_dir = Path(settings.paths.data_dir)

    token = mint_capability_token()
    try:
        result = apply_reviewed_update(
            token,
            req.session_id,
            pending_review_dir,
            todo_path,
            data_dir,
            state_dir,
            settings.concurrency.lock_path,
            settings.concurrency.lock_timeout_seconds,
        )
    except InvalidTransitionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except FileNotFoundError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except TodoFileUnparsableError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

    return JSONResponse({
        "session_id": req.session_id,
        "state": result["state"],
        "applied_count": result["applied_count"],
        "conflicts": result["conflicts"],
    })


# ── Semantic search (BM25) ────────────────────────────────────────────────────

@app.get("/api/search")
async def search_meetings_endpoint(q: str = ""):
    """BM25 keyword search over data/meetings/*.summary.md and *.md.

    The corpus is cached (see cli/search.py's mtime-based invalidation) so
    repeated searches don't re-read every file under meetings_dir.
    No network calls: rank_bm25 is a pure-Python library."""
    from cli.search import search_meetings
    from fastapi.encoders import jsonable_encoder

    if not q.strip():
        return JSONResponse({"results": []})

    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    results = search_meetings(meetings_dir, q.strip())
    return JSONResponse(jsonable_encoder({
        "results": [
            {
                "session_id": r.session_id,
                "score": r.score,
                "snippet": r.snippet,
                "source": r.source,
            }
            for r in results
        ]
    }))


# ── Per-meeting detail view ───────────────────────────────────────────────────

@app.get("/api/meetings/{session_id}")
async def get_meeting_detail(session_id: str):
    """Return transcript, summary, actions, and highlights for a single session.

    Path traversal is blocked by validate_session_id() before any filesystem
    access.  404 if the session transcript does not exist."""
    from mcp_server.schemas import validate_session_id
    from fastapi import HTTPException

    try:
        validate_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    transcript_path = meetings_dir / f"{session_id}.md"
    if not transcript_path.exists():
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    def read_opt(path: Path):
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None

    def read_json_opt(path: Path):
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

    return JSONResponse({
        "session_id": session_id,
        "transcript": read_opt(transcript_path),
        "summary": read_opt(meetings_dir / f"{session_id}.summary.md"),
        "actions": read_json_opt(meetings_dir / f"{session_id}.actions.json"),
        "highlights": read_json_opt(meetings_dir / f"{session_id}.highlights.json"),
    })


# ── Server control ────────────────────────────────────────────────────────────

@app.get("/api/server/status")
async def server_status():
    """Return uptime, LLM server state, recording state, and processing state."""
    global _llm_server_proc
    uptime_seconds = int(time.monotonic() - _server_start_time)
    h, r = divmod(uptime_seconds, 3600)
    m, s = divmod(r, 60)
    uptime_str = f"{h:02d}:{m:02d}:{s:02d}"

    # Check the persistent LLM server managed by this endpoint
    llm_running = _llm_server_proc is not None and _llm_server_proc.poll() is None
    llm_pid = _llm_server_proc.pid if llm_running else None

    # Also check if the pipeline started a separate llm server on the configured port
    llm_port = settings.llm.port
    port_listening = False
    try:
        import socket
        with socket.create_connection(("127.0.0.1", llm_port), timeout=0.3):
            port_listening = True
    except OSError:
        pass

    return JSONResponse({
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
        "web_pid": os.getpid(),
        "llm_running": llm_running or port_listening,
        "llm_pid": llm_pid,
        "llm_port": llm_port,
        "recording": bool(recording_processes and any(p.poll() is None for p in recording_processes)),
        "processing": processing,
        "active_session": active_session_id,
    })


@app.post("/api/server/llm/start")
async def llm_start():
    """Start the LLM server as a background process managed by the dashboard."""
    global _llm_server_proc
    if _llm_server_proc is not None and _llm_server_proc.poll() is None:
        return JSONResponse({"status": "already_running", "pid": _llm_server_proc.pid})
    _llm_server_proc = subprocess.Popen(
        [sys.executable, "-m", "cli.main", "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return JSONResponse({"status": "started", "pid": _llm_server_proc.pid})


@app.post("/api/server/llm/stop")
async def llm_stop():
    """Kill all llama-server processes: the tracked one AND any orphan on the LLM port."""
    global _llm_server_proc
    killed = []

    # Kill the process we track explicitly
    if _llm_server_proc is not None and _llm_server_proc.poll() is None:
        _llm_server_proc.terminate()
        try:
            _llm_server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _llm_server_proc.kill()
        killed.append(_llm_server_proc.pid)
    _llm_server_proc = None

    # Also kill any orphaned llama-server processes (from pipeline runs or prior sessions)
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name"]):
            if proc.info["name"] and "llama-server" in proc.info["name"].lower():
                if proc.pid not in killed:
                    try:
                        proc.terminate()
                        proc.wait(timeout=3)
                        killed.append(proc.pid)
                    except (psutil.NoSuchProcess, psutil.TimeoutExpired, psutil.AccessDenied):
                        pass
    except ImportError:
        pass  # psutil not installed; only managed process was killed

    if killed:
        return JSONResponse({"status": "stopped", "killed_pids": killed})
    return JSONResponse({"status": "not_running"})


@app.post("/api/server/restart")
async def restart_web_server():
    """Restart the web dashboard process.  Sends response first, then execs a
    fresh Python process on the same argv after a 1-second delay."""
    def _do_restart():
        time.sleep(1.0)
        os.execv(sys.executable, [sys.executable, "-m", "cli.main", "web"])

    t = threading.Thread(target=_do_restart, daemon=False)
    t.start()
    return JSONResponse({"status": "restarting", "delay_seconds": 1})


@app.post("/api/data/reset")
async def reset_all_data():
    """Delete all meeting records, session state, pending reviews, and empty
    todo.md.  Intended for clearing test/demo data; irreversible."""
    import shutil
    data_dir = Path(settings.paths.data_dir)
    cleared = {}
    for subdir in ["meetings", "state", "pending_review"]:
        d = data_dir / subdir
        if d.exists():
            count = sum(1 for _ in d.iterdir())
            for f in d.iterdir():
                f.unlink(missing_ok=True)
            cleared[subdir] = count
        else:
            cleared[subdir] = 0
    todo_path = data_dir / "todo.md"
    if todo_path.exists():
        todo_path.write_text("")
        cleared["todo"] = "cleared"
    return JSONResponse({"status": "ok", "cleared": cleared})


# ── Todo complete ──────────────────────────────────────────────────────────────

class CompleteRequest(BaseModel):
    task_id: str

@app.post("/api/todo/complete")
async def complete_task(req: CompleteRequest):
    from mcp_server.todo import parse_todo, format_todo_file, TodoFileUnparsableError

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    if not todo_path.exists():
        return {"status": "error", "detail": "todo.md does not exist"}

    try:
        todo_file = parse_todo(todo_path)
    except TodoFileUnparsableError as exc:
        # Surfaced explicitly rather than silently rewriting an unparsable file
        # with naive string matching, which risks corrupting it further.
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=409)

    found = False
    for item in todo_file.items:
        if item.id == req.task_id:
            item.done = True
            found = True

    if found:
        todo_path.write_text(format_todo_file(todo_file))
    return {"status": "success" if found else "not_found"}
