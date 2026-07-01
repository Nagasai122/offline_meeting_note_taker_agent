# Claude CLI Implementation Prompt — Meeting Agent v2
**Use with:** `claude` (Claude Code CLI) in the `D:\meeting-agent` directory  
**Generated:** 2026-07-01  
**Covers:** Phases 1–7 from `docs/architecture_v2.md`

Paste the entire block below as a single prompt to Claude Code:

---

```
You are implementing the Meeting Agent v2 upgrade described in docs/architecture_v2.md.
Work through the phases in strict order — do not start Phase 2 until Phase 1 tests pass.
All code must be production-quality: type-annotated, no bare except clauses, no print() calls
in library code, no shell=True in subprocess calls.

The project root is D:\meeting-agent. Run all commands in that directory.
The active Python environment has the meeting-agent package installed in editable mode.

===========================================================================
PHASE 1 — CRITICAL FIXES (no new features, must land first)
===========================================================================

--- FIX C1: Async event-loop block in run_pipeline (CRITICAL) ---

File: cli/web.py
Locate the `run_pipeline` async function. It calls `subprocess.run(...)` (a blocking
call) directly inside an async function that is started via `asyncio.create_task()`.
This blocks the entire FastAPI event loop for the full duration of transcription + agent
run — no HTTP requests can be served while it is running.

Replace EVERY `subprocess.run(...)` call inside `run_pipeline` with
`await asyncio.create_subprocess_exec(...)` using `asyncio.subprocess.PIPE` for stdout
and stderr, and `await proc.communicate()` or streaming with `proc.stdout.readline()`.

Steps:
1. Read the entire run_pipeline function carefully first.
2. Identify each subprocess.run call and map its args to the asyncio equivalent.
3. Replace each one. Keep identical argument lists — only the call mechanism changes.
4. Ensure all await expressions are correct (this is already an async function).
5. Also replace any subprocess.run calls in helper functions called from run_pipeline
   if those helpers are also called from async context.
6. Replace all print(...) calls in run_pipeline and its helpers with logger.info(...).
   The logger is already configured at module level in cli/web.py.
7. After changes, grep -n "subprocess.run" cli/web.py to confirm zero matches remain.
8. grep -n "print(" cli/web.py to confirm zero print() calls remain in run_pipeline scope.

--- FIX C2: XSS in app.js (CRITICAL) ---

File: static/app.js
User-supplied strings from API responses are interpolated directly into innerHTML
without HTML-escaping. An attacker who can inject content into a meeting transcript,
subject line, or action item description can run arbitrary JavaScript in the user's
browser.

Steps:
1. Add this helper near the top of app.js (before any other functions):

   function esc(str) {
     if (str == null) return '';
     return String(str)
       .replace(/&/g, '&amp;')
       .replace(/</g, '&lt;')
       .replace(/>/g, '&gt;')
       .replace(/"/g, '&quot;')
       .replace(/'/g, '&#39;');
   }

2. Search for all innerHTML assignments that embed API data. The known locations are:
   - t.description (task/action items)
   - n.content (notes / highlights)
   - m.subject (calendar event subjects)
   - session title / summary strings in Past Meetings rendering
   - Search result snippets
   - Any other places where API-derived data is placed inside template literals that
     are assigned to innerHTML.
   Grep for "innerHTML" to get the full list.

3. Wrap every such interpolation in esc(): e.g. `${t.description}` → `${esc(t.description)}`
   For URLs, also validate they start with https:// or are relative paths before use in href.

4. Do NOT wrap values that are themselves HTML tag strings (e.g. '<span class="...">');
   those are your own template strings, not user data.

5. After changes, grep -n "innerHTML" static/app.js and manually review each remaining
   assignment to confirm no unescaped user data remains.

--- FIX S1: Lock path hardcoded in process command ---

File: cli/main.py
The `process` command (around line 237) has:
    lock_path = state_dir / ".lock"
hardcoded. Every other call site uses settings.concurrency.lock_path.
Replace that one line with:
    lock_path = Path(settings.concurrency.lock_path)
Verify: grep -n "lock_path" cli/main.py — all occurrences should reference settings.

--- FIX S2: trust_env=False missing from httpx client ---

File: cli/web.py
The `_wait_for_llm_ready` function creates an httpx.AsyncClient for health-checking
the llama-server. Without trust_env=False, httpx will read proxy env vars (http_proxy,
https_proxy, HTTP_PROXY etc.) which could cause the local health-check request to be
routed through a proxy, timing out or leaking metadata.

Find the AsyncClient instantiation inside _wait_for_llm_ready and add trust_env=False:
    async with httpx.AsyncClient(trust_env=False, ...) as client:

--- FIX M1: Logo typo ---

File: static/index.html
Find the line containing <h2>Nemotron</h2> and change it to:
    <h2>Meeting Agent</h2>

--- VERIFY PHASE 1 ---

Run: python -m pytest tests/ -x -q (if tests directory exists)
Run: python -c "import ast, pathlib; ast.parse(pathlib.Path('cli/web.py').read_text())"
Run: python -c "import ast, pathlib; ast.parse(pathlib.Path('cli/main.py').read_text())"
Confirm: grep -c "subprocess.run" cli/web.py returns 0
Confirm: grep -c "innerHTML" static/app.js followed by manual check for unescaped vars

===========================================================================
PHASE 2 — MEETING TYPES + MoM TEMPLATES + CHUNKED EXTRACTION
===========================================================================

--- STEP 2.1: Meeting type system ---

Create file: mcp_server/meeting_type.py

Content:
"""
Meeting type enumeration and auto-detection from session slug.
Three types are supported; each drives a distinct extraction prompt and MoM template.
"""
from __future__ import annotations
import re
from enum import Enum


class MeetingType(str, Enum):
    IS_CALL = "is-call"
    PROJECT = "project-meeting"
    SEMINAR = "seminar"


_SLUG_PATTERNS = [
    (re.compile(r'^is-call-'), MeetingType.IS_CALL),
    (re.compile(r'^seminar-'), MeetingType.SEMINAR),
]


def detect_meeting_type(session_id: str) -> MeetingType:
    """Return MeetingType from session_id slug prefix. Default: PROJECT."""
    for pattern, meeting_type in _SLUG_PATTERNS:
        if pattern.match(session_id):
            return meeting_type
    return MeetingType.PROJECT


def load_meeting_type(type_file_path) -> MeetingType:
    """Read .type file; fall back to slug-based detection if file absent."""
    from pathlib import Path
    p = Path(type_file_path)
    if p.exists():
        raw = p.read_text().strip()
        try:
            return MeetingType(raw)
        except ValueError:
            pass
    # fallback: detect from filename stem (session_id)
    return detect_meeting_type(p.stem.split('.')[0])


--- STEP 2.2: Type-aware extraction prompts ---

File: mcp_server/tools/extraction.py
Read the file first to understand the current ACTION_ITEM_SYSTEM_PROMPT and
extract_action_items function.

Add three type-specific system prompts below the existing one:

IS_CALL_SYSTEM_PROMPT — instructs LLM to extract:
- progress_reported: list of completed items since last call
- new_targets: list of {task, due_date} dicts
- blockers: list of blocker descriptions
- action_items: list of {id, description, assignee, due_date, priority}
- continuation_summary: 1-paragraph summary for next session context

PROJECT_SYSTEM_PROMPT — instructs LLM to extract:
- attendees: list of names
- agenda_items: list of topics
- decisions: list of decision strings
- action_items: list of {id, description, assignee, due_date, priority}
- next_meeting: date/time string or null

SEMINAR_SYSTEM_PROMPT — instructs LLM to extract:
- speaker: name string or null
- topic: topic title string
- key_concepts: list of concept strings
- notable_insights: list of insight strings
- open_questions: list of question strings
- references: list of reference strings
- action_items: list (usually empty for seminars)

Modify extract_action_items to accept an optional meeting_type: MeetingType parameter.
Select the correct system prompt based on meeting_type.
Import MeetingType from mcp_server.meeting_type.

--- STEP 2.3: MoM template writer ---

Create file: mcp_server/mom_writer.py

Implement three functions:
- write_is_call_mom(session_id, extracted_data, output_path) → None
- write_project_mom(session_id, extracted_data, output_path) → None
- write_seminar_mom(session_id, extracted_data, output_path) → None

Each function takes the parsed extraction JSON (dict) and formats it using the
templates defined in docs/architecture_v2.md Section 5. Write the result to
output_path as a .mom.md file.

Also implement:
- write_mom(session_id, extracted_data, meeting_type, meetings_dir) → Path
  which dispatches to the correct writer based on meeting_type and returns
  the path of the written .mom.md file.

--- STEP 2.4: Chunked extraction pipeline ---

Create file: transcribe/chunker.py

Implement:
- estimate_tokens(text: str) -> int
  Approximation: len(text.split()) * 1.35 (words to tokens for English technical speech)

- chunk_transcript(
      segments: list[dict],   # list of {start, end, text} dicts
      chunk_tokens: int = 5000,
      overlap_tokens: int = 400,
  ) -> list[list[dict]]
  Groups segments into chunks where each chunk's estimated token count ≤ chunk_tokens.
  Overlap: the last N segments of chunk K are prepended to chunk K+1 where those
  segments' total tokens ≈ overlap_tokens.
  Returns a list of segment groups (each group is a list of segment dicts).

- merge_action_items(chunk_results: list[dict]) -> list[dict]
  Takes per-chunk extracted action_items lists.
  Deduplicates by description similarity: use difflib.SequenceMatcher; if two items
  have ratio > 0.85, keep the one with a non-null due_date (or the first if both null).
  Assigns a fresh UUID to each surviving item (uuid4).
  Returns merged, deduplicated list.

File: mcp_server/tools/extraction.py
Modify extract_action_items to:
1. Check if transcript token count > 5000 (using estimate_tokens from chunker.py).
2. If not: proceed as before (single LLM call).
3. If yes: call chunk_transcript, run extract_action_items on each chunk sequentially
   (NOT in parallel — single llama-server instance, one context at a time), write each
   chunk's raw result to data/meetings/<session_id>.chunk_N.json, call merge_action_items,
   run one final synthesis_pass LLM call to produce the unified summary, write .mom.md
   via mom_writer.write_mom, return merged result.
   
The synthesis_pass LLM call uses a prompt like:
  "You are given {N} sequential summaries from sections of the same meeting.
   Produce one coherent {meeting_type} MoM following the template.
   Summaries: {summaries_json}"

--- STEP 2.5: Per-session Whisper model override ---

File: cli/main.py
In the `process` command, add an optional argument:
    --whisper-model: str, default None (uses settings value when None)

Pass it through to the transcription call. If provided, override the model for
this session only and store it in session metadata:
    transition(..., whisper_model=whisper_model_used)

File: cli/web.py
Add a `whisper_model` optional field to the StartRecordingRequest body (default: null).
Pass it through to the process pipeline. When null, the default from settings is used.

--- VERIFY PHASE 2 ---

Run: python -c "from mcp_server.meeting_type import detect_meeting_type, MeetingType; assert detect_meeting_type('is-call-20260701-090000') == MeetingType.IS_CALL; assert detect_meeting_type('seminar-llm-20260701-140000') == MeetingType.SEMINAR; assert detect_meeting_type('project-review-20260701-100000') == MeetingType.PROJECT; print('meeting_type detection OK')"

Run: python -c "from transcribe.chunker import chunk_transcript, estimate_tokens; segs=[{'start':i,'end':i+30,'text':'word '*50} for i in range(0,3600,30)]; chunks=chunk_transcript(segs); print(f'3h meeting → {len(chunks)} chunks'); assert 5 <= len(chunks) <= 9, f'unexpected chunk count {len(chunks)}'"

Run: python -c "from mcp_server.mom_writer import write_mom; print('mom_writer import OK')"

===========================================================================
PHASE 3 — DOCUMENT CONTEXT + MAIL CONTEXT + CALENDAR MATCHING
===========================================================================

--- STEP 3.1: Document context ingestion ---

Install dependencies (if not already present):
    pip install pdfplumber python-pptx python-docx python-multipart --break-system-packages

Create file: cli/doc_ingest.py

Implement:
- extract_text_from_pdf(path: Path) -> str
  Use pdfplumber. Open each page, extract text. Join pages with "\n\n".
  Handle PasswordError (encrypted PDF) → raise ValueError("PDF is encrypted").
  
- extract_text_from_pptx(path: Path) -> str
  Use python-pptx. For each slide: title text + "\n" + body text from all shapes.
  Join slides with "\n\n---\n\n".

- extract_text_from_docx(path: Path) -> str
  Use python-docx. Extract all paragraph texts. Join with "\n".

- extract_text(path: Path) -> str
  Dispatcher: checks suffix (.pdf, .pptx, .ppt [raise NotImplementedError], .docx, .doc
  [raise NotImplementedError], .txt). Raises ValueError for unsupported extensions.

- summarise_doc_context(
      raw_text: str,
      llm_call: Callable[[str, str], str],  # (system_prompt, user_text) -> response
      max_output_tokens: int = 1000,
  ) -> str
  Chunk raw_text into 2000-token chunks.
  For each chunk, call llm_call with:
    system: "Summarise the following section of a meeting document in 3–5 bullet points.
             Focus on key arguments, data, decisions, and terminology."
    user: <chunk text>
  Collect bullet-point summaries.
  Concatenate; if total exceeds max_output_tokens worth of characters, truncate to
  roughly that size (4 chars per token approximation), preserving complete bullet points.
  Return final summary string.

- ingest_document(path: Path, session_id: str, meetings_dir: Path, llm_call: Callable) -> Path
  Full pipeline: extract_text → summarise_doc_context → write to
  meetings_dir / f"{session_id}.doc_context.txt" → return that path.

File: cli/web.py
Add endpoint:
    POST /api/context/upload
    Request: multipart/form-data with fields: session_id (str), file (UploadFile)
    Validation:
      - validate_session_id(session_id) → 422 if invalid
      - Check session exists (load state) → 404 if not
      - Check file suffix in {'.pdf', '.pptx', '.docx', '.txt'} → 400 if not
      - Check file size ≤ 50MB → 413 if exceeded
    Processing:
      - Save uploaded file to tmp/<session_id>_doc_<filename> (secure filename: strip path components)
      - Call ingest_document(tmp_path, session_id, meetings_dir, llm_call)
      - Delete tmp file after processing
    Response: {"status": "processed", "session_id": ..., "filename": ..., "summary_tokens": <int>}

--- STEP 3.2: Mail context extraction ---

Create file: cli/mail_sync.py  (new; separate from calendar concerns)

Implement using win32com.client (already available if teams_sync.py works):

- fetch_mail_context(
      session_start: datetime,
      subject_hint: str,
      search_window_hours: float = 24.0,
  ) -> str | None
  
  1. Connect to Outlook.Application COM (handle COMError → return None with log warning).
  2. Get default Inbox folder.
  3. Filter items: ReceivedTime within [session_start - search_window_hours,
     session_start + search_window_hours].
  4. Tokenise subject_hint: split on spaces/hyphens/underscores, keep tokens ≥ 4 chars,
     lowercase.
  5. For each mail item, score = (number of hint tokens in item.Subject.lower()) / len(hint_tokens).
  6. Best match: highest score ≥ 0.3. If no match, return None.
  7. Extract item.Body (plain text; if empty, try item.HTMLBody stripped of tags via
     a simple regex removing <[^>]+> patterns).
  8. Truncate to 2000 characters (≈500 tokens). Return truncated body.
  
  Important: wrap entire COM interaction in try/except Exception to avoid crashing
  the pipeline if Outlook is not open or COM fails.

- save_mail_context(session_id: str, meetings_dir: Path, body: str) -> Path
  Writes meetings_dir / f"{session_id}.mail_context.txt". Returns path.

File: cli/web.py
Add endpoint:
    POST /api/context/mail
    Request body: {"session_id": str, "subject_hint": str}
    Validates session_id. Loads session to get start_time from metadata.
    Calls fetch_mail_context. If None: returns {"status": "no_match"}.
    Otherwise: calls save_mail_context. Returns {"status": "saved", "preview": first_100_chars}.

--- STEP 3.3: Calendar event matching ---

Create file: cli/calendar_matcher.py

Implement:
- match_calendar_event(
      session_start: datetime,
      session_end: datetime,
      calendar_cache_path: Path,
  ) -> dict | None
  
  Load data/calendar.json. For each event E:
    overlap = max(0, min(session_end, E_end) - max(session_start, E_start)) in seconds
    ratio = overlap / max((session_end - session_start).seconds, (E_end - E_start).seconds, 1)
  Return event with highest ratio if ratio >= 0.5, else None.
  
  The calendar.json structure must be read from the existing file to understand the
  key names used. Read data/calendar.json (if it exists) before writing this function.
  If the file does not exist, return None.

- save_calendar_match(session_id: str, state_dir: Path, lock_path: Path,
                      lock_timeout: float, event: dict) -> None
  Calls transition() with metadata_updates:
    calendar_event_id=event.get("id") or event.get("EntryID"),
    calendar_subject=event.get("subject") or event.get("Subject"),
    calendar_start=str(event.get("start") or event.get("Start")),
    calendar_organiser=event.get("organiser") or event.get("organizer") or "",

Wire calendar matching into run_pipeline in cli/web.py:
  After TRANSCRIBED state, attempt calendar match. If found, call save_calendar_match.
  This is a best-effort enrichment; if it fails (exception), log the error and continue.

--- VERIFY PHASE 3 ---

Run: python -c "from cli.doc_ingest import extract_text_from_pdf, extract_text_from_pptx, extract_text; print('doc_ingest import OK')"
Run: python -c "from cli.mail_sync import fetch_mail_context; print('mail_sync import OK')"
Run: python -c "from cli.calendar_matcher import match_calendar_event; print('calendar_matcher import OK')"

Create a minimal test PDF and PPTX:
    python -c "
from pathlib import Path
# Create a minimal PDF with pdfplumber-readable text
import pdfplumber, io

# Quick smoke test with a real PDF if available, else skip
print('Phase 3 import checks PASS')
"

===========================================================================
PHASE 4 — TRANSCRIPT IMPORT
===========================================================================

--- STEP 4.1: Transcript parsers ---

Create file: transcribe/import_parsers.py

Implement four parsers, each returning a list of segment dicts [{start, end, text}]:

- parse_whisper_json(path: Path) -> list[dict]
  Load JSON. If top-level key "segments" exists, use it directly (each has "start",
  "end", "text"). If top-level is a list, treat as segments directly.

- parse_vtt(path: Path) -> list[dict]
  Parse WebVTT format. Regex to extract timestamps (HH:MM:SS.mmm --> HH:MM:SS.mmm)
  and text blocks. Convert timestamps to float seconds.
  Speaker labels (SPEAKER_XX:) should be stripped from text.

- parse_srt(path: Path) -> list[dict]
  Parse SRT format. Regex for sequence number, timestamp line (HH:MM:SS,mmm -->
  HH:MM:SS,mmm), and text block. Convert timestamps to float seconds.

- parse_plain_text(path: Path) -> list[dict]
  Split text into paragraphs (double newline). Assign synthetic timestamps:
  start = paragraph_index * 30.0, end = start + 30.0 (30s blocks).
  Text = paragraph content.

- parse_transcript_file(path: Path) -> list[dict]
  Dispatcher on suffix: .json → parse_whisper_json, .vtt → parse_vtt,
  .srt → parse_srt, .txt → parse_plain_text. Raises ValueError for other suffixes.

- segments_to_text(segments: list[dict]) -> str
  Join all segment texts with "\n". Used for writing the .md artefact.

--- STEP 4.2: Import CLI command ---

File: cli/main.py
Add a new Typer command: `import-transcript`

Arguments:
  --session-id TEXT        [required] Session ID to use (must not already exist)
  --file PATH              [required] Transcript file (.json, .vtt, .srt, .txt)
  --type [is-call|project-meeting|seminar]   [default: project-meeting]
  --whisper-model TEXT     [default: None, stored in metadata as "imported"]

Logic:
  1. Validate --file exists and has a supported suffix → raise if not.
  2. Call create_session(state_dir, session_id, lock_path, lock_timeout,
                         initial_state=State.STOPPED,
                         meeting_type=type_value,
                         source="import",
                         whisper_model=whisper_model or "imported")
  3. Write .type file to meetings_dir.
  4. Call parse_transcript_file(file) → segments.
  5. Write segments to data/meetings/<session_id>.json (Whisper-compatible format).
  6. Write segments_to_text(segments) to data/meetings/<session_id>.md.
  7. Transition STOPPED → TRANSCRIBED.
  8. Call the same extraction pipeline as the normal process flow (reuse the function,
     do not duplicate the logic).
  9. Print "Import complete. Session: {session_id}" when done.

--- STEP 4.3: Import API endpoint ---

File: cli/web.py
Add endpoint:
    POST /api/upload/transcript
    Request: multipart/form-data with fields:
      - session_id: str (optional; if not provided, auto-generate as "import-{YYYYMMDD}-{HHMMSS}")
      - file: UploadFile (.json, .vtt, .srt, .txt only)
      - meeting_type: str (optional; default "project-meeting")
    Validation:
      - If session_id provided: validate_session_id → 422 if invalid, 409 if already exists
      - file suffix check → 400 if not supported
      - file size ≤ 50MB → 413 if exceeded
    Processing:
      - Save to tmp/<session_id>_transcript<suffix>
      - parse_transcript_file → segments
      - create_session(STOPPED) + write .type + write .md + .json
      - transition(STOPPED → TRANSCRIBED)
      - asyncio.create_task(run_extraction_only(session_id, meeting_type))
      - Delete tmp file
    Response: {"status": "importing", "session_id": ..., "segments_count": int}

--- VERIFY PHASE 4 ---

Generate a test VTT file and verify parsing:
python -c "
from pathlib import Path
from transcribe.import_parsers import parse_vtt, parse_srt, parse_plain_text

# Test VTT
vtt = Path('/tmp/test.vtt')
vtt.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:05.000\nHello, welcome to the meeting.\n\n00:00:06.000 --> 00:00:10.000\nToday we will discuss progress.\n')
segs = parse_vtt(vtt)
assert len(segs) == 2, f'Expected 2 segments, got {len(segs)}'
assert segs[0]['start'] == 1.0
assert 'Hello' in segs[0]['text']
print('VTT parse OK')

# Test SRT
srt = Path('/tmp/test.srt')
srt.write_text('1\n00:00:01,000 --> 00:00:05,000\nHello world\n\n2\n00:00:06,000 --> 00:00:10,000\nSecond line\n')
segs = parse_srt(srt)
assert len(segs) == 2
assert segs[1]['text'] == 'Second line'
print('SRT parse OK')
print('Phase 4 import parser tests PASS')
"

===========================================================================
PHASE 5 — UI REDESIGN
===========================================================================

--- STEP 5.1: New navigation items in index.html ---

File: static/index.html
Add to the sidebar nav (preserve existing tabs; add after existing items):
  <li data-tab="is-call-hub" class="nav-item">IS Call Hub</li>
  <li data-tab="project-meetings" class="nav-item">Project Meetings</li>
  <li data-tab="seminars" class="nav-item">Seminars</li>
  <li data-tab="settings" class="nav-item">Settings</li>

Add corresponding panel divs:
  <div id="is-call-hub" class="tab-panel" ...>
  <div id="project-meetings" class="tab-panel" ...>
  <div id="seminars" class="tab-panel" ...>
  <div id="settings" class="tab-panel" ...>

--- STEP 5.2: IS Call Hub panel ---

In the is-call-hub panel, implement:
1. A prominent "Start IS Call" button that calls:
     startMeeting('is-call')
   This should auto-generate the slug as `is-call-{YYYYMMDD}-{HHMMSS}` server-side.
   The button should be styled with a larger font and primary colour so it is immediately
   visible — the IS call is the most frequent action for this user.

2. A two-column layout below the button:
   Left: "Yesterday's Targets" — fetches the most recent is-call-* session's .actions.json
         and renders each action item with its due_date and completion checkbox.
   Right: "Today's Progress" — empty until today's IS call is completed; then shows today's
          extracted action items with their status.

3. A scrollable list of past IS calls (newest first) showing:
   - Date, duration, number of action items, count of completed vs open items.
   - Each row is clickable to open the detail modal.

Use the existing GET /api/briefing endpoint to get session data. Filter client-side
for sessions where session_id starts with "is-call-".
The actions data is fetched from GET /api/meetings/{session_id}.

--- STEP 5.3: Pre-meeting context upload panel ---

When the user clicks "Start Meeting" (not IS Call) and selects type Project or Seminar,
show a modal BEFORE starting the recording. This modal contains:

1. A file drop zone (drag-and-drop + click to browse):
   Accept: .pdf, .pptx, .docx, .txt
   Max size: 50MB
   On file selection: display filename + size; enable "Process Context" button.

2. A textarea for free-form agenda or notes (max 1000 chars).
   These notes are saved as a .mail_context.txt equivalent (since they are typed context,
   not mail-fetched — label as "Pre-meeting notes").

3. A "Fetch mail context" button:
   Calls POST /api/context/mail with the session's subject_hint (derived from the
   meeting title input the user typed).
   Shows a status indicator: "Found email from {date}" or "No matching email found".

4. A "Start Recording" button that:
   - Starts the recording (POST /api/record/start or the existing start endpoint)
   - If a file was selected and processed, uploads it via POST /api/context/upload
   - If agenda notes were typed, saves them
   Then proceeds to the normal recording UI.

The existing "Start IS Call" one-tap button bypasses this modal entirely.

--- STEP 5.4: Highlight with inline note ---

In the recording UI (static/app.js), when the "Highlight" button is clicked:
Current: records timestamp only.
New: after clicking, show an inline input field (≤80 chars placeholder: "Add a note…")
that appears for 8 seconds. If the user types and presses Enter (or the 8 seconds expire),
save {"timestamp": ..., "note": input_value_or_empty, "segment_offset_seconds": ...}.
The segment_offset_seconds = (Date.now() - recording_start_ms) / 1000.
Send to existing highlight save endpoint with the extended payload.

--- STEP 5.5: Type selector in recording controls ---

In the recording controls area of index.html, add a select dropdown:
  <select id="meeting-type-select">
    <option value="project-meeting">Project Meeting</option>
    <option value="is-call">IS Call (ad-hoc)</option>
    <option value="seminar">Seminar</option>
  </select>

Pass the selected value as meeting_type in the StartRecordingRequest body.

--- STEP 5.6: MoM preview in Needs Review modal ---

When the review modal opens for a session, add a "Minutes of Meeting" tab alongside
the existing action items view. Fetch the .mom.md content from:
  GET /api/meetings/{session_id}   (add mom_content field to response)
Render the MoM markdown as preformatted text (use <pre> tag, not innerHTML with
markdown parsing — this avoids XSS from the MoM content).

--- STEP 5.7: Settings panel ---

Create a simple settings panel with:
1. Default Whisper model: radio buttons — Fast (base) / Balanced (small) / Accurate (large-v3)
   Reads from GET /api/settings and saves via PATCH /api/settings.
   Implement those two endpoints in cli/web.py (read/write a subset of settings.toml fields).

2. Privacy notice about mail context:
   Static text explaining that mail bodies are stored locally only in data/meetings/
   and are never transmitted.

--- STEP 5.8: Refactor switchTab ---

File: static/app.js
Replace the current switchTab function (which manually lists tab IDs) with a
data-driven approach:
  function switchTab(tabId) {
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const panel = document.getElementById(tabId);
    const navItem = document.querySelector(`[data-tab="${tabId}"]`);
    if (panel) panel.classList.add('active');
    if (navItem) navItem.classList.add('active');
  }

--- VERIFY PHASE 5 ---

Open the web dashboard in a browser and manually verify:
1. All five new nav items are visible and clickable.
2. IS Call Hub shows "Start IS Call" button prominently.
3. Clicking Start Meeting (non-IS-call type) shows the pre-meeting modal.
4. Highlight button shows inline note field.
5. Meeting type dropdown is present in recording controls.
6. Needs Review modal shows MoM tab.
7. Settings panel shows Whisper model options.

Also verify that esc() is applied to all user-data innerHTML insertions by checking
that a test action item with description '<script>alert(1)</script>' renders as
literal text, not as an executed script.

===========================================================================
CROSS-CUTTING CONCERNS (apply throughout all phases)
===========================================================================

1. Imports: never use star imports (from x import *). All imports explicit.
2. Type annotations: all new functions and methods must have full type annotations.
3. Error handling: no bare except clauses. Catch specific exception types.
4. Logging: use the module-level logger everywhere. No print() calls in library code.
5. Tests: for any new pure-logic function (chunker.py, import_parsers.py,
   meeting_type.py, mom_writer.py), write a test function alongside it in tests/
   if that directory exists, or as a doctest in the module docstring if not.
6. Docstrings: one-line summary + Args + Returns for all public functions.
7. File writes: always write to a .tmp file first, then rename (atomic write pattern)
   to avoid partial writes leaving corrupt artefacts if interrupted.
8. Invariants: do NOT add any import of apply_reviewed_update or mint_capability_token
   in any new module under mcp_server/. These calls belong only in cli/.

===========================================================================
FINAL INTEGRATION CHECK
===========================================================================

After all phases are complete:

1. Run: meeting-agent serve
   Verify: web dashboard loads at http://localhost:8765
   Verify: all five new nav items are visible
   Verify: no JavaScript errors in browser console

2. Simulate a full pipeline with transcript import:
   - Create a 10-line .txt test transcript
   - POST to /api/upload/transcript
   - Verify session moves STOPPED → TRANSCRIBED → EXTRACTED → PROPOSED
   - Open Needs Review tab → verify the session appears
   - Accept all items → Apply → verify todo.md updated

3. Simulate document context upload:
   - If a test PDF exists: POST to /api/context/upload
   - Verify .doc_context.txt appears in data/meetings/

4. Run: grep -rn "subprocess.run" cli/ — must return zero matches (excluding comments)
5. Run: grep -rn "print(" cli/ mcp_server/ transcribe/ -- must return zero matches
   (excluding __main__ blocks and test scripts)
6. Run: python -m py_compile cli/web.py cli/main.py mcp_server/tools/extraction.py
   transcribe/chunker.py transcribe/import_parsers.py cli/doc_ingest.py
   cli/mail_sync.py cli/calendar_matcher.py mcp_server/mom_writer.py
   mcp_server/meeting_type.py
   All must exit 0.

Report back with:
- Phase completion status (PASS/FAIL per phase)
- Any files that could not be modified and why
- Any test failures with the actual vs expected values
- The final grep counts for subprocess.run and print()
- Line counts for each new file created
```
