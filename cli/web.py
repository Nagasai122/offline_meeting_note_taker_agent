import collections
import subprocess
import os
import json
import asyncio
import logging
import wave
from pathlib import Path
from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from datetime import datetime
from sse_starlette.sse import EventSourceResponse
import threading
import time
from llm.http_probe import make_local_client, probe_ok

from config.loader import load_settings
from cli.briefing import build_daily_briefing
from audio_capture.session_buffer import sweep_orphaned_audio
import sys

from contextlib import asynccontextmanager
import re

DEFAULT_SETTINGS_PATH = Path("config/settings.toml")

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


def _origin_allowed(origin: str) -> bool:
    if origin == "null":
        # Browsers send the literal string "null" as Origin for some
        # sandboxed/local contexts (e.g. a file:// page) -- not attacker-
        # controlled in the way a real cross-origin website is, and not
        # worth rejecting for this app's threat model.
        return True
    try:
        from urllib.parse import urlparse
        hostname = urlparse(origin).hostname
    except Exception:
        return False
    return hostname in ("127.0.0.1", "localhost")


@app.middleware("http")
async def _csrf_origin_guard(request: Request, call_next):
    """Reject cross-origin state-changing requests against this otherwise
    unauthenticated localhost-only API (CSRF hardening).

    Why this exists: this server binds 127.0.0.1 only and has no auth of any
    kind -- "localhost-only" is the entire security model. Without this
    check, any webpage the user's browser has open concurrently (a malicious
    site, a compromised ad, a rogue browser extension) could trigger state-
    changing requests here with zero user interaction: plain HTML <form>
    submissions and multipart/form-data or text/plain fetch() bodies are all
    CORS-"simple" requests that never trigger a preflight, so the browser's
    same-origin policy alone does not protect a server that performs no
    origin check of its own. Manual-task CRUD in particular bypasses the
    human-review gate entirely (by design, for a different reason -- it's
    meant to write immediately), so this was a genuine path to silently
    creating/editing/deleting data/todo.md entries, or starting/stopping a
    recording, from an unrelated tab.

    Only applied to methods that mutate state -- GET/HEAD/OPTIONS are always
    allowed through. A request with NO Origin/Referer header at all is
    deliberately allowed too: modern browsers always attach an Origin header
    to cross-origin fetch()/form requests (that's the mechanism this guard
    actually relies on), so an absent header means either a same-origin
    browser request that happened to omit it, or a non-browser client (curl,
    the test suite's TestClient, a future CLI-adjacent tool) -- neither of
    which is the attack this guards against. Rejecting only when an
    Origin/Referer IS present and does NOT resolve to 127.0.0.1/localhost
    keeps that legitimate traffic working while closing the actual gap.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin and not _origin_allowed(origin):
            return JSONResponse(
                {"error": "Rejected: request did not originate from this app's own dashboard."},
                status_code=403,
            )
    return await call_next(request)

# Keep track of active recording processes.
# NOTE: these are plain module-level globals with no cross-process sharing.
# Do not run this app with uvicorn --workers > 1 (or any multi-process
# server) -- each worker would get its own independent copy and silently
# diverge (e.g. one worker thinks a session is recording, another doesn't).
recording_processes = []
active_session_id = None
processing = False
pipeline_stage: str | None = None   # None | "RECORDING" | "TRANSCRIBING" | "LLM_LOADING" | "EXTRACTING" | "AWAITING_REVIEW" | "ERROR"
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


_LIVE_TRANSCRIPTION_WINDOW_SECONDS = 30.0


def _extract_recent_audio_window(wav_path: Path, window_seconds: float, out_path: Path) -> bool:
    """Write the last `window_seconds` of `wav_path` to `out_path` as a
    smaller, same-format WAV file. Returns False (leaving out_path untouched)
    if wav_path can't be read as a WAV right now -- e.g. it's mid-write by
    the recorder; a torn read here just skips one poll cycle, not fatal.

    Bug fix (O(n^2) hot path): live_transcription_worker used to call
    model.transcribe() on the ENTIRE growing recording every poll cycle, so
    total transcription work across a meeting grew quadratically with
    elapsed time. This app explicitly supports long ("Seminar") sessions --
    for a multi-hour recording, each cycle's transcription time eventually
    exceeded the 3s poll interval, pinning a CPU core and making the live
    preview fall further and further behind real time. Bounding the input to
    only the most recent audio makes each cycle's cost constant regardless of
    total meeting length; re-encoding to a small sibling WAV (rather than
    passing raw frames directly) keeps using faster-whisper's normal
    file-based decode path unchanged, which already resamples/handles both
    the mic (16kHz mono) and loopback (48kHz stereo) source formats
    correctly -- avoids re-deriving that logic here."""
    try:
        with wave.open(str(wav_path), "rb") as src:
            n_channels = src.getnchannels()
            sampwidth = src.getsampwidth()
            framerate = src.getframerate()
            total_frames = src.getnframes()
            window_frames = int(window_seconds * framerate)
            start_frame = max(0, total_frames - window_frames)
            src.setpos(start_frame)
            frames = src.readframes(total_frames - start_frame)
    except (wave.Error, OSError, EOFError):
        return False

    if not frames:
        return False

    with wave.open(str(out_path), "wb") as dst:
        dst.setnchannels(n_channels)
        dst.setsampwidth(sampwidth)
        dst.setframerate(framerate)
        dst.writeframes(frames)
    return True


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
        # Bounded sibling file re-written each cycle -- see
        # _extract_recent_audio_window's docstring for why transcribing only
        # this window (instead of the whole growing recording) is the fix for
        # the O(n^2) hot path.
        window_path = Path(settings.paths.tmp_dir) / f"{session_id}-live-window.wav"

        last_size = 0
        while active_session_id == session_id:
            time.sleep(3)
            # Prefer loopback (captures the other party's audio in a video call)
            # but fall back to mic if loopback has no real audio content (e.g.
            # when recording ambient/phone audio where WASAPI loopback captures
            # nothing and produces a header-only WAV). Checking size > 44 (the
            # WAV header size) distinguishes a live loopback stream from an empty
            # placeholder file, preventing the worker from being stuck on a dead
            # loopback path and never reaching the mic audio.
            loop_size = loop_wav_path.stat().st_size if loop_wav_path.exists() else 0
            mic_size  = mic_wav_path.stat().st_size  if mic_wav_path.exists()  else 0
            if loop_size > 44:
                wav_path = loop_wav_path
            elif mic_size > 44:
                wav_path = mic_wav_path
            else:
                continue  # neither source has real audio yet

            current_size = wav_path.stat().st_size
            if current_size <= 44:  # header-only or empty WAV — nothing to transcribe yet
                continue
            if current_size > last_size + 160000:  # ~5s of audio accumulated
                last_size = current_size
                try:
                    if not _extract_recent_audio_window(
                        wav_path, _LIVE_TRANSCRIPTION_WINDOW_SECONDS, window_path
                    ):
                        continue
                    segments, _ = model.transcribe(str(window_path), beam_size=1, vad_filter=True)
                    text = " ".join([s.text for s in segments])
                    if text.strip():
                        live_transcript = text
                except Exception as e:
                    logger.debug("Live transcription chunk failed for %s: %s", session_id, e)
        window_path.unlink(missing_ok=True)
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

# Serializes concurrent calendar syncs. Bug fix: this diff added a second
# Sync button (Calendar tab, alongside the existing Dashboard-tab one),
# making two near-simultaneous POST /api/calendar/sync calls a realistic
# scenario for the first time. fetch_outlook_calendar (cli/teams_sync.py)
# writes calendar.json via atomic_write_text with no lock of its own, and
# atomic_write_text's temp filename is fixed (not unique per caller), so two
# concurrent writers race on the same .tmp file with silent last-writer-wins
# data loss as the best case. Same lazy-construction rationale as the other
# module-level locks in this file.
_calendar_sync_lock: asyncio.Lock | None = None


def _get_calendar_sync_lock() -> asyncio.Lock:
    global _calendar_sync_lock
    if _calendar_sync_lock is None:
        _calendar_sync_lock = asyncio.Lock()
    return _calendar_sync_lock


@app.post("/api/calendar/sync")
async def sync_calendar_endpoint():
    """Explicit, user-triggered local Outlook calendar sync (COM, not network).
    Deliberately the only place this runs from inside the web dashboard --
    no startup hook, no background timer."""
    try:
        from cli.teams_sync import fetch_outlook_calendar
        calendar_cache = Path(settings.paths.data_dir) / "calendar.json"
        # Bug fix: fetch_outlook_calendar is a fully synchronous win32com
        # call. This handler is `async def`, but previously called it
        # directly (unlike apply_reviewed_update/export_all elsewhere in this
        # file, which are correctly offloaded) -- since uvicorn runs
        # single-worker here by design, a slow/first-touch COM round-trip
        # froze the entire event loop, so the dashboard's own 5s polling and
        # every other request appeared to hang for the sync's whole duration,
        # which is most of why "click Sync" felt like it didn't do anything.
        async with _get_calendar_sync_lock():
            count = await asyncio.to_thread(fetch_outlook_calendar, calendar_cache)
        return {"status": "synced", "count": count}
    except Exception as e:
        logger.warning("Calendar sync failed: %s", e)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=502)

@app.get("/api/briefing")
async def get_briefing():
    from fastapi.encoders import jsonable_encoder
    todo_path = Path(settings.paths.data_dir) / "todo.md"
    state_dir = Path(settings.paths.data_dir) / "state"
    # Bug fix: build_daily_briefing scans every session state file and
    # parses the whole of todo.md on every call -- negligible today, but this
    # endpoint is polled every 5s for as long as the dashboard tab is open,
    # and was previously called synchronously on the event loop thread,
    # blocking all other requests (SSE live-transcript, any other tab) for
    # its duration. Offloaded the same way other blocking calls in this file
    # already are; the cost only grows with total session/todo.md history, so
    # this heads off the same class of freeze the calendar-sync fix
    # addressed, before it becomes noticeable.
    briefing = await asyncio.to_thread(build_daily_briefing, todo_path, state_dir)
    # Add our global processing state
    global processing, pipeline_error, pipeline_stage
    briefing["processing"] = processing
    briefing["pipeline_stage"] = pipeline_stage
    briefing["recording"] = len(recording_processes) > 0
    briefing["error"] = pipeline_error

    # Reuse the sessions dict pipeline_status() already computed above (via
    # build_daily_briefing) rather than re-parsing pending-review drafts here
    # -- this only needs a count, and the dashboard polls this endpoint every
    # 5s, so a second parse pass per poll would be wasted work. Lets the
    # #review-badge nav counter (static/app.js's updateUI) stay current even
    # while the user is on a different tab, instead of only refreshing when
    # loadReviewQueue() is explicitly called (tab-switch or post-decision).
    sessions_status = briefing.get("sessions") or {}
    briefing["review_pending_count"] = (
        len(sessions_status.get("awaiting_review", []))
        + len(sessions_status.get("awaiting_apply", []))
    )

    # bugfix-02 Fix F: the recording's true start time so a browser refresh
    # doesn't reset the client's elapsed-time reference to Date.now(). No new
    # persistence needed -- session state doesn't exist yet at this point in
    # the lifecycle (create_session only happens later, in `process`), but the
    # timestamp is already durably encoded in active_session_id itself (the
    # same "-YYYYMMDD-HHMMSS" suffix cli/calendar_matcher.py already parses),
    # so this is a read, not a new write path. New field, not a change to the
    # existing boolean `recording` field above, to avoid touching that
    # contract's existing consumers.
    briefing["active_recording"] = None
    if briefing["recording"] and active_session_id:
        ts_match = re.search(r"-(\d{8})-(\d{6})$", active_session_id)
        if ts_match:
            started_at = datetime.strptime(
                f"{ts_match.group(1)}{ts_match.group(2)}", "%Y%m%d%H%M%S"
            ).isoformat()
            meetings_dir = Path(settings.paths.data_dir) / "meetings"
            type_path = meetings_dir / f"{active_session_id}.type"
            briefing["active_recording"] = {
                "session_id": active_session_id,
                "started_at": started_at,
                "meeting_type": type_path.read_text(encoding="utf-8").strip() if type_path.exists() else "general",
            }

    return JSONResponse(jsonable_encoder(briefing))

class StartRecordRequest(BaseModel):
    context: str | None = None
    title: str | None = None
    meeting_type: str | None = None  # "is-call" | "project-meeting" | "seminar"
    whisper_model: str | None = None

# Tracks the whisper_model override (if any) for the currently-active session,
# so run_pipeline can thread it into the `process` subprocess. Module-level
# global, same pattern/caveat as recording_processes/active_session_id above.
_active_whisper_model: str | None = None

# Guards the check-then-spawn sequence in /api/record/start (bugfix-02 Fix C):
# without it, two near-simultaneous requests could both pass the "already
# recording" check before either has set recording_processes, each spawning
# its own record subprocess -- the loser's process becomes untracked/orphaned
# rather than rejected. Lazily created (not at module import time) so it is
# always bound to the event loop that is actually running, per asyncio.Lock's
# own guidance against constructing it before a loop exists.
_recording_lock: asyncio.Lock | None = None


def _get_recording_lock() -> asyncio.Lock:
    global _recording_lock
    if _recording_lock is None:
        _recording_lock = asyncio.Lock()
    return _recording_lock


# Guards _llm_server_proc across llm_start/llm_stop -- see llm_start's own
# comment for why this is needed once llm_stop's termination wait moved to a
# thread. Same lazy-construction rationale as _recording_lock above.
_llm_lock: asyncio.Lock | None = None


def _get_llm_lock() -> asyncio.Lock:
    global _llm_lock
    if _llm_lock is None:
        _llm_lock = asyncio.Lock()
    return _llm_lock


@app.post("/api/record/start")
async def start_recording(req: StartRecordRequest | None = None):
    global recording_processes, active_session_id, _active_whisper_model
    async with _get_recording_lock():
        return await _start_recording_locked(req)


async def _start_recording_locked(req: StartRecordRequest | None) -> dict:
    global recording_processes, active_session_id, _active_whisper_model
    if recording_processes and all(p.poll() is None for p in recording_processes):
        return {"status": "already recording", "session_id": active_session_id}

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')

    if req and req.meeting_type == "is-call":
        # One-tap flow (architecture_v2.md §12.2): bypasses the title prompt
        # entirely and uses the is-call-* slug so meeting_type auto-detection
        # (mcp_server.meeting_type.detect_meeting_type) recognises it even if
        # the .type file were ever lost.
        active_session_id = f"is-call-{timestamp}"
    elif req and req.title:
        # Slugify the title
        slug = req.title.lower()
        slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
        slug = slug[:40] # cap length
        # Prefix so slug-based re-detection (mcp_server.meeting_type.detect_meeting_type)
        # agrees with the explicit selection even if the .type file is ever lost --
        # important now that the slug-detection default is GENERAL, not PROJECT, so an
        # un-prefixed project-meeting slug would otherwise silently misclassify.
        if req.meeting_type == "seminar":
            slug = f"seminar-{slug}"
        elif req.meeting_type == "project-meeting":
            slug = f"project-{slug}"
        active_session_id = f"{slug}-{timestamp}"
    else:
        active_session_id = f"general-{timestamp}"

    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    meetings_dir.mkdir(exist_ok=True, parents=True)

    # Explicit UI selection is priority 1 in the meeting-type resolution order
    # (architecture_v2.md §4); write it now so it exists before session state
    # does (state is only created later, in `process`/run_pipeline).
    if req and req.meeting_type:
        from mcp_server.meeting_type import write_meeting_type
        write_meeting_type(meetings_dir, active_session_id, req.meeting_type)

    # Save context if provided
    if req and req.context:
        context_path = meetings_dir / f"{active_session_id}.context.txt"
        context_path.write_text(req.context, encoding="utf-8")

    _active_whisper_model = req.whisper_model if req else None

    global live_transcript, pipeline_stage
    live_transcript = ""
    pipeline_stage = "RECORDING"
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

class HighlightRequest(BaseModel):
    note: str | None = None
    segment_offset_seconds: float | None = None
    update_last: bool = False


@app.post("/api/record/highlight")
async def log_highlight(req: HighlightRequest | None = None):
    """Log a highlight timestamp during a live recording.

    Called twice per highlight from the dashboard (architecture_v2.md §12.4):
    once bare on button click (so the highlight's timestamp reflects the
    click instant even if the user never adds a note), and optionally again
    with update_last=true if a note is typed within the 8s inline window --
    that second call amends the just-logged entry rather than appending a
    second, near-duplicate one."""
    global active_session_id
    if not active_session_id:
        return {"status": "not recording"}

    highlight_path = Path(settings.paths.data_dir) / "meetings" / f"{active_session_id}.highlights.json"
    highlights = []
    if highlight_path.exists():
        highlights = json.loads(highlight_path.read_text(encoding="utf-8"))

    if req and req.update_last and highlights:
        highlights[-1]["note"] = req.note
        highlights[-1]["segment_offset_seconds"] = req.segment_offset_seconds
    else:
        entry = {"timestamp": datetime.now().isoformat()}
        if req and req.note:
            entry["note"] = req.note
        if req and req.segment_offset_seconds is not None:
            entry["segment_offset_seconds"] = req.segment_offset_seconds
        highlights.append(entry)

    # atomic_write_text rather than a plain write_text: this is a
    # read-modify-write of a JSON list, called repeatedly while a recording
    # is live -- a crash mid-write previously risked truncating/corrupting
    # highlights.json, which extract_action_items also reads back into the
    # extraction prompt.
    atomic_write_text(highlight_path, json.dumps(highlights))
    return {"status": "highlight_logged"}

_SUPPORTED_DOC_SUFFIXES = {".pdf", ".pptx", ".docx", ".txt"}
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB


@app.post("/api/context/upload")
async def upload_context_document(session_id: str = Form(...), file: UploadFile = File(...)):
    """Pre-meeting document context upload (architecture_v2.md §7.3): extracts
    text offline, summarises it via the local LLM (bounded to ~1000 tokens),
    and writes data/meetings/<session_id>.doc_context.txt."""
    from mcp_server.schemas import validate_session_id, SchemaValidationError

    try:
        validate_session_id(session_id)
    except SchemaValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    # A session created only via /api/record/start has no state file yet (state
    # is created later, in `process`) -- so "session exists" here means either
    # a state file OR a prior artefact (e.g. .type/.context.txt) under meetings_dir,
    # not strictly state_mod.load_session_state succeeding.
    state_dir = Path(settings.paths.data_dir) / "state"
    session_known = (state_dir / f"{session_id}.json").exists() or any(
        meetings_dir.glob(f"{session_id}.*")
    ) or session_id == active_session_id
    if not session_known:
        return JSONResponse({"error": f"Unknown session '{session_id}'."}, status_code=404)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _SUPPORTED_DOC_SUFFIXES:
        return JSONResponse(
            {"error": f"Unsupported file type '{suffix}'. Allowed: {sorted(_SUPPORTED_DOC_SUFFIXES)}"},
            status_code=400,
        )

    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File exceeds 50MB limit."}, status_code=413)

    tmp_dir = Path(settings.paths.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_filename = Path(file.filename or "upload").name  # strip any path components
    tmp_path = tmp_dir / f"{session_id}_doc_{safe_filename}"
    tmp_path.write_bytes(contents)

    try:
        from cli.doc_ingest import ingest_document
        from llm.client import HttpLLMClient

        llm_client = HttpLLMClient(base_url=f"http://{settings.llm.host}:{settings.llm.port}")
        output_path = await asyncio.to_thread(
            ingest_document, tmp_path, session_id, meetings_dir, llm_client.complete
        )
        summary_tokens = len(output_path.read_text(encoding="utf-8").split())
    except (ValueError, NotImplementedError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        tmp_path.unlink(missing_ok=True)

    return JSONResponse({
        "status": "processed", "session_id": session_id,
        "filename": safe_filename, "summary_tokens": summary_tokens,
    })


class MailContextRequest(BaseModel):
    session_id: str | None = None
    subject_hint: str


@app.post("/api/context/mail")
async def fetch_mail_context_endpoint(req: MailContextRequest):
    """Best-effort Outlook mail-body context fetch (architecture_v2.md §9).
    Never raises on Outlook being unavailable -- returns status="no_match".

    session_id is optional: the pre-meeting context modal (architecture_v2.md
    §12.3) calls this *before* a recording -- and therefore a session_id --
    exists (the slug is only decided server-side in /api/record/start). In
    that case this returns the full body for the client to fold into the
    agenda/context text sent at record-start time, rather than persisting a
    .mail_context.txt for a session that doesn't exist yet. When session_id
    IS provided (e.g. a future "fetch during an active recording" call site),
    it persists as before."""
    from cli.mail_sync import fetch_mail_context, save_mail_context

    if req.session_id is not None:
        from mcp_server.schemas import validate_session_id, SchemaValidationError
        try:
            validate_session_id(req.session_id)
        except SchemaValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    session_start = datetime.now()
    if req.session_id:
        ts_match = re.search(r"-(\d{8})-(\d{6})$", req.session_id)
        if ts_match:
            session_start = datetime.strptime(f"{ts_match.group(1)}{ts_match.group(2)}", "%Y%m%d%H%M%S")

    body = await asyncio.to_thread(fetch_mail_context, session_start, req.subject_hint)
    if body is None:
        return JSONResponse({"status": "no_match"})

    if req.session_id:
        meetings_dir = Path(settings.paths.data_dir) / "meetings"
        save_mail_context(req.session_id, meetings_dir, body)
        return JSONResponse({"status": "saved", "preview": body[:100]})

    return JSONResponse({"status": "found", "body": body, "preview": body[:100]})


@app.post("/api/context/mail-file")
async def upload_mail_file(file: UploadFile = File(...), session_id: str | None = Form(None)):
    """Deterministic email context: parse a dragged-and-dropped .eml/.msg file
    (cli/mail_import.py) instead of fuzzy-matching the Outlook inbox. Same
    session_id semantics as /api/context/mail: without one, the parsed text is
    returned for the pre-meeting modal to fold into the agenda notes; with
    one, it persists as `<session_id>.mail_context.txt`."""
    from cli.mail_import import (
        SUPPORTED_MAIL_SUFFIXES, MailParseError, format_mail_context, parse_mail_file,
    )
    from cli.mail_sync import save_mail_context

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_MAIL_SUFFIXES:
        return JSONResponse(
            {"error": f"Unsupported file type '{suffix}'. Allowed: {sorted(SUPPORTED_MAIL_SUFFIXES)}"},
            status_code=400,
        )
    if session_id is not None:
        from mcp_server.schemas import validate_session_id, SchemaValidationError
        try:
            validate_session_id(session_id)
        except SchemaValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File exceeds 50MB limit."}, status_code=413)

    tmp_dir = Path(settings.paths.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"mailimport_{datetime.now().strftime('%H%M%S%f')}{suffix}"
    tmp_path.write_bytes(contents)
    try:
        parsed = await asyncio.to_thread(parse_mail_file, tmp_path)
    except MailParseError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        tmp_path.unlink(missing_ok=True)

    context_text = format_mail_context(parsed)
    if session_id:
        meetings_dir = Path(settings.paths.data_dir) / "meetings"
        save_mail_context(session_id, meetings_dir, context_text)
        return JSONResponse({"status": "saved", "subject": parsed["subject"]})
    return JSONResponse({"status": "parsed", "subject": parsed["subject"], "body": context_text})


_SUPPORTED_TRANSCRIPT_SUFFIXES = {".json", ".vtt", ".srt", ".txt"}


def _fail_session_best_effort(session_id: str, exc: Exception) -> None:
    """Transition `session_id` to FAILED after an unhandled pipeline exception,
    so it doesn't sit at STOPPED/TRANSCRIBED/EXTRACTED forever with only the
    ephemeral `pipeline_error` global (cleared by the next pipeline run) as any
    record of what happened -- bugfix-02 Fix B. Deliberately best-effort:
    - if the session has no state file yet (failure before create_session),
      there's nothing to transition;
    - if it's already terminal (FAILED/APPLIED) -- e.g. an inner step like
      apply_reviewed_update's TODO_FILE_UNPARSEABLE handling already called
      transition(FAILED) itself before re-raising -- this is a deliberate
      no-op, not a second failure to report.
    Never raises: a failure to *record* the failure must not mask the
    original exception at the caller's except block."""
    from mcp_server.state import InvalidTransitionError, State, load_session_state, transition

    state_dir = Path(settings.paths.data_dir) / "state"
    try:
        current = load_session_state(state_dir, session_id)
    except FileNotFoundError:
        return
    if current.state in (State.FAILED, State.APPLIED):
        return
    try:
        transition(
            state_dir, session_id, State.FAILED,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
            error=str(exc), error_type=type(exc).__name__,
        )
    except InvalidTransitionError as state_exc:
        logger.error("Could not transition session %s to FAILED: %s", session_id, state_exc)


async def run_extraction_only(session_id: str) -> None:
    """Run agent-run for a session already at TRANSCRIBED (import-transcript's
    web counterpart -- no process/transcribe step, since there is no audio).
    Mirrors run_pipeline's LLM-readiness + agent-run steps only."""
    global processing, pipeline_error
    pipeline_error = None
    llm_port = settings.llm.port
    llm_already_running = False
    try:
        import socket as _socket
        with _socket.create_connection((settings.llm.host, llm_port), timeout=0.5):
            llm_already_running = True
    except OSError:
        pass

    serve_proc = None
    serve_log = None
    if not llm_already_running:
        logger.info("[%s] Starting LLM Server...", session_id)
        serve_proc, serve_log = _spawn_serve_subprocess_with_log()

    try:
        await _wait_for_llm_ready(settings.llm.startup_timeout_seconds, serve_proc, serve_log)
        logger.info("[%s] Running agent (import)...", session_id)
        code, out = await _run_subprocess([sys.executable, "-m", "cli.main", "agent-run", "--session-id", session_id])
        if code != 0:
            raise Exception(f"Agent processing failed: {out}")
        logger.info("[%s] Extraction complete -- items awaiting human review in data/pending_review/.", session_id)
    except Exception as e:
        logger.error("[%s] Error in import pipeline: %s", session_id, e, exc_info=True)
        pipeline_error = str(e)
        _fail_session_best_effort(session_id, e)
    finally:
        if serve_proc is not None:
            await asyncio.to_thread(_terminate_and_wait, serve_proc, 10)
        processing = False


@app.post("/api/upload/transcript")
async def upload_transcript(
    file: UploadFile = File(...),
    session_id: str | None = Form(None),
    meeting_type: str = Form("project-meeting"),
    calendar_event_id: str | None = Form(None),
):
    """Import an externally-produced transcript directly at TRANSCRIBED
    (architecture_v2.md §8), then background the rest of the pipeline exactly
    as run_pipeline does for a recorded session, minus the transcribe step.

    `calendar_event_id` is optional (bugfix-01 Fix 5.3): when given, the
    matching event from data/calendar.json (matched via _calendar_event_id,
    since the cache has no native id field) is attached to the session's
    metadata, the same fields `cli.calendar_matcher.save_calendar_match` would
    write for an auto-matched live recording. If the id doesn't resolve to a
    real event, this logs a warning and proceeds without a calendar link --
    it never fails the import over optional enrichment."""
    from mcp_server.meeting_type import MeetingType, write_meeting_type
    from mcp_server.schemas import validate_session_id, SchemaValidationError
    from mcp_server import state as state_mod

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _SUPPORTED_TRANSCRIPT_SUFFIXES:
        return JSONResponse(
            {"error": f"Unsupported file type '{suffix}'. Allowed: {sorted(_SUPPORTED_TRANSCRIPT_SUFFIXES)}"},
            status_code=400,
        )
    try:
        MeetingType(meeting_type)
    except ValueError:
        return JSONResponse({"error": f"Invalid meeting_type {meeting_type!r}."}, status_code=422)

    if session_id is None:
        session_id = f"import-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    else:
        try:
            validate_session_id(session_id)
        except SchemaValidationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        state_dir_check = Path(settings.paths.data_dir) / "state"
        if (state_dir_check / f"{session_id}.json").exists():
            return JSONResponse({"error": f"Session '{session_id}' already exists."}, status_code=409)

    contents = await file.read()
    if len(contents) > _MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File exceeds 50MB limit."}, status_code=413)

    tmp_dir = Path(settings.paths.tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{session_id}_transcript{suffix}"
    tmp_path.write_bytes(contents)

    try:
        from transcribe.import_parsers import parse_transcript_file
        from transcribe.postprocess import write_transcript
        from transcribe.whisper_runner import TranscriptionResult, TranscriptSegment

        segments = parse_transcript_file(tmp_path)
        if not segments:
            return JSONResponse({"error": "No segments could be parsed from this file."}, status_code=400)

        state_dir = Path(settings.paths.data_dir) / "state"
        meetings_dir = Path(settings.paths.data_dir) / "meetings"
        meetings_dir.mkdir(parents=True, exist_ok=True)

        calendar_metadata: dict = {}
        if calendar_event_id:
            calendar_cache = Path(settings.paths.data_dir) / "calendar.json"
            if calendar_cache.exists():
                try:
                    cached_events = json.loads(calendar_cache.read_text(encoding="utf-8"))
                    matched_event = next(
                        (e for e in cached_events if _calendar_event_id(e) == calendar_event_id), None
                    )
                    if matched_event:
                        calendar_metadata = {
                            "calendar_event_id": calendar_event_id,
                            "calendar_subject": matched_event.get("subject"),
                            "calendar_start": matched_event.get("start"),
                            "calendar_organiser": matched_event.get("organizer") or matched_event.get("organiser"),
                        }
                    else:
                        logger.warning(
                            "[%s] calendar_event_id %r did not match any cached event; "
                            "proceeding without a calendar link.", session_id, calendar_event_id,
                        )
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("[%s] Could not read calendar.json for linking: %s", session_id, exc)

        state_mod.create_session(
            state_dir, session_id, settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
            initial_state=state_mod.State.STOPPED, meeting_type=meeting_type, source="import",
            whisper_model="imported", **calendar_metadata,
        )
        write_meeting_type(meetings_dir, session_id, meeting_type)

        result = TranscriptionResult(
            session_id=session_id,
            segments=[TranscriptSegment(**seg) for seg in segments],
            language="unknown",
            duration_seconds=segments[-1]["end"],
            model_name="imported",
            diarised=False,
        )
        write_transcript(meetings_dir, result)

        state_mod.transition(
            state_dir, session_id, state_mod.State.TRANSCRIBED,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
            transcript_path=str(meetings_dir / f"{session_id}.md"),
        )
    except ValueError as exc:
        # Malformed transcript content (truncated JSON, bad segment shape --
        # import_parsers normalises these to ValueError). A bad upload is a
        # client error, not a server crash.
        return JSONResponse({"error": f"Could not parse transcript: {exc}"}, status_code=400)
    finally:
        tmp_path.unlink(missing_ok=True)

    global processing
    processing = True
    asyncio.create_task(run_extraction_only(session_id))

    return JSONResponse({"status": "importing", "session_id": session_id, "segments_count": len(segments)})


def _terminate_and_wait(proc: subprocess.Popen, timeout_seconds: float) -> None:
    """terminate() then wait() with a kill() fallback on timeout -- the same
    three-line pattern this file previously repeated inline at every
    subprocess-shutdown site (run_pipeline's and run_extraction_only's
    LLM-shutdown `finally` blocks, and POST /api/server/llm/stop), each of
    them blocking the event loop for up to the full timeout since the calls
    were made directly inside `async def` functions/coroutines. Callers
    should `await asyncio.to_thread(_terminate_and_wait, proc, timeout)`."""
    proc.terminate()
    try:
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_for_recording_processes_to_stop(processes: list[subprocess.Popen], grace_seconds: float) -> None:
    """Blocking wait for the mic/loopback recorder subprocesses to exit after
    their stop-sentinel files are touched, falling back to terminate() for
    any that don't. Pulled out into its own function so stop_recording can
    run it via asyncio.to_thread instead of blocking the event loop for the
    whole grace period (see the call site's comment)."""
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if all(p.poll() is not None for p in processes):
            break
        time.sleep(0.1)

    for p in processes:
        if p.poll() is None:
            logger.warning("Record subprocess did not exit within grace period; force-killing.")
            p.terminate()
            p.wait(timeout=2)


@app.post("/api/record/stop")
async def stop_recording(auto_accept: bool = False, session_id: str | None = None):
    """`auto_accept=False` (default) stops the pipeline after agent-run and
    leaves extracted items in data/pending_review/ for a human to review via
    the normal `meeting-agent review`/`apply` commands -- preserving the
    project's human-in-the-loop guarantee. Pass `auto_accept=true` only if you
    explicitly want the web dashboard to accept every extracted item with no
    review step; this is an opt-in, not the default.

    SB-2.2: optional `session_id` parameter — when provided, the request is
    rejected with 409 if it doesn't match the currently-active session, so a
    stale or replayed stop request can't silently terminate the wrong session."""
    global recording_processes, active_session_id, processing, _active_whisper_model, _processing_session_id
    # Bug fix: this function used to rely on an accident -- the old blocking
    # wait/kill loop below froze the whole single-worker event loop for its
    # duration, which incidentally also prevented a concurrent
    # start_recording from running at the same time. Once that wait was
    # offloaded to a thread (see the comment further down), the event loop
    # became free during the wait, so a fast Stop-then-Start could interleave:
    # _start_recording_locked's guard could see one of the two recorder
    # subprocesses already exited (mic/loopback don't exit at exactly the
    # same instant) and proceed to start a new recording, which this
    # function's unconditional `recording_processes = []` etc. below would
    # then clobber. start_recording already serializes through
    # _get_recording_lock() for exactly this class of race (see its own
    # comment); stop_recording now holds the same lock for its whole
    # critical section so the two can never interleave.
    async with _get_recording_lock():
        if not recording_processes:
            return {"status": "not recording"}
        if session_id is not None and session_id != active_session_id:
            return JSONResponse(
                {"error": f"session_id mismatch: request has '{session_id}', active is '{active_session_id}'"},
                status_code=409,
            )

        sess_id = active_session_id
        whisper_model_for_session = _active_whisper_model

        # Signal both subprocesses to stop gracefully via sentinel files.  On Windows,
        # p.terminate() = TerminateProcess() which kills instantly before Python's
        # I/O buffer is flushed — the WAV writer's close() never runs, leaving a
        # 0-byte or header-only WAV that libav can't open.  The sentinel file lets the
        # record subprocess exit its poll loop normally so buffer.stop() / wave.close()
        # runs and all data is safely on disk.
        tmp_dir = Path(settings.paths.tmp_dir)
        for sub_id in [f"{sess_id}-mic", f"{sess_id}-loop"]:
            (tmp_dir / f"{sub_id}.stop").touch()

        # Bug fix: this wait/terminate loop is blocking (time.sleep, Popen.wait)
        # and used to run directly inside this async def handler -- for up to
        # ~4-6s, every other request (the dashboard's 5s polling, the live-
        # transcript SSE stream, any other tab) froze, since uvicorn runs
        # single-worker here by design. Offloaded to a thread, same pattern
        # already used elsewhere in this file for other blocking calls. Safe
        # to hold _recording_lock (an asyncio.Lock) across this await -- it
        # only blocks other coroutines from acquiring the same lock, not the
        # event loop itself.
        await asyncio.to_thread(_wait_for_recording_processes_to_stop, recording_processes, 4.0)

        recording_processes = []
        active_session_id = None
        _active_whisper_model = None

        # Recording has already stopped and its audio is safely on disk above --
        # that part can never be rejected. The pipeline itself, however, must not
        # race a still-running one for the shared `processing` lock (e.g. a
        # resumed stalled session's pipeline still in flight), so it's scheduled
        # via the same-serializing helper rather than claimed immediately here.
        asyncio.create_task(_run_pipeline_when_free(sess_id, auto_accept, whisper_model_for_session))
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

def _spawn_serve_subprocess_with_log() -> tuple[subprocess.Popen, "collections.deque"]:
    """Launch `cli.main serve` with its stdout captured into a bounded, thread-safe
    deque, so a failure can be diagnosed (process-liveness + last output) instead of
    just silently waiting out the full health-check timeout. Mirrors the same
    capture pattern llm/server_manager.py uses for the llama-server grandchild --
    this captures the `cli.main serve` child's output, which relays that same
    grandchild output via its own logger."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "cli.main", "serve"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    log_buffer: collections.deque = collections.deque(maxlen=500)

    def _relay() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            log_buffer.append(line.rstrip())

    threading.Thread(target=_relay, daemon=True).start()
    return proc, log_buffer


async def _wait_for_llm_ready(
    timeout_seconds: float,
    serve_proc: subprocess.Popen | None = None,
    log_buffer=None,
) -> None:
    """Poll the LLM server's own health endpoint instead of guessing a fixed
    sleep -- a 30B-class model can take well over 15s to cold-load, and a
    small model is ready in a couple of seconds, so a fixed sleep was either
    too short (raw connection-refused failures downstream) or wastefully long.

    `serve_proc`/`log_buffer` are optional: when the caller spawned the
    `cli.main serve` subprocess itself (not reusing an already-running server),
    passing them lets this function fail fast if that process exits early,
    with its captured output in the error, rather than waiting out the full
    timeout against a process that is already dead."""
    health_url = f"http://{settings.llm.host}:{settings.llm.port}{settings.llm.health_check_path}"
    v1_health_url = f"http://{settings.llm.host}:{settings.llm.port}/v1/health"
    deadline = time.monotonic() + timeout_seconds

    def _log_tail() -> str:
        if not log_buffer:
            return "(no output captured)"
        tail = list(log_buffer)[-20:]
        return "\n".join(tail) if tail else "(no output captured)"

    async with make_local_client() as client:
        while time.monotonic() < deadline:
            if serve_proc is not None and serve_proc.poll() is not None:
                raise RuntimeError(
                    f"`meeting-agent serve` exited (code={serve_proc.returncode}) before the "
                    f"LLM server became healthy.\nLast output:\n{_log_tail()}\n"
                    "Common causes: model weights not found (run `meeting-agent setup "
                    "--profile <name>`), CUDA out of memory, or a corrupt download."
                )
            for url in (health_url, v1_health_url):
                if await probe_ok(client, url, timeout=2.0):
                    return
            await asyncio.sleep(1)

    raise TimeoutError(
        f"LLM server did not become healthy within {timeout_seconds:.0f}s at {health_url}.\n"
        f"Last server output:\n{_log_tail()}\n"
        "If you see 'model file does not exist': run `meeting-agent setup --profile <name>`.\n"
        "If you see a CUDA error: check `nvidia-smi` and your driver version.\n"
        "If there is no output at all: confirm LLAMA_SERVER_EXE points at a real executable."
    )


async def _run_subprocess(
    args: list[str], extra_env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run a subprocess without blocking the event loop.  `subprocess.run()`
    is synchronous I/O; called directly inside a coroutine it would freeze
    every other request (SSE live-transcript, /api/briefing polls, highlight
    clicks) for the full duration of the child process.

    `extra_env` is merged on top of the current environment so callers can pass
    per-invocation flags (e.g. Fix 1.3's MA_TRANSCRIPTION_DONE) without touching
    the module-level environment."""
    import copy

    env: dict | None = None
    if extra_env:
        env = copy.copy(os.environ)
        env.update(extra_env)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, (stdout or b"").decode(errors="replace")


async def run_pipeline(session_id: str, auto_accept: bool = False, whisper_model: str | None = None):
    global processing, pipeline_error, pipeline_stage
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
        with _socket.create_connection((settings.llm.host, llm_port), timeout=0.5):
            llm_already_running = True
    except OSError:
        pass

    serve_proc = None
    serve_log = None
    if llm_already_running:
        logger.info("[%s] LLM server already running on port %s — reusing.", session_id, llm_port)
    else:
        # Spawned here (non-blocking) but NOT awaited yet -- Whisper transcription
        # below has no dependency on the LLM server, so gating it behind LLM
        # readiness only added latency for no reason. Starting the subprocess now
        # lets its cold-load happen in parallel with transcription; we only block
        # on _wait_for_llm_ready right before the one step that actually needs it
        # (agent-run / extraction).
        logger.info("[%s] Starting LLM Server (in parallel with transcription)...", session_id)
        serve_proc, serve_log = _spawn_serve_subprocess_with_log()

    try:
        # 2. Process (transcribe) -- Whisper only, no LLM server dependency.
        pipeline_stage = "TRANSCRIBING"
        logger.info("[%s] Transcribing audio...", session_id)
        process_args = [sys.executable, "-m", "cli.main", "process", "--session-id", session_id]
        if whisper_model:
            process_args += ["--whisper-model", whisper_model]
        code, out = await _run_subprocess(process_args)
        if code != 0:
            raise Exception(f"Transcription failed: {out}")

        # Best-effort calendar-event enrichment (architecture_v2.md §10) --
        # never blocks or fails the pipeline. session_start is recovered from
        # the session_id's own embedded "-YYYYMMDD-HHMMSS" timestamp (set at
        # /api/record/start time) since this web-triggered flow does not go
        # through mcp_server.tools.recording.start_meeting (which is the only
        # code path that would otherwise record a RECORDING history entry).
        try:
            from cli.calendar_matcher import match_calendar_event, save_calendar_match
            from mcp_server.state import load_session_state

            state_dir = Path(settings.paths.data_dir) / "state"
            ts_match = re.search(r"-(\d{8})-(\d{6})$", session_id)
            session_state = load_session_state(state_dir, session_id)
            stopped_at = next((h["at"] for h in session_state.history if h["state"] == "STOPPED"), None)
            if ts_match and stopped_at:
                session_start = datetime.strptime(f"{ts_match.group(1)}{ts_match.group(2)}", "%Y%m%d%H%M%S")
                session_end = datetime.fromisoformat(stopped_at).replace(tzinfo=None)
                calendar_cache = Path(settings.paths.data_dir) / "calendar.json"
                event = match_calendar_event(session_start, session_end, calendar_cache)
                if event:
                    save_calendar_match(
                        session_id, state_dir, settings.concurrency.lock_path,
                        settings.concurrency.lock_timeout_seconds, event,
                    )
                    logger.info("[%s] Matched calendar event: %s", session_id, event.get("subject"))
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment only
            logger.warning("[%s] Calendar event matching failed (non-fatal): %s", session_id, exc)

        # 3. LLM readiness gate -- deliberately placed here, immediately before the
        # one step that actually needs the LLM server, not before recording/
        # transcription (see the comment where serve_proc is spawned above).
        if not llm_already_running:
            pipeline_stage = "LLM_LOADING"
        try:
            await _wait_for_llm_ready(settings.llm.startup_timeout_seconds, serve_proc, serve_log)
        except (RuntimeError, TimeoutError) as exc:
            raise Exception(
                f"LLM server unavailable — transcription saved at "
                f"data/meetings/{session_id}.md. You can retry extraction later with: "
                f"meeting-agent agent-run --session-id {session_id} "
                f"(once the LLM server is confirmed healthy). Underlying error: {exc}"
            )

        # 4. Agent Run — Fix 1.3: signal that transcription is already done so
        # the agent does not attempt to re-transcribe via transcribe_meeting.
        pipeline_stage = "EXTRACTING"
        logger.info("[%s] Running agent...", session_id)
        code, out = await _run_subprocess(
            [sys.executable, "-m", "cli.main", "agent-run", "--session-id", session_id],
            extra_env={"MA_TRANSCRIPTION_DONE": "1"},
        )
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
                        priority=item.priority, evidence=item.evidence,
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
                        # apply_reviewed_update -> cli.git_backup.commit_all/ensure_repo
                        # uses a blocking subprocess.run internally (git commit); offloaded
                        # to a thread so it doesn't block the event loop, same rationale
                        # as _run_subprocess above for the process/agent-run steps.
                        await asyncio.to_thread(
                            apply_reviewed_update,
                            token, session_id, pending_dir, todo_path, data_dir, state_dir,
                            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
                        )
                    except (InvalidTransitionError, TodoFileUnparsableError) as exc:
                        raise Exception(f"Apply failed: {exc}")
        else:
            pipeline_stage = "AWAITING_REVIEW"
            logger.info("[%s] Extraction complete -- items awaiting human review in data/pending_review/.", session_id)
    except Exception as e:
        pipeline_stage = "ERROR"
        logger.error("[%s] Error in pipeline: %s", session_id, e, exc_info=True)
        pipeline_error = str(e)
        _fail_session_best_effort(session_id, e)
    finally:
        if serve_proc is not None:
            # Only stop the server if we started it; if it was already running
            # we leave it alone so the user's pre-warmed instance stays up.
            logger.info("[%s] Stopping LLM Server (started by this pipeline run)...", session_id)
            await asyncio.to_thread(_terminate_and_wait, serve_proc, 10)
        else:
            logger.info("[%s] LLM server was pre-existing — left running.", session_id)
        # SB-2.1: ensure active_session_id and recording_processes are cleared even
        # if stop_recording() wasn't the code path that triggered this pipeline run
        # (e.g. run_extraction_only, a retry, or an unexpected call order).
        global active_session_id, recording_processes
        if active_session_id == session_id:
            active_session_id = None
            recording_processes = []
        processing = False
        if pipeline_stage not in ("AWAITING_REVIEW", "ERROR"):
            pipeline_stage = None
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
        # Fix 3.3: load quality metadata from session state for the review UI.
        quality_label = None
        quality_score = None
        quality_flags: list = []
        try:
            from mcp_server.state import load_session_state as _load_ss
            sess_state = _load_ss(state_dir, session_id)
            quality_label = sess_state.metadata.get("quality_label")
            quality_score = sess_state.metadata.get("quality_score")
            quality_flags = sess_state.metadata.get("quality_flags") or []
        except FileNotFoundError:
            pass
        result.append({
            "session_id": session_id,
            "quality_label": quality_label,
            "quality_score": quality_score,
            "quality_flags": quality_flags,
            "items": [
                {
                    "id": item.id,
                    "description": item.description,
                    "owner": item.owner,
                    "due_date": item.due_date,
                    "priority": item.priority,
                    "evidence": item.evidence,
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
    priority: str | None = None
    evidence: str | None = None
    rejection_reason: str | None = None  # Fix 3.4: optional reason for rejections


class ReviewDecideRequest(BaseModel):
    session_id: str
    decisions: list[ReviewItemDecision]


@app.post("/api/review/decide")
async def post_review_decide(req: ReviewDecideRequest):
    """Accept/reject (with optional edits) each proposed item for one session.

    Calls complete_review() exactly as cli/main.py's `review` command does —
    no transition logic is reimplemented here."""
    from cli.review_apply import ReviewDecision, complete_review, load_pending_items as _load_pending
    from mcp_server.state import InvalidTransitionError

    state_dir = Path(settings.paths.data_dir) / "state"
    pending_review_dir = Path(settings.paths.data_dir) / "pending_review"

    # Fix 3.4 (record_edit): snapshot original descriptions NOW, before
    # complete_review() renames/moves the pending draft.  Used later to detect
    # description changes the user made before accepting, so each correction
    # is written to data/feedback/edits.jsonl as a training signal.
    _draft_path = pending_review_dir / f"{req.session_id}.md"
    _original_descriptions: dict[str, str] = {}
    try:
        for _orig_item in _load_pending(_draft_path):
            _original_descriptions[_orig_item.id] = _orig_item.description or ""
    except Exception:  # noqa: BLE001 - best-effort; never blocks the review
        pass

    decisions = [
        ReviewDecision(
            id=d.id,
            decision=d.decision,
            description=d.description,
            owner=d.owner,
            due_date=d.due_date,
            session_id=req.session_id,
            priority=d.priority,
            evidence=d.evidence,
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

    # Bug fix: pipeline_stage is set to "AWAITING_REVIEW" by run_pipeline once
    # a session reaches PROPOSED (see that function's tail), but nothing ever
    # cleared it again once the human actually acted on the review -- the
    # Dashboard tab's "Ready for Review" banner kept showing indefinitely
    # until some *other* pipeline run happened to overwrite the global. Reset
    # only on a successful decision (not on the error returns above, so a
    # rejected/invalid request doesn't mask a genuinely still-in-flight
    # pipeline). Unconditional otherwise: this module is single-process/
    # single-session by design (see the recording_processes/processing
    # globals' own docstrings), so there is no concurrent session whose stage
    # this could clobber.
    global pipeline_stage
    pipeline_stage = None

    # Fix 3.4: record rejections as feedback for future extraction prompts.
    from cli.feedback import record_rejection as _record_rejection

    feedback_dir = Path(settings.paths.data_dir) / "feedback"
    meeting_type_str = ""
    try:
        from mcp_server.state import load_session_state as _load_ss2
        _ss = _load_ss2(state_dir, req.session_id)
        meeting_type_str = _ss.metadata.get("meeting_type", "")
    except FileNotFoundError:
        pass
    for d in req.decisions:
        if d.decision == "reject":
            try:
                _record_rejection(
                    session_id=req.session_id,
                    item_id=d.id,
                    item_description=d.description,
                    rejection_reason=d.rejection_reason or "",
                    feedback_dir=feedback_dir,
                    meeting_type=meeting_type_str,
                )
            except Exception as exc:  # noqa: BLE001 - feedback is best-effort
                logger.warning("Failed to record rejection for item %s: %s", d.id, exc)

    # Fix 3.4 (record_edit): for accepted items whose description was edited by
    # the user before submitting, write a correction record so future extraction
    # prompts can reference real human rewrites as few-shot examples.
    from cli.feedback import record_edit as _record_edit
    for d in req.decisions:
        if d.decision == "accept":
            original = _original_descriptions.get(d.id, "")
            if original and d.description and d.description != original:
                try:
                    _record_edit(
                        session_id=req.session_id,
                        item_id=d.id,
                        original=original,
                        corrected=d.description,
                        feedback_dir=feedback_dir,
                        meeting_type=meeting_type_str,
                    )
                except Exception as exc:  # noqa: BLE001 - feedback is best-effort
                    logger.warning("Failed to record edit for item %s: %s", d.id, exc)

    return JSONResponse({
        "session_id": req.session_id,
        "state": result["state"],
        "accepted_count": result["accepted_count"],
        "rejected_count": result["rejected_count"],
    })


class VaultExportRequest(BaseModel):
    session_id: str | None = None  # None -> export all reviewed-grade sessions


@app.post("/api/export/vault")
async def post_vault_export(req: VaultExportRequest):
    """Obsidian/Markdown-vault export. Requires [export].vault_dir in
    settings.toml (the web UI has no path picker by design -- the vault
    location is configuration, not per-request input)."""
    from cli.vault_export import ExportError, export_all, export_session

    vault_dir = getattr(getattr(settings, "export", None), "vault_dir", "") or ""
    if not vault_dir.strip():
        return JSONResponse(
            {"error": "No vault configured. Set [export].vault_dir in config/settings.toml."},
            status_code=400,
        )
    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    todo_path = Path(settings.paths.data_dir) / "todo.md"
    state_dir = Path(settings.paths.data_dir) / "state"
    try:
        if req.session_id:
            path = await asyncio.to_thread(
                export_session, req.session_id, meetings_dir, todo_path, state_dir, vault_dir
            )
            return JSONResponse({"status": "exported", "paths": [str(path)]})
        written = await asyncio.to_thread(export_all, meetings_dir, todo_path, state_dir, vault_dir)
        return JSONResponse({"status": "exported", "paths": [str(p) for p in written]})
    except ExportError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


class DocxExportRequest(BaseModel):
    session_id: str


@app.post("/api/export/docx")
async def post_docx_export(req: DocxExportRequest):
    """Standalone Word export (roadmap item 3) -- independent of
    [export].vault_dir, unlike /api/export/vault above. Renders to a
    tmp file and streams it back as a download rather than writing into
    data/, since a docx is a one-off artefact the user takes with them, not
    something meant to accumulate in a synced location."""
    from cli.docx_export import ExportError, export_session_docx

    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    todo_path = Path(settings.paths.data_dir) / "todo.md"
    state_dir = Path(settings.paths.data_dir) / "state"
    tmp_dir = Path(settings.paths.tmp_dir)
    output_path = tmp_dir / f"{req.session_id}.docx"
    try:
        # python-docx's Document.save() is blocking file I/O -- offloaded the
        # same way every other blocking call in this file is.
        path = await asyncio.to_thread(
            export_session_docx, req.session_id, meetings_dir, todo_path, state_dir, output_path
        )
    except ExportError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return FileResponse(
        path,
        filename=f"{req.session_id}.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


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
        # Offloaded to a thread: apply_reviewed_update -> cli.git_backup's
        # commit_all/ensure_repo use a blocking subprocess.run internally.
        result = await asyncio.to_thread(
            apply_reviewed_update,
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

    # Bug fix (defensive, mirrors post_review_decide above): reset
    # pipeline_stage on a successful apply too, in case a session was decided
    # via the CLI's own `review` command and only applied through the web
    # dashboard here -- post_review_decide never ran in this process for that
    # session, so its reset wouldn't have fired.
    global pipeline_stage
    pipeline_stage = None

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
    state_dir = Path(settings.paths.data_dir) / "state"

    # Hybrid (BM25 + local dense vectors) when the semantic index exists;
    # transparently BM25-only otherwise -- cli/semantic_search.py degrades
    # rather than failing the search box.
    db_path = Path(settings.paths.data_dir) / "semantic_index.db"
    if db_path.exists():
        from cli.semantic_search import hybrid_search

        hybrid = await asyncio.to_thread(
            hybrid_search, q.strip(), meetings_dir, state_dir, db_path
        )
        return JSONResponse(jsonable_encoder({
            "semantic": hybrid["semantic_available"],
            "results": hybrid["results"],
        }))

    results = search_meetings(meetings_dir, q.strip(), state_dir=state_dir)
    return JSONResponse(jsonable_encoder({
        "semantic": False,
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


@app.post("/api/search/reindex")
async def post_search_reindex():
    """(Re)build the local semantic index over reviewed sessions. Explicitly
    user-triggered (embedding a large history takes seconds-to-minutes on
    CPU); the index file is derived data under data/, safe to delete."""
    from cli.semantic_search import refresh_index

    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    state_dir = Path(settings.paths.data_dir) / "state"
    db_path = Path(settings.paths.data_dir) / "semantic_index.db"
    try:
        stats = await asyncio.to_thread(refresh_index, meetings_dir, state_dir, db_path)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not a 500
        return JSONResponse(
            {"error": f"Semantic indexing unavailable: {exc}. "
                      "Run `meeting-agent setup` to fetch the embedding model."},
            status_code=503,
        )
    return JSONResponse({"status": "indexed", **stats})


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

    # Calendar-link metadata (architecture_v2.md §10 / bugfix-01 Fix 5.4) --
    # best-effort: a session may not have state yet (e.g. mid-recording), or may
    # never have been matched to a calendar event.
    calendar_subject = None
    calendar_start = None
    calendar_organiser = None
    try:
        from mcp_server.state import load_session_state

        state_dir = Path(settings.paths.data_dir) / "state"
        session_state = load_session_state(state_dir, session_id)
        calendar_subject = session_state.metadata.get("calendar_subject") or session_state.metadata.get("calendar_event_subject")
        calendar_start = session_state.metadata.get("calendar_start") or session_state.metadata.get("calendar_event_start")
        calendar_organiser = session_state.metadata.get("calendar_organiser") or session_state.metadata.get("calendar_event_organiser")
    except FileNotFoundError:
        pass

    return JSONResponse({
        "session_id": session_id,
        "transcript": read_opt(transcript_path),
        "summary": read_opt(meetings_dir / f"{session_id}.summary.md"),
        "actions": read_json_opt(meetings_dir / f"{session_id}.actions.json"),
        "highlights": read_json_opt(meetings_dir / f"{session_id}.highlights.json"),
        "mom_content": read_opt(meetings_dir / f"{session_id}.mom.md"),
        "calendar_subject": calendar_subject,
        "calendar_start": calendar_start,
        "calendar_organiser": calendar_organiser,
    })


def _calendar_event_id(event: dict) -> str:
    """Derive a stable id for a calendar.json event (the cache has no native id
    field). Hash of subject+date+start -- stable across re-syncs as long as the
    event itself doesn't change, good enough to round-trip a selection from the
    frontend back to the same event server-side without changing calendar.json's
    storage format."""
    import hashlib

    raw = f"{event.get('subject', '')}|{event.get('date', '')}|{event.get('start', '')}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


@app.get("/api/calendar/events")
async def get_calendar_events(date: str):
    """List cached calendar events near `date` (YYYY-MM-DD), for the transcript-
    upload calendar-link picker. Never raises on a missing/malformed cache --
    calendar linking is optional enrichment, not a hard dependency."""
    from datetime import timedelta as _timedelta

    calendar_cache = Path(settings.paths.data_dir) / "calendar.json"
    if not calendar_cache.exists():
        return JSONResponse({"events": [], "date": date})

    try:
        target = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"error": f"Invalid date {date!r}, expected YYYY-MM-DD."}, status_code=422)

    try:
        events = json.loads(calendar_cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({"events": [], "date": date})

    window = {(target - _timedelta(days=1)).isoformat(), target.isoformat(), (target + _timedelta(days=1)).isoformat()}
    matched = [e for e in events if e.get("date") in window]

    return JSONResponse({
        "events": [
            {
                "id": _calendar_event_id(e),
                "subject": e.get("subject"),
                "date": e.get("date"),
                "start": e.get("start"),
                "end": e.get("end"),
                "organiser": e.get("organizer") or e.get("organiser"),
            }
            for e in matched
        ],
        "date": date,
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
        with socket.create_connection((settings.llm.host, llm_port), timeout=0.3):
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
        "pipeline_stage": pipeline_stage,
        "active_session": active_session_id,
    })


@app.post("/api/server/llm/start")
async def llm_start():
    """Start the LLM server as a background process managed by the dashboard."""
    global _llm_server_proc
    # Bug fix: llm_stop's termination wait is offloaded to a thread (below),
    # which frees the event loop for its duration -- without a lock, a
    # concurrent llm_start could see the not-yet-reaped old process and
    # wrongly report "already_running", or overwrite _llm_server_proc with a
    # new process that llm_stop's unconditional `_llm_server_proc = None`
    # (once its wait completes) would then orphan. Same _get_recording_lock
    # pattern used for the analogous recording race.
    async with _get_llm_lock():
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

    async with _get_llm_lock():
        # Kill the process we track explicitly
        if _llm_server_proc is not None and _llm_server_proc.poll() is None:
            killed.append(_llm_server_proc.pid)
            await asyncio.to_thread(_terminate_and_wait, _llm_server_proc, 5)
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


# ── AI reasoning: recurring blockers + weekly digest ────────────────────────────

@app.get("/api/blockers/recurring")
async def get_recurring_blockers():
    """Reads data/recurring_blockers.json (written best-effort after each
    IS-call extraction). Returns an empty list if it does not exist yet."""
    path = Path(settings.paths.data_dir) / "recurring_blockers.json"
    if not path.exists():
        return JSONResponse({"blockers": [], "last_updated": None})
    data = json.loads(path.read_text(encoding="utf-8"))
    return JSONResponse(data)


@app.get("/api/summary/weekly")
async def get_weekly_summary(days: int = 7):
    """User-initiated weekly digest (never auto-run). Serves the cached
    result if younger than 6h, else generates a fresh one via the local LLM."""
    from cli.weekly_summary import generate_weekly_summary, load_cached_weekly_summary

    meetings_dir = Path(settings.paths.data_dir) / "meetings"
    state_dir = Path(settings.paths.data_dir) / "state"

    cached = load_cached_weekly_summary(meetings_dir)
    if cached:
        return JSONResponse(cached)

    from llm.client import HttpLLMClient

    llm_client = HttpLLMClient(base_url=f"http://{settings.llm.host}:{settings.llm.port}")
    try:
        summary = await asyncio.to_thread(generate_weekly_summary, meetings_dir, state_dir, llm_client.complete, days)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not a pipeline failure
        return JSONResponse({"error": str(exc)}, status_code=502)

    cached = load_cached_weekly_summary(meetings_dir)
    return JSONResponse(cached or {"summary": summary, "generated_at": None, "session_count": None})


# ── Settings ───────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    """Read-only subset of settings.toml the UI needs (Settings panel)."""
    return JSONResponse({
        "whisper_model": settings.whisper.model,
        "diarisation_enabled": settings.whisper.diarisation_enabled,
    })


class SettingsPatchRequest(BaseModel):
    whisper_model: str | None = None
    diarisation_enabled: bool | None = None


@app.patch("/api/settings")
async def patch_settings(req: SettingsPatchRequest):
    """Update a narrow, known-safe subset of settings.toml (currently just
    [whisper].model). Uses a targeted regex substitution rather than a full
    tomllib-parse + re-serialise round trip, since Python's stdlib TOML
    support is read-only and a full rewrite would silently drop comments in
    a file the user may hand-edit -- see architecture_v2.md deviation notes."""
    if req.whisper_model is None and req.diarisation_enabled is None:
        return JSONResponse({"status": "no_changes"})

    text = DEFAULT_SETTINGS_PATH.read_text(encoding="utf-8")
    new_text = text
    if req.whisper_model is not None:
        valid_models = {"base", "small", "medium", "large-v3", "distil-large-v3"}
        if req.whisper_model not in valid_models:
            return JSONResponse({"error": f"whisper_model must be one of {sorted(valid_models)}"}, status_code=422)
        new_text, count = re.subn(
            r'(\[whisper\][^\[]*?\bmodel\s*=\s*)"[^"]*"',
            lambda m: f'{m.group(1)}"{req.whisper_model}"',
            new_text, count=1, flags=re.DOTALL,
        )
        if count == 0:
            return JSONResponse({"error": "Could not locate [whisper].model in settings.toml"}, status_code=500)
    if req.diarisation_enabled is not None:
        new_text, count = re.subn(
            r'(\[whisper\][^\[]*?\bdiarisation_enabled\s*=\s*)(true|false)',
            lambda m: f'{m.group(1)}{str(req.diarisation_enabled).lower()}',
            new_text, count=1, flags=re.DOTALL,
        )
        if count == 0:
            return JSONResponse({"error": "Could not locate [whisper].diarisation_enabled in settings.toml"}, status_code=500)

    # Bug fix: this used to hand-roll a tmp-write + rename without fsync()ing
    # the tmp file first -- atomic against a torn read (another process never
    # sees a half-written file), but not against power loss (a crash between
    # the write and the rename could still lose the tmp file's contents on
    # some filesystems/OS caches). atomic_write_text (already imported at the
    # top of this module, and already used elsewhere in it) closes that gap
    # with the same tmp+fsync+os.replace pattern used for every other
    # artefact writer in this project.
    atomic_write_text(DEFAULT_SETTINGS_PATH, new_text)

    global settings
    settings = load_settings(DEFAULT_SETTINGS_PATH)
    return JSONResponse({
        "status": "saved",
        "whisper_model": settings.whisper.model,
        "diarisation_enabled": settings.whisper.diarisation_enabled,
    })


# ── Manual task entry / status tracking (architecture_v2.md §Phase 7.2) ─────────

class ManualTaskRequest(BaseModel):
    description: str
    title: str | None = None
    owner: str | None = None
    due_date: str | None = None
    priority: str = "MEDIUM"
    status: str | None = None
    tag: str | None = None
    progress_note: str | None = None
    project_id: str | None = None
    reminder_date: str | None = None


@app.post("/api/tasks/manual")
async def create_manual_task(req: ManualTaskRequest):
    from cli.capability import mint_capability_token
    from cli.review_apply import write_manual_task

    description = req.description.strip()
    if not description or len(description) > 500:
        return JSONResponse({"error": "description must be non-empty and at most 500 characters."}, status_code=422)
    if req.title and len(req.title) > 200:
        return JSONResponse({"error": "title must be at most 200 characters."}, status_code=422)
    if req.owner and len(req.owner) > 200:
        return JSONResponse({"error": "owner must be at most 200 characters."}, status_code=422)
    if req.priority not in {"HIGH", "MEDIUM", "LOW"}:
        return JSONResponse({"error": "priority must be one of HIGH/MEDIUM/LOW."}, status_code=422)
    if req.status is not None and req.status not in _VALID_TASK_STATUSES:
        return JSONResponse({"error": f"status must be one of {sorted(_VALID_TASK_STATUSES)}."}, status_code=422)
    if req.tag and len(req.tag) > 50:
        return JSONResponse({"error": "tag must be at most 50 characters."}, status_code=422)
    if req.progress_note and len(req.progress_note) > 200:
        return JSONResponse({"error": "progress_note must be at most 200 characters."}, status_code=422)
    if req.project_id and len(req.project_id) > 100:
        return JSONResponse({"error": "project_id must be at most 100 characters."}, status_code=422)

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    token = mint_capability_token()
    task_id = write_manual_task(
        token,
        {
            "description": description, "title": req.title, "owner": req.owner,
            "due_date": req.due_date, "priority": req.priority, "status": req.status,
            "tag": req.tag, "progress_note": req.progress_note,
            "project_id": req.project_id, "reminder_date": req.reminder_date,
        },
        todo_path, settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
    )
    return JSONResponse({"task_id": task_id, "status": "created"})


class TaskPatchRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    owner: str | None = None
    due_date: str | None = None
    priority: str | None = None
    status: str | None = None
    project_id: str | None = None
    institution: str | None = None
    tag: str | None = None
    progress_note: str | None = None
    reminder_date: str | None = None
    # owner_type deliberately not exposed here yet -- P2 hasn't landed the
    # owner_type vocabulary/validation, so there's nothing meaningful for a
    # human to pick in the edit UI yet. update_task_status's allow-list
    # already covers it so this field can be added here later without
    # touching that function again.


_VALID_TASK_STATUSES = {"todo", "in_progress", "done", "blocked"}


def _task_to_detail_dict(item) -> dict:
    """Full field set for GET /api/tasks/{id} and the side-panel edit UI --
    a superset of _item_to_dict (cli/review_apply.py), which only serves the
    narrower conflict-reporting use case in apply_reviewed_update."""
    return {
        "id": item.id, "title": item.title, "description": item.description,
        "done": item.done, "owner": item.owner, "owner_type": item.owner_type,
        "confidence": item.confidence, "due_date": item.due_date,
        "reminder_date": item.reminder_date, "session_id": item.session_id,
        "priority": item.priority, "status": item.status, "source": item.source,
        "project_id": item.project_id, "institution": item.institution,
        "tag": item.tag, "progress_note": item.progress_note, "evidence": item.evidence,
        "comments": item.comments or [], "attachments": item.attachments or [],
    }


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """Read-only lookup for the task detail side panel -- no capability
    token needed (read, not write)."""
    from mcp_server.todo import parse_todo

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    # Bug fix: parse_todo reparses the whole of todo.md -- offloaded the same
    # way get_briefing/sync_calendar_endpoint are, so a large todo.md or lock
    # contention elsewhere doesn't freeze the single-worker event loop for
    # every task-detail click.
    todo_file = await asyncio.to_thread(parse_todo, todo_path)
    item = next((i for i in todo_file.items if i.id == task_id), None)
    if item is None:
        return JSONResponse({"error": f"No task with id '{task_id}'."}, status_code=404)
    return JSONResponse(_task_to_detail_dict(item))


@app.patch("/api/tasks/{task_id}")
async def patch_task(task_id: str, req: TaskPatchRequest):
    from cli.capability import mint_capability_token
    from cli.review_apply import update_task_status

    if req.status is not None and req.status not in _VALID_TASK_STATUSES:
        return JSONResponse({"error": f"status must be one of {sorted(_VALID_TASK_STATUSES)}"}, status_code=422)
    # Bug fix: priority had no allow-list check here, unlike create_manual_task
    # -- an arbitrary string (or "") could be PATCHed straight into todo.md's
    # priority field with no validation.
    if req.priority is not None and req.priority not in {"HIGH", "MEDIUM", "LOW"}:
        return JSONResponse({"error": "priority must be one of HIGH/MEDIUM/LOW."}, status_code=422)
    if req.title and len(req.title) > 200:
        return JSONResponse({"error": "title must be at most 200 characters."}, status_code=422)
    # Bug fix: `if req.description and ...` is a falsy check, so
    # description="" skipped this length check entirely and then passed
    # update_task_status's `is not None` guard, silently blanking the task's
    # description with zero validation -- unlike create_manual_task, which
    # requires and strips a non-empty description. A provided (non-None)
    # description must be non-empty after stripping here too; None still
    # means "leave the description untouched", per PATCH's partial-update
    # semantics.
    description_update = req.description
    if req.description is not None:
        description_update = req.description.strip()
        if not description_update:
            return JSONResponse({"error": "description must not be empty."}, status_code=422)
        if len(description_update) > 500:
            return JSONResponse({"error": "description must be at most 500 characters."}, status_code=422)
    if req.owner and len(req.owner) > 200:
        return JSONResponse({"error": "owner must be at most 200 characters."}, status_code=422)
    if req.project_id and len(req.project_id) > 100:
        return JSONResponse({"error": "project_id must be at most 100 characters."}, status_code=422)
    if req.tag and len(req.tag) > 50:
        return JSONResponse({"error": "tag must be at most 50 characters."}, status_code=422)
    if req.progress_note and len(req.progress_note) > 200:
        return JSONResponse({"error": "progress_note must be at most 200 characters."}, status_code=422)

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    updates = {
        "title": req.title, "description": description_update, "owner": req.owner,
        "due_date": req.due_date, "priority": req.priority, "status": req.status,
        "project_id": req.project_id, "institution": req.institution, "tag": req.tag,
        "progress_note": req.progress_note, "reminder_date": req.reminder_date,
    }
    token = mint_capability_token()
    try:
        # Bug fix: update_task_status acquires a FileLock with a multi-second
        # configurable timeout (config/settings.toml's lock_timeout_seconds)
        # around a synchronous parse+rewrite of todo.md. Called directly, a
        # contended lock (e.g. a background apply in progress) blocked the
        # whole single-worker event loop for up to that timeout -- offloaded
        # to a thread, matching this diff's own hardening of calendar-sync/
        # briefing elsewhere in this file.
        await asyncio.to_thread(
            update_task_status,
            token, task_id, updates, todo_path,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    updated_fields = [k for k, v in updates.items() if v is not None]
    return JSONResponse({"task_id": task_id, "updated_fields": updated_fields})


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """Soft delete: marks status="deleted" and keeps the record in todo.md
    for history/audit, rather than removing the line."""
    from cli.capability import mint_capability_token
    from cli.review_apply import update_task_status

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    token = mint_capability_token()
    try:
        await asyncio.to_thread(
            update_task_status,
            token, task_id, {"status": "deleted"}, todo_path,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"task_id": task_id, "status": "deleted"})


@app.post("/api/tasks/{task_id}/duplicate")
async def duplicate_task_endpoint(task_id: str):
    from cli.capability import mint_capability_token
    from cli.review_apply import duplicate_task

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    token = mint_capability_token()
    try:
        clone = await asyncio.to_thread(
            duplicate_task,
            token, task_id, todo_path,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"task_id": clone.id, "status": "created", "duplicate_of": task_id})


class TaskCommentRequest(BaseModel):
    text: str
    author: str | None = None


@app.post("/api/tasks/{task_id}/comments")
async def add_task_comment_endpoint(task_id: str, req: TaskCommentRequest):
    from cli.capability import mint_capability_token
    from cli.review_apply import add_task_comment

    text = req.text.strip()
    if not text or len(text) > 1000:
        return JSONResponse({"error": "text must be non-empty and at most 1000 characters."}, status_code=422)

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    token = mint_capability_token()
    try:
        item = await asyncio.to_thread(
            add_task_comment,
            token, task_id, req.author, text, todo_path,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"task_id": task_id, "comments": item.comments})


_TASK_ATTACHMENT_SUFFIXES = {".pdf", ".pptx", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".xlsx"}


@app.post("/api/tasks/{task_id}/attachments")
async def add_task_attachment_endpoint(task_id: str, file: UploadFile = File(...)):
    """Same size cap and extension allowlist as /api/context/upload -- task
    attachments are a separate concept and separate storage location
    (data/task_attachments/<task_id>/) from meeting-context attachments
    (data/meetings/), even when a task originated from a meeting: a task's
    attachments are about that task, not a copy of everything the source
    meeting saw."""
    from cli.capability import mint_capability_token
    from cli.review_apply import add_task_attachment

    filename = Path(file.filename or "upload").name
    suffix = Path(filename).suffix.lower()
    if suffix not in _TASK_ATTACHMENT_SUFFIXES:
        return JSONResponse(
            {"error": f"Unsupported file type '{suffix}'. Allowed: {sorted(_TASK_ATTACHMENT_SUFFIXES)}"},
            status_code=400,
        )

    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File exceeds the 50MB limit."}, status_code=413)

    attachments_dir = Path(settings.paths.data_dir) / "task_attachments" / task_id
    attachments_dir.mkdir(parents=True, exist_ok=True)
    dest_path = attachments_dir / filename
    if dest_path.exists():
        # Bug fix: uploading two different files with the same name to one
        # task used to silently overwrite the first file's bytes on disk
        # while todo.md still ended up with two separate attachment records
        # both pointing at the same (now-wrong) path -- the first upload's
        # content was permanently lost with no error surfaced. Disambiguate
        # with a short random suffix before the extension instead.
        from uuid import uuid4
        stem, suffix_ext = Path(filename).stem, Path(filename).suffix
        filename = f"{stem}-{uuid4().hex[:8]}{suffix_ext}"
        dest_path = attachments_dir / filename
    await asyncio.to_thread(dest_path.write_bytes, content)

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    token = mint_capability_token()
    relative_path = str(Path("task_attachments") / task_id / filename)
    try:
        item = await asyncio.to_thread(
            add_task_attachment,
            token, task_id, filename, relative_path, todo_path,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
        )
    except KeyError as exc:
        dest_path.unlink(missing_ok=True)
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"task_id": task_id, "attachments": item.attachments})


# ── Todo complete ──────────────────────────────────────────────────────────────

class CompleteRequest(BaseModel):
    task_id: str

@app.post("/api/todo/complete")
async def complete_task(req: CompleteRequest):
    """Mark a task done.

    Bug fix: this endpoint used to write data/todo.md directly (a bare
    `write_text`, no FileLock, no CapabilityToken, no atomic write) -- a
    second, ungated path to the project's most-protected file, alongside
    PATCH /api/tasks/{id} which goes through all three via
    update_task_status(). Now delegates to that same function instead, so
    the two "mark done" code paths share one write-safety mechanism and one
    behavior: both now set status="done" (not just the done=True flag),
    where this endpoint previously left status untouched. The request/
    response shape is unchanged (still {"task_id": ...} in, {"status": ...}
    out) so the existing frontend call site (completeTask() in app.js) needs
    no changes."""
    from cli.capability import mint_capability_token
    from cli.review_apply import update_task_status

    todo_path = Path(settings.paths.data_dir) / "todo.md"
    token = mint_capability_token()
    try:
        await asyncio.to_thread(
            update_task_status,
            token, req.task_id, {"status": "done"}, todo_path,
            settings.concurrency.lock_path, settings.concurrency.lock_timeout_seconds,
        )
    except KeyError:
        return {"status": "not_found"}
    return {"status": "success"}
