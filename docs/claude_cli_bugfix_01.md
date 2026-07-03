# Claude CLI Bug-Fix Prompt — Batch 01
**Use with:** `claude` in `D:\meeting-agent`  
**Generated:** 2026-07-01  
**Fixes:** 5 confirmed bugs from first post-implementation run

Paste the entire block below as a single prompt to Claude Code:

---

```
You are fixing 5 confirmed bugs in the Meeting Agent codebase at D:\meeting-agent.
Work through each fix in order. Do not skip a fix because it seems minor.
Run the verification command after each fix before moving to the next.

===========================================================================
FIX 1 — LLM SERVER: ENV VAR VALIDATION + FAST-FAIL + SURFACE STARTUP LOG
===========================================================================

The symptom is: "LLM server did not become healthy within 300s".
Root cause A: LLAMA_SERVER_EXE is not set (session-scoped env var lost on terminal close).
Root cause B: If llama-server crashes on launch, the health poller waits the full 300s
              before reporting anything — the startup_log deque has the real error but
              it is never surfaced in the exception message.
Root cause C: No process-liveness check in the poll loop.

--- FIX 1.1: Validate LLAMA_SERVER_EXE at serve startup ---

File: cli/main.py (the `serve` command) or wherever the LLM server is launched.
Also read: llm/server_manager.py — this is where the actual launch happens.

Read both files before making any changes.

In the function that resolves the llama-server executable path (likely in
llm/server_manager.py), add a pre-launch validation:

```python
def _resolve_exe(settings_exe_path: str | None) -> Path:
    """Resolve llama-server executable, with clear diagnostics on failure.
    
    Priority:
    1. LLAMA_SERVER_EXE environment variable
    2. settings.toml llm.server_exe value
    3. 'llama-server' on PATH
    
    Raises FileNotFoundError with actionable message if not found.
    """
    import os, shutil
    from pathlib import Path
    
    env_override = os.environ.get('LLAMA_SERVER_EXE', '').strip()
    if env_override:
        p = Path(env_override)
        if not p.exists():
            raise FileNotFoundError(
                f"LLAMA_SERVER_EXE is set to '{env_override}' but that file does not exist.\n"
                f"Fix: check the path, or re-download llama-server.exe to D:\\llama.cpp\\\n"
                f"Then set permanently with:\n"
                f"  [System.Environment]::SetEnvironmentVariable('LLAMA_SERVER_EXE', "
                f"'{env_override}', 'User')"
            )
        return p
    
    if settings_exe_path:
        p = Path(settings_exe_path)
        if p.exists():
            return p
    
    found = shutil.which('llama-server') or shutil.which('llama-server.exe')
    if found:
        return Path(found)
    
    raise FileNotFoundError(
        "llama-server executable not found. Set LLAMA_SERVER_EXE permanently:\n"
        "  [System.Environment]::SetEnvironmentVariable("
        "'LLAMA_SERVER_EXE', 'D:\\\\llama.cpp\\\\llama-server.exe', 'User')\n"
        "Then open a new terminal and retry."
    )
```

Replace the existing executable resolution logic with a call to _resolve_exe.
This raises immediately (before any subprocess call) with a human-readable message.

--- FIX 1.2: Process-liveness check in health poll loop ---

File: cli/web.py — _wait_for_llm_ready function (or equivalent in llm/server_manager.py)

Read the function. It currently polls the /health endpoint in a loop.
Add a process-liveness check to the loop body:

```python
# Inside the polling loop, BEFORE the httpx.get call:
if process is not None and process.returncode is not None:
    # Process has already exited — no point waiting further
    log_output = "\n".join(list(startup_log)) if startup_log else "(no output captured)"
    raise RuntimeError(
        f"llama-server exited with code {process.returncode} before becoming healthy.\n"
        f"Last output:\n{log_output}\n"
        f"Common causes:\n"
        f"  - Model weights not found (run: meeting-agent setup --profile <name>)\n"
        f"  - CUDA out of memory (reduce --n-gpu-layers in settings.toml)\n"
        f"  - Corrupt download (delete models/ and re-run setup)"
    )
```

The `process` and `startup_log` variables must be accessible in this scope.
If they are not (e.g. the function signature does not accept them), refactor so
that _wait_for_llm_ready receives the process object and startup_log deque as
parameters. Read the function signature carefully first.

--- FIX 1.3: Surface startup_log in the timeout error ---

In the same _wait_for_llm_ready function, when the 300-second timeout is reached,
the current exception message says only "LLM server did not become healthy within 300s".
Change it to include the last 20 lines of startup_log:

```python
log_tail = list(startup_log)[-20:] if startup_log else []
log_text = "\n".join(log_tail) if log_tail else "(no output — process may not have started)"
raise TimeoutError(
    f"LLM server did not become healthy within {timeout_seconds}s "
    f"at {health_url}\n"
    f"Last server output:\n{log_text}\n"
    f"If you see 'model file does not exist': run meeting-agent setup --profile <name>\n"
    f"If you see 'CUDA error': check nvidia-smi and driver version\n"
    f"If no output: check LLAMA_SERVER_EXE is set (see Fix 1.1 above)"
)
```

--- FIX 1.4: Also try /v1/health as fallback ---

Some versions of llama-server expose /v1/health instead of /health, or expose both.
In _wait_for_llm_ready, try both endpoints in each poll iteration:

```python
for health_path in ['/health', '/v1/health']:
    try:
        r = await client.get(base_url + health_path, timeout=2.0)
        if r.status_code == 200:
            return  # healthy
    except (httpx.ConnectError, httpx.TimeoutException):
        pass
```

--- VERIFY FIX 1 ---

python -c "
import os
# Simulate missing env var scenario
os.environ.pop('LLAMA_SERVER_EXE', None)
from llm.server_manager import _resolve_exe
try:
    _resolve_exe(None)
    print('ERROR: should have raised FileNotFoundError')
except FileNotFoundError as e:
    assert 'SetEnvironmentVariable' in str(e), f'Missing instructions in error: {e}'
    print('Fix 1.1 PASS: clear error raised when exe not found')
"

===========================================================================
FIX 2 — DECOUPLE RECORDING/TRANSCRIPTION FROM LLM SERVER HEALTH
===========================================================================

Root cause: run_pipeline in cli/web.py waits for LLM health BEFORE starting recording
or transcription. Whisper (transcription) has no dependency on llama-server. Only the
extraction step (TRANSCRIBED → EXTRACTED) needs the LLM.

The correct pipeline order is:
  Start recording → Stop recording → Transcribe (Whisper only, no LLM needed)
  → [at this point, wait for LLM if not healthy] → Extract → Propose

--- FIX 2.1: Move LLM health gate to extraction step only ---

File: cli/web.py — run_pipeline function

Read the full run_pipeline function carefully.

Find where _wait_for_llm_ready (or equivalent) is called.
Move that call so it happens AFTER the transcription step completes and BEFORE the
extraction agent run begins. Do not call it before or during recording/transcription.

The exact restructuring depends on how the pipeline is structured. The key invariant:
  - Recording start: no LLM check
  - Recording stop: no LLM check
  - Transcription (Whisper): no LLM check
  - Pre-extraction: LLM health check here (and only here)

If the LLM fails to become healthy, transition the session to FAILED with a clear
error message: "LLM server unavailable — transcription saved at data/meetings/{session_id}.md
You can retry extraction later with: meeting-agent process --session-id {session_id} --skip-transcribe"

--- FIX 2.2: Add --skip-transcribe flag to process command ---

File: cli/main.py — process command

Add optional flag: --skip-transcribe / no-skip-transcribe (default False)

When --skip-transcribe is set:
  - Check that the session already has a .md transcript file
  - If yes: skip the Whisper step, transition directly from STOPPED → TRANSCRIBED
  - Then run extraction as normal
  - This enables recovery when transcription succeeded but extraction failed

--- VERIFY FIX 2 ---

Read the modified run_pipeline and confirm:
grep -n "_wait_for_llm_ready\|wait_for_llm\|healthy" cli/web.py

The health-check call must appear AFTER the line that transitions the session to
TRANSCRIBED, and BEFORE the line that starts the extraction agent run.

===========================================================================
FIX 3 — ADD "GENERAL / OTHER" MEETING TYPE
===========================================================================

There is no option to record ad-hoc calls with colleagues, collaborators, or external
parties who are not IS, not consortium members, and not seminar speakers.
A fourth type "general" is needed as a catch-all.

--- FIX 3.1: Add GENERAL to MeetingType enum ---

File: mcp_server/meeting_type.py

Add:
    GENERAL = "general"

In detect_meeting_type: the existing default (when no prefix matches) is already
PROJECT, but update the default to GENERAL since not every unrecognised meeting
is a project meeting. Add a new prefix for project meetings:

Updated pattern list:
    (re.compile(r'^is-call-'), MeetingType.IS_CALL),
    (re.compile(r'^seminar-'), MeetingType.SEMINAR),
    (re.compile(r'^project-'), MeetingType.PROJECT),
    # anything else → GENERAL

Default for unmatched slugs: MeetingType.GENERAL (change from PROJECT).

--- FIX 3.2: General extraction prompt ---

File: mcp_server/tools/extraction.py

Add GENERAL_SYSTEM_PROMPT:

GENERAL_SYSTEM_PROMPT = """
You are extracting structured notes from a general meeting or call.
Recording date: {recording_date_iso}

Extract the following as JSON:
{
  "summary": "2-3 sentence overview of what was discussed",
  "participants": ["list of names or roles if identifiable, else []"],
  "key_points": ["list of main discussion points"],
  "action_items": [
    {
      "id": "<uuid4>",
      "description": "<what needs to be done>",
      "assignee": "<name or role if mentioned, else null>",
      "due_date": "<ISO date if mentioned or inferable, else null>",
      "priority": "<HIGH|MEDIUM|LOW — infer from language>",
      "depends_on": "<description of blocking item if applicable, else omit>"
    }
  ],
  "decisions": ["list of decisions made, if any"]
}

Output only valid JSON. No markdown fences. No commentary outside the JSON.
"""

Wire it into the prompt-selection logic alongside the other three types.

--- FIX 3.3: General MoM template ---

File: mcp_server/mom_writer.py

Add write_general_mom(session_id, extracted_data, output_path) → None:

Template:
```
## Meeting Notes — {YYYY-MM-DD}

**Session:** {session_id}
**Duration:** {duration}
**Participants:** {participants or 'Not recorded'}

### Summary
{summary}

### Key Points
{bullet list of key_points}

### Decisions
{bullet list of decisions, or 'None recorded' if empty}

### Action Items
| # | Task | Assigned to | Due Date | Priority |
|---|------|-------------|----------|----------|
{table rows}
```

Update write_mom() dispatcher to handle MeetingType.GENERAL.

--- FIX 3.4: General type in UI dropdown ---

File: static/index.html and/or static/app.js

In the meeting type selector dropdown, add a fourth option:
    <option value="general">General / Other</option>

Make "General / Other" the default selected option (not "Project Meeting") since it
is the lowest-friction choice for ad-hoc calls. The user can explicitly select a more
specific type when they know the meeting context.

Also ensure the IS Call Hub's "Start IS Call" button hard-codes type "is-call" and
is unaffected by the dropdown selection.

--- VERIFY FIX 3 ---

python -c "
from mcp_server.meeting_type import detect_meeting_type, MeetingType
assert detect_meeting_type('is-call-20260701-090000') == MeetingType.IS_CALL
assert detect_meeting_type('seminar-llm-20260701-140000') == MeetingType.SEMINAR
assert detect_meeting_type('project-review-20260701') == MeetingType.PROJECT
assert detect_meeting_type('chat-with-alex-20260701-113000') == MeetingType.GENERAL
assert detect_meeting_type('random-20260701-100000') == MeetingType.GENERAL
print('MeetingType detection PASS — GENERAL is correct default')
"

===========================================================================
FIX 4 — CONTEXT OPTIONAL FOR PROJECT/SEMINAR + BUTTON UNRESPONSIVE
===========================================================================

Two sub-bugs:
A: The "Start Recording" button is disabled until a PDF/PPTX is uploaded, making
   context mandatory. It must be optional.
B: Even when the button appears enabled, clicking it does nothing (event listener
   not wired, or the disabled attribute is not fully cleared, or a JS error is thrown
   silently).

--- FIX 4.1: Remove mandatory file gate from Start Recording ---

File: static/app.js

Search for the code that controls the disabled state of the "Start Recording" button
in the pre-meeting modal. It will look something like:

    startRecordingBtn.disabled = !fileSelected;
or
    if (!uploadedFile) { startRecordingBtn.disabled = true; return; }

Remove or change the condition so the button is enabled as soon as:
  - The meeting type is selected (always true, since there is a default), OR
  - The description/title field has at least 1 character of text

The file upload and agenda textarea should remain in the modal as optional inputs.
Add "(optional)" label text to the file drop zone and the agenda textarea labels.

Also update any server-side validation in cli/web.py: the StartRecordingRequest
body should have no required fields beyond meeting_type (which has a default of
"general"). description/title should be optional (default to "").

--- FIX 4.2: Fix the unresponsive button click ---

This requires reading the actual event listener code for the Start Recording button.

Read static/app.js fully and find:
1. The element ID or class of the Start Recording button in the pre-meeting modal
2. Where its click event listener is attached (addEventListener or onclick)
3. What the handler function does

Common causes of an unresponsive button:
a) Event listener attached before the element exists in the DOM (timing issue):
   Fix — move the addEventListener call inside a DOMContentLoaded handler, or use
   event delegation on the document.
b) Handler throws a JS exception silently (open browser console to check):
   Fix — add try/catch in the handler and console.error the caught exception.
c) Button is inside a <form> with an implicit submit that refreshes the page:
   Fix — add event.preventDefault() at the top of the handler, or change <form>
   to <div>.
d) The disabled attribute is removed on the HTML element but a CSS pointer-events:none
   rule is still active:
   Fix — inspect the element in browser DevTools and check computed styles.

After reading the code, diagnose which cause applies and fix it.

Then add defensive error surfacing to the handler: wrap the entire click handler body
in try/catch and show any caught error in a visible error div inside the modal
(never silent failure).

--- FIX 4.3: Description field enables Start Recording ---

Currently the meeting description field (title textarea or input) requires a value
according to the placeholder text, but entering text does not enable the button.

Add an input event listener to the description field:

    descriptionInput.addEventListener('input', () => {
        startBtn.disabled = descriptionInput.value.trim().length === 0;
    });

On initial modal open: set startBtn.disabled = true.
When description has at least 1 character: set startBtn.disabled = false.
File upload remains optional and does not affect the button state.

--- VERIFY FIX 4 ---

Open the web dashboard in a browser with DevTools console open.
1. Click "Start Meeting" for a Project Meeting.
2. The modal opens.
3. WITHOUT uploading any file, type any text in the description field.
4. Confirm the Start Recording button becomes enabled.
5. Click Start Recording.
6. Confirm no JS error appears in console.
7. Confirm recording starts (recording indicator appears or an API call is made).

Also test: leave description empty → button remains disabled → correct.

===========================================================================
FIX 5 — TRANSCRIPT UPLOAD: LINK TO EXISTING CALENDAR MEETING
===========================================================================

When the user uploads a transcript from a pre-recorded meeting, they need to link
it to an existing Outlook calendar event so the session inherits the meeting's
subject, attendees, and date metadata.

--- FIX 5.1: Calendar event picker in transcript upload UI ---

File: static/index.html and static/app.js

In the "Upload Transcript" UI (wherever it currently exists — find it by searching
for the /api/upload/transcript fetch call in app.js), add:

A "Link to calendar meeting (optional)" section:
- A date input (default: today) labeled "Meeting date"
- A search/filter input labeled "Meeting subject (type to filter)"
- A <select> or scrollable list that populates from GET /api/calendar/events?date=YYYY-MM-DD
- A "Search" button that calls the endpoint and populates the list
- When the user selects an event, store the event's id in a hidden field
- The selected event's subject and start time are shown as confirmation text

If the user does not select any event, the transcript is imported without a calendar link
(existing behaviour — do not break this).

--- FIX 5.2: Calendar events search endpoint ---

File: cli/web.py

Add endpoint:
    GET /api/calendar/events
    Query params: date (str, YYYY-MM-DD format, required)
    
    Load data/calendar.json (the existing calendar cache).
    Filter events where date(event.start) == requested date.
    If no events on that date, also return events within ±1 day (to handle timezone
    differences and all-day events).
    
    Response:
    {
      "events": [
        {
          "id": "<event id>",
          "subject": "<title>",
          "start": "<ISO datetime>",
          "end": "<ISO datetime>",
          "organiser": "<name or null>"
        }
      ],
      "date": "<requested date>"
    }
    
    If data/calendar.json does not exist or is malformed: return {"events": [], "date": ...}
    Never raise a 500 — the calendar is optional enrichment.

--- FIX 5.3: Accept calendar_event_id in transcript upload ---

File: cli/web.py — POST /api/upload/transcript endpoint

Extend the request body (multipart/form-data) to include an optional field:
    calendar_event_id: str | None = None

When processing the import:
    If calendar_event_id is provided and non-empty:
        Load data/calendar.json, find the matching event by id.
        If found: pass calendar_event_id, calendar_subject, calendar_start,
                  calendar_organiser as metadata to create_session().
        If not found: log a warning, proceed without calendar link (do not error).

This ensures the session shows "Linked to: {calendar_subject}" in the Past Meetings
detail view, the same as a live-recorded session that was auto-matched.

--- FIX 5.4: Show calendar link in session detail view ---

File: static/app.js — the meeting detail modal render function

When rendering a session's detail view (GET /api/meetings/{session_id}), check if
state.metadata contains calendar_subject and calendar_start.

If present, render a "Calendar event" row in the session metadata section:
    📅 Linked calendar event: {calendar_subject} at {formatted calendar_start}

Apply esc() to calendar_subject before rendering into innerHTML.

--- VERIFY FIX 5 ---

1. Open a browser to the dashboard.
2. Navigate to the transcript upload section.
3. Set the date to today.
4. Click "Search" for calendar events.
5. Verify either events appear (if calendar.json has today's events) or an empty state
   message appears ("No meetings found for this date").
6. Upload a .txt transcript file.
7. Without selecting a calendar event: confirm import proceeds as before.
8. With a calendar event selected: confirm the session detail view shows the calendar link.

Run: python -c "
import httpx
r = httpx.get('http://localhost:8765/api/calendar/events?date=2026-07-01')
assert r.status_code == 200, f'Unexpected status: {r.status_code}'
data = r.json()
assert 'events' in data, f'Missing events key: {data}'
print(f'/api/calendar/events OK — {len(data[\"events\"])} event(s) found for 2026-07-01')
"

===========================================================================
CROSS-CUTTING: SILENT JS ERRORS
===========================================================================

After all five fixes, open the browser console (F12 → Console tab) and check for
any red errors while:
1. Loading the dashboard
2. Switching to each tab
3. Opening the pre-meeting modal
4. Clicking Start Recording (with description filled)
5. Opening the transcript upload panel

For each JS error found:
- Read the error message and line number
- Find the source in static/app.js
- Fix the underlying issue (typically: accessing a property of null/undefined,
  or calling a function before it is defined)
- Never use try/catch to swallow errors silently — always console.error them

===========================================================================
FINAL VERIFICATION
===========================================================================

Run all of these in sequence:

1. python -m py_compile cli/web.py cli/main.py mcp_server/meeting_type.py
   mcp_server/tools/extraction.py mcp_server/mom_writer.py llm/server_manager.py
   All must exit 0.

2. python -c "
   from mcp_server.meeting_type import MeetingType, detect_meeting_type
   from mcp_server.tools.loop_closure import close_prior_targets
   from cli.reminders import check_and_notify
   from llm.server_manager import _resolve_exe
   print('All imports OK')
   "

3. Start meeting-agent serve in one terminal.
   Open http://localhost:8765 in browser with DevTools console open.
   Confirm zero red errors in console on page load.

4. Start a General / Other meeting recording.
   Verify it starts without requiring any context upload.
   Stop after 10 seconds.
   Verify pipeline proceeds to TRANSCRIBED state.

5. Upload a .txt transcript with no calendar link.
   Verify session appears in Past Meetings.

6. Attempt to trigger the 300-second timeout scenario by temporarily renaming
   llama-server.exe, running meeting-agent serve, and verifying the error message
   now includes actionable instructions and the startup log. Then rename it back.

Report:
- PASS/FAIL per fix (1–5)
- Any JS errors that remain in the browser console
- The exact error message now shown for the LLM timeout scenario
- Confirmation that the General type appears in the dropdown
- Confirmation that Start Recording is enabled by description text alone
```
