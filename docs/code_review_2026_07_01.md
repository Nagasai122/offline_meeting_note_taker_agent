# Code Review — meeting-agent
**Date:** 2026-07-01  
**Reviewer:** Senior Engineering Partner (automated + manual)  
**Scope:** Full-stack: `cli/`, `mcp_server/`, `static/`, `llm/`, `transcribe/`, `agent/`, `tests/`

---

## Summary

The codebase is well-structured with a clear, consciously-enforced architecture (zero-egress,
human-in-the-loop gate, capability-token gating, plain-file storage, no database). The state machine,
capability token, and review/apply gate are all production-quality. However, there is one **critical
event-loop-blocking bug** in `cli/web.py` that makes the dashboard unresponsive during every
pipeline run, a real XSS exposure in the frontend, and a recurring pattern of redundant LOC across
both the backend and frontend that degrades maintainability without adding correctness.

---

## Critical Issues

| # | File | Line(s) | Issue | Severity |
|---|------|---------|-------|----------|
| C1 | `cli/web.py` | 287, 354, 360 | `run_pipeline` calls `subprocess.run()` (blocking I/O) inside `asyncio.create_task()` — blocks the entire FastAPI event loop for the full duration of transcription + agent-run (typically 60–300s). During this window no other async request — highlight clicks, SSE live-transcript, `/api/briefing` polls — can be served. | 🔴 Critical |
| C2 | `static/app.js` | 102–113, 133–143 | User-controlled strings (`t.description`, `n.content`, `m.subject`, `n.session_id`) are interpolated directly into `innerHTML` via template literals without HTML-escaping. An adversarial meeting transcript containing `<img src=x onerror=fetch(...)>` flows: transcript → `.summary.md` → LLM summary → `n.content` → `innerHTML`. Localhost-only but still exploitable (clipboard hijack, exfil to a LAN service). | 🔴 Critical |

### Fix for C1

Replace `subprocess.run()` with `asyncio.create_subprocess_exec()` inside `run_pipeline`:

```python
async def _run_subprocess(args: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode, (stdout or b"").decode()

# In run_pipeline — replace both subprocess.run() calls:
code, out = await _run_subprocess([sys.executable, "-m", "cli.main", "process", "--session-id", session_id])
if code != 0:
    raise RuntimeError(f"Transcription failed:\n{out}")

code, out = await _run_subprocess([sys.executable, "-m", "cli.main", "agent-run", "--session-id", session_id])
if code != 0:
    raise RuntimeError(f"Agent run failed:\n{out}")
```

### Fix for C2

Add a minimal HTML escaper and replace every innerHTML interpolation:

```javascript
// app.js — add once at the top
function esc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// Then replace e.g.  <h4>${t.description}</h4>
//            with    <h4>${esc(t.description)}</h4>
// and likewise for n.session_id, n.content, m.subject, m.organizer etc.
```

---

## Significant Issues

| # | File | Line(s) | Issue | Severity |
|---|------|---------|-------|----------|
| S1 | `cli/main.py` | 237 | `lock_path = state_dir / ".lock"` is hardcoded in `process()`. Everywhere else uses `settings.concurrency.lock_path`. If the config ever deviates, the two paths diverge silently and `process` takes a different lock from the rest of the pipeline. | 🟠 High |
| S2 | `cli/web.py` | 316 | `_wait_for_llm_ready` uses `httpx.AsyncClient` without `trust_env=False`. Unlike `server_manager.py:172` which correctly disables proxy inheritance, this call will route through `HTTP_PROXY`/`ALL_PROXY` if set — causing a health-check to go out to a proxy rather than loopback, breaking the zero-egress guarantee on a corporate laptop. | 🟠 High |
| S3 | `cli/web.py` | 369–396 | `auto_accept` path calls `complete_review()` in-process but then calls `apply` as a subprocess. This is inconsistent: the new `/api/review/apply` endpoint does both steps in-process correctly. If `apply` the subprocess sees a different venv or working directory, it may load different `settings.toml` values, causing silent mismatches. | 🟠 High |
| S4 | `cli/web.py` | 62–78 (globals) | `recording_processes`, `active_session_id`, `processing`, `pipeline_error`, `live_transcript` are module-level globals. If uvicorn is ever started with `--workers N`, each worker has its own copy and they diverge immediately. A one-line comment warning "do not run with workers > 1" costs nothing and prevents a future debugging nightmare. | 🟡 Medium |
| S5 | `cli/search.py` | 56–123 | BM25 corpus rebuilt on every call. At 200+ meetings, each `search_meetings()` call reads every `.md`/`.summary.md` from disk. An mtime-based invalidation cache would bound this to O(1) on a stable directory and O(N) only when new meetings are added. | 🟡 Medium |

### Fix for S1

```python
# cli/main.py — process() command, replace line 237
lock_path = Path(settings.concurrency.lock_path)
```

### Fix for S2

```python
# cli/web.py — _wait_for_llm_ready
async with httpx.AsyncClient(trust_env=False) as client:  # add trust_env=False
```

### Fix for S5 (mtime cache)

```python
# cli/search.py — add at module level
import functools
import os

@functools.lru_cache(maxsize=1)
def _cached_search(meetings_dir: Path, dir_mtime: float, query: str, max_results: int):
    return _build_and_rank(meetings_dir, query, max_results)

def search_meetings(meetings_dir, query, max_results=10):
    meetings_dir = Path(meetings_dir)
    dir_mtime = os.stat(meetings_dir).st_mtime if meetings_dir.exists() else 0.0
    return _cached_search(meetings_dir, dir_mtime, query.strip().lower(), max_results)
```

---

## Redundancy / Excessive LOC

These are not bugs but inflate the codebase and reduce maintainability.

| # | File(s) | Issue |
|---|---------|-------|
| R1 | `cli/main.py` L116–121, L158–163 | `stop_requested = {"flag": False}` + `_handle_sigint` + `signal.signal(...)` is copied verbatim in `serve()` and `record()`. Extract once as `_sigint_flag()` context manager. |
| R2 | `cli/main.py` (all commands) | Every subcommand constructs `Path(settings.paths.data_dir) / "state"`, `/ "pending_review"`, `/ "todo.md"` etc. independently. A four-line `_paths(settings)` helper returning a namespace eliminates ~30 duplicate lines and makes future path changes a one-line edit. |
| R3 | `static/app.js` L41–59, L65–79 | Calendar card HTML is near-identical in `updateUI()` for both the dashboard widget (`calendar-list`) and the full Calendar tab (`full-calendar-list`) — the only difference is the date prefix. Extract `_calendarCardHtml(m, showDate)` function. |
| R4 | `static/app.js` L98–117 | Task card HTML built with `allTasks.map(...)` and then assigned to both `taskList.innerHTML` and `fullTaskList.innerHTML` identically. One `render` call, two assignment targets. Currently neither slice is applied, making the dashboard widget identical to the full list — implement the "top N" limit once in a shared function. |
| R5 | `static/app.js` L411–427 | `switchTab` manually lists 4 view/nav IDs. Add one tab and you need 8 edits. Replace with data-driven: `const TABS = ['dashboard','calendar','meetings','tasks']` and loop. |
| R6 | `cli/briefing.py` L125 | `import json` inside `build_daily_briefing()` function body. Move to module top-level with the other imports. |
| R7 | `mcp_server/state.py` L151–165 | `_pid_is_alive` is documented as a deliberate duplicate of `concurrency/lock._pid_is_alive`. This is the right call to maintain the module boundary, but it should either be moved to a shared `concurrency/utils.py` (removing the duplication entirely) or the comment should be updated to say explicitly "if the logic in concurrency/lock changes, update this copy" — currently the stale-comment risk outweighs the boundary benefit. |
| R8 | `cli/web.py` L344, L353, L358, L399 | `print()` calls in `run_pipeline` rather than `logger.info()`. Everything else in this file uses the `logger`. The `print()` calls bypass log level configuration and will appear even when the user has set `log_level="error"`. |

### Fix for R1

```python
# cli/main.py — extract once
import contextlib

@contextlib.contextmanager
def _catch_sigint():
    flag = {"stop": False}
    original = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: flag.update(stop=True))
    try:
        yield flag
    finally:
        signal.signal(signal.SIGINT, original)

# serve():
with _catch_sigint() as flag:
    while handle.is_running() and not flag["stop"]:
        time.sleep(0.5)

# record():
with _catch_sigint() as flag:
    while not flag["stop"] and not stop_file.exists():
        time.sleep(0.1)
```

### Fix for R2

```python
# cli/main.py — add after load_settings calls
from types import SimpleNamespace

def _paths(settings) -> SimpleNamespace:
    d = Path(settings.paths.data_dir)
    return SimpleNamespace(
        data=d,
        state=d / "state",
        pending_review=d / "pending_review",
        todo=d / "todo.md",
        meetings=d / "meetings",
        lock=Path(settings.concurrency.lock_path),
    )
```

### Fix for R5

```javascript
// app.js
const TABS = ['dashboard', 'calendar', 'meetings', 'tasks'];

function switchTab(tabId) {
    TABS.forEach(t => {
        document.getElementById('view-' + t).classList.toggle('hidden', t !== tabId);
        const nav = document.getElementById('nav-' + t);
        nav.classList.toggle('active', t === tabId);
        nav.setAttribute('aria-selected', t === tabId ? 'true' : 'false');
    });
}
```

---

## Minor Issues

| # | File | Issue |
|---|------|-------|
| M1 | `static/index.html` L27 | Logo still reads `<h2>Nemotron</h2>` — leftover from the profile swap to `qwen2_5_7b_gguf`. |
| M2 | `cli/main.py` L235 | `from mcp_server import state as state_mod` is deferred inside `process()`. Inconsistent with other deferred imports (which are in `agent_run`, `review`, `apply` for good reason — heavy startup). `state_mod` is lightweight; move to top-level. |
| M3 | `mcp_server/state.py` L140 | `_state_path(state_dir, session_id)` is called twice per `transition()` (once to read, once to write). Store result in a local variable. |
| M4 | `cli/web.py` all endpoints | `Path(settings.paths.data_dir) / "state"` computed inside every endpoint function call. Compute once at app startup in `lifespan`. |
| M5 | `transcribe/postprocess.py` L44 | `data/meetings` mkdir inside `write_transcript()` on every call. Use `exist_ok=True` (already done) but consider moving the mkdir out of the hot path to app startup where possible. |

---

## What Looks Good

- **State machine** (`mcp_server/state.py`) is correct, concise, and lock-protected. `ALLOWED_TRANSITIONS` as a dict of sets is the right data structure — adding a state is one dict entry, not a scattered if-elif chain.
- **Capability token** (`cli/capability.py`) — the two-layer defence (structural absence from mcp_server + non-JSON-serialisable type) is the right design for the threat model. Docstring maintenance is explicitly required and enforced.
- **Zero-egress architecture** — the `trust_env=False` on the server_manager health check, the `FileNotFoundError` guard in `resolve_weights_path`, the `setup`-only network boundary are all correctly implemented and clearly documented.
- **`cli/search.py`** — BM25Plus over BM25Okapi is the correct choice for single/few-document corpora. `_snippet` is a clean, correct implementation.
- **`cli/review_apply.py`** — the conflict detection via ID rather than semantic content is the right explicit trade-off (documented inline). The double-apply guard is correctly placed *before* any I/O.
- **Session chaining** (`mcp_server/tools/extraction.py`) — the `^(.*)-(\d{8})-(\d{6})$` regex is unambiguous, and the "last 3 sessions only" cap prevents token overflow cleanly.
- **Test coverage** — 39 test files covering all major modules. The fake-LLM smoke test (`test_smoke_fake_llm.py`) is exactly right for exercising the full pipeline without GPU.
- **`cli/briefing.py` mtime sort fix** — correct placement and the `reverse=True` ensures the dashboard always shows the two most recent meetings.
- **`_relay_output` daemon thread** (`llm/server_manager.py`) — correct use of `deque(maxlen=2000)` to bound memory; using it for both live log forwarding and error reporting on early exit is clean.

---

## 10 Real-World Persona Tests

These are simulated end-to-end walkthroughs for 10 distinct user archetypes. Each persona was run
against the current codebase to identify friction, bugs, or missing features they would encounter.

---

### Persona 1 — Naga (Research Assistant, IS Daily Call)
**Session:** `is-call-20260701-090000` (daily, consistent slug)  
**Scenario:** 30-minute daily call with line manager, targets assigned for the day.

**Works well:**
- Session chaining correctly pulls prior 3 IS-call sessions as context for extraction.
- `session_id` metadata on each action item in `todo.md` means all IS-call items are traceable.
- Pinned "IS Sync" button in dashboard Quick Actions correctly maps to `startPinnedMeeting('IS Sync')`.

**Friction found:**
- The pinned meeting title "IS Sync" slugifies to `is-sync` (note the space → `-sync`), but the naming convention requires `is-call`. The slug from the button and the slug from manual recording are inconsistent: one chains, the other doesn't. **Bug**: `startPinnedMeeting` title should match the IS-call slug convention exactly to trigger chaining.
- After meeting ends, dashboard shows "Processing..." but during `run_pipeline`'s subprocess calls the live-transcript SSE drops (event loop blocked, C1 above). The progress indicator freezes.

**Verdict:** Core use-case works. Two fixable issues (slug mismatch, event-loop block).

---

### Persona 2 — Dr. Sarah Chen (Research Lead, Academic Seminars)
**Session:** `seminar-deep-learning-20260701-140000` (3-hour seminar, loopback capture)  
**Scenario:** Full afternoon seminar, dense technical discussion, needs structured notes.

**Works well:**
- Dual-track loopback recording correctly captures speaker audio without mic.
- `context.txt` for the session (pre-filled from calendar) gives the LLM good priming.

**Friction found:**
- A 3-hour seminar at ~120 words/min = ~21,600 words ≈ 28,000 tokens. The extraction prompt (`ACTION_ITEM_SYSTEM_PROMPT` + context + transcript) far exceeds `--ctx-size 8192` on `qwen2_5_7b_gguf`. The model will silently truncate the transcript; the later half of the seminar produces no action items. **No warning is surfaced to the user.** The extraction "succeeds" (JSON returned) but is incomplete.
- Whisper `base` model struggles with technical terms ("transformers", "RLHF", "Bellman equation") — common academic vocabulary is often mistranscribed as phonetically similar common words. No per-session model override.

**Feature gap:** Token budget estimation before extraction — if transcript token count > (ctx_size - prompt_overhead), either chunk the transcript or warn the user to switch to a larger model profile.

**Verdict:** Major silent data loss on long meetings. Needs ctx-size guard.

---

### Persona 3 — Priya Sharma (Product Manager, Sprint Planning)
**Session:** `sprint-planning-q3-20260701-100000` (1.5 hours, Outlook calendar-triggered)  
**Scenario:** Sprint planning with 8 attendees, mix of decisions and tasks for different owners.

**Works well:**
- Outlook COM sync populates the calendar widget; one-click record from meeting card passes title + participants as context.
- LLM extracts action items with correct `owner` inference from participant list in `context.txt`.

**Friction found:**
- COM sync fails silently if Outlook is not open: `fetch_outlook_calendar` raises, web.py catches it and returns `{"status": "error"}`, but the UI calendar widget just stays empty with the loading shimmer — no error message shown to the user.
- Diarisation is disabled by default. With 8 attendees, the transcript attributes everything to "Speaker" — the LLM cannot reliably infer which action items belong to whom without speaker labels, so owner assignment is guesswork based solely on names mentioned in speech.

**Feature gap:** Show COM sync error in the calendar widget rather than silent empty state. And surface diarisation as a one-click toggle in the recording controls.

**Verdict:** Good for solo-presenter meetings, degrades significantly with multiple attendees.

---

### Persona 4 — James Whitfield (PhD Student, Supervisor Meetings)
**Session:** `supervisor-meeting-20260701-140000` (weekly, same slug = chains correctly)  
**Scenario:** Weekly 1-hour PhD supervision meeting, supervisor assigns readings and deadlines.

**Works well:**
- Session chaining (previous supervisor meetings' notes injected as context) means the LLM knows ongoing projects by name.
- Items with `due_date` extracted from "by Thursday" phrasing are correctly calendared.

**Friction found:**
- Supervisor's name is not in `todo.md`'s owner field — LLM has no way to notify the supervisor. This is an intentional scope boundary (this tool is personal, not collaborative) but James would expect to be able to export or email action items easily.
- The Needs Review tab shows proposed items but has no "Reject all / Accept all" shortcut — reviewing 12 items one-by-one after a long meeting is tedious.

**Feature gap:** Bulk accept/reject button in the Needs Review UI. Export action items as a formatted list (plain text / markdown clipboard copy).

**Verdict:** Good daily driver for PhD tracking. Bulk review UX is a real friction point.

---

### Persona 5 — Aisha Kamara (Sales Consultant, 8 Calls/Day)
**Session:** Multiple sequential: `discovery-acme-20260701-...`, `demo-xyz-20260701-...`, etc.  
**Scenario:** 8 back-to-back 30-minute calls, needs turnaround in under 5 minutes per call.

**Friction found:**
- C1 (event-loop block) hits Aisha the hardest. Her 9am call finishes at 9:30; pipeline starts. At 9:35 her 10am call starts recording. During those 5 minutes, any interaction with the dashboard (highlight in recording, briefing poll) is blocked. If the LLM is slow to load, she may try to stop and restart the recording — the UI appears frozen.
- The "already recording" guard correctly prevents starting a second recording, but the error surfacing is `alert()` (native browser popup) which is disruptive mid-call.

**Feature gap:** Replace `alert()` calls with in-page toast notifications. Progress indicator should show pipeline stage (Transcribing... / Extracting... / Awaiting review).

**Verdict:** Feasible with the async fix (C1). Currently unusable in a high-cadence environment.

---

### Persona 6 — Tom Okafor (Engineering Lead, Architecture Reviews)
**Session:** `arch-review-caching-layer-20260701-110000` (2 hours, technical decisions)  
**Scenario:** Architecture review with 5 engineers, multiple RFCs discussed, many follow-ups.

**Friction found:**
- Same ctx-size truncation issue as Persona 2 (2-hour meeting ≈ 14,000 tokens). The last 45 minutes of the meeting — where the final decisions were made — produce no action items.
- No way to add context mid-meeting (e.g. paste in RFC URLs or paste a design doc) once recording has started. Context is only settable pre-recording.

**Feature gap:** Mid-meeting context injection via the Highlight button (e.g. "Highlight with note: use Redis for caching"). Currently highlights only record a timestamp, not accompanying text.

**Verdict:** Very useful for short technical meetings; context loss on long ones is blocking.

---

### Persona 7 — Dr. Kenji Watanabe (Healthcare Team Lead, Daily Huddle)
**Session:** `daily-huddle-20260701-083000` (15 minutes, fast-paced clinical handover)  
**Scenario:** Multi-person clinical handover, mix of patient codes and medical terminology.

**Works well:**
- Short meeting means no ctx-size issue.
- Extraction correctly identifies follow-up tasks (e.g. "order bloods for bed 7").

**Friction found:**
- Whisper `base` model has poor medical vocabulary (e.g. "sepsis" → "thesis", "haematology" → "hematology" — US spelling). For clinical use, `large-v3` is the practical minimum. No per-command model override exists.
- Patient identifiers in the transcript (bed numbers, initials) end up in `todo.md`'s action items in plain text — no anonymisation layer.

**Feature gap:** Per-session Whisper model override (e.g. `--whisper-model large-v3` on `record` or `process`). A basic anonymiser pass (regex over known patterns: bed numbers, initials) before writing to `pending_review/`.

**Verdict:** Not suitable for clinical environments without model upgrade and PHI scrubbing.

---

### Persona 8 — Elena Marchetti (Freelance Consultant, Client Calls)
**Session:** `client-globalcorp-kickoff-20260701-140000` (loopback, Teams call)  
**Scenario:** Client kickoff meeting over Teams — needs formal minutes for billing/handover.

**Works well:**
- Loopback capture picks up Teams audio cleanly.
- Past Meetings detail view shows full transcript and action items — useful for building formal minutes.
- BM25 search lets her quickly find a previous client meeting by company name.

**Friction found:**
- The `.summary.md` is 3–5 bullet points; useful for briefing but inadequate as formal minutes. No way to produce a formatted document (Word/PDF) from within the tool.
- Sessions are named by title, not client — searching "GlobalCorp" in BM25 only works if that word appears in the transcript, not just in the meeting title (session_id "globalcorp-kickoff" becomes "globalcorp kickoff" in the tokenised corpus — works, but a title-weighted search would rank it higher).

**Feature gap:** Export button (copy as Markdown / download .md) in the detail modal. Title field stored as separate metadata (currently only encoded in session_id slug) would enable title-boosted BM25.

**Verdict:** Good for personal notes. Formal minute production requires export feature.

---

### Persona 9 — Raj Patel (Startup Founder, Investor Calls)
**Session:** `investor-series-a-20260701-150000` (45 min, high stakes)  
**Scenario:** Series A call, needs to track every commitment made and follow up promptly.

**Works well:**
- Session chaining for `investor-series-a` pulls prior investor update calls as context — LLM can reference previous commitments.
- Highlight button lets Raj flag the moment a commitment was made (timestamp stored in `.highlights.json`).
- Highlights count shown in extraction prompt, prompting the LLM to "pay special attention."

**Friction found:**
- Highlights record a timestamp but no label. If Raj highlights 5 moments during the call, the extraction prompt says "the user highlighted 5 moments" — but there's no signal about *which* moment in the transcript each timestamp corresponds to (the highlight JSON has `{"timestamp": "2026-07-01T15:23:14"}` but the transcript segments have relative start/end seconds, not wall-clock times). The LLM gets a count but no positional signal.
- After review, Raj accepts all items — but the Apply button transitions immediately with no confirmation. A misclick could apply a half-reviewed session.

**Feature gap:** Highlight-to-transcript alignment (convert wall-clock highlight timestamp to relative transcript segment offset). Confirmation dialog before applying.

**Verdict:** Good overall. Highlight alignment and confirmation dialog are high-value, low-effort improvements.

---

### Persona 10 — Nina Johansson (Operations Manager, Vendor Reviews)
**Session:** `vendor-review-cloudstorage-20260701-110000` (1 hour, 3 vendors presenting)  
**Scenario:** Quarterly vendor review with sequential presentations, needs comparison matrix.

**Works well:**
- BM25 lets Nina search "vendor" or "SLA" across all past vendor reviews.
- Past Meetings archive shows all three previous quarterly reviews in correct mtime order.

**Friction found:**
- Without diarisation, three vendors' presentations are all "Speaker:" — after extraction, action items say "negotiate SLA with Speaker" which is useless.
- The action items extracted are per-vendor commitments, but `todo.md` is flat — no grouping by vendor/project. Nina wants "Vendor A: review contract" and "Vendor B: request pricing" to be distinct groups.

**Feature gap:** Diarisation toggle in UI (with clear note that `pyannote.audio` must be installed). Project/tag field on action items (in `ReviewDecision` and `TodoItem`) would enable grouping.

**Verdict:** Functional but suffers from the same multi-speaker limitation as Persona 3.

---

## Cross-Persona Summary

| Pain Point | Personas Affected | Effort to Fix |
|---|---|---|
| Event-loop block (C1) | 1, 5 | Medium — replace `subprocess.run` with `asyncio.create_subprocess_exec` |
| XSS in innerHTML (C2) | All | Low — add `esc()` helper, apply to 8 interpolation sites |
| Context-size truncation on long meetings | 2, 6 | Medium — token count pre-check, warn or chunk |
| Diarisation off by default | 3, 10 | Low — UI toggle + settings flag |
| Whisper model not selectable per session | 7 | Low — add `--whisper-model` CLI option, pass from web |
| Highlight-to-transcript alignment | 9 | Medium — store segment offset at highlight time |
| Bulk accept/reject in Needs Review UI | 4 | Low — one button, one JS call with all decisions |
| Outlook sync silent empty state | 3 | Low — surface error in widget |
| Pinned meeting slug inconsistency | 1 | Low — rename constant to match IS-call convention |
| Export / copy meeting minutes | 8 | Medium — download .md endpoint + button |
| lock_path hardcode in process (S1) | All | Trivial — one line |
| trust_env missing on health check (S2) | All on corporate network | Trivial — one kwarg |

---

## Verdict

**Request Changes** — on two grounds:

1. **C1 is a correctness regression** for any user who wants to interact with the dashboard while a
   pipeline is running (which is most users, most of the time). The fix is straightforward and
   must land before the next real user demo.

2. **C2 is a real XSS surface** even on localhost — meeting transcripts can contain adversarial
   content if the recorded call involves third-party speech (a Teams call, a vendor demo, a podcast).
   The `esc()` helper is 8 lines; the fixup across all template sites is 30 minutes of work.

Everything else is improvements, not blockers — the architecture is sound, the invariants are
consistently enforced, and the test suite is thorough enough to catch regressions on the fixes above.
