# Meeting Agent — Architecture v2 Specification
**Date:** 2026-07-01  
**Status:** ✅ Implemented (2026-07-01) — see `docs/architecture.md`'s v2 section for a
summary of what landed and a few deliberate deviations from the literal spec below
(e.g. the extraction JSON contract stayed additive rather than diverging per type, to
preserve backward compatibility with the existing pipeline). A follow-up bug-fix pass
is tracked separately in `docs/claude_cli_bugfix_01.md`.  
**Author:** System Architecture Review

---

## 1. Requirements Mapping

| # | Requirement | Architecture response |
|---|---|---|
| R1 | Complete offline, privacy-preserving | All new components use only pip-installable pure-Python libraries (pdfplumber, python-pptx, rank-bm25). Zero new network calls at runtime. |
| R2 | Fast and lightweight | GPU used for Whisper + LLM. Document context summarised before injection (bounded to 1,000 tokens). Chunked extraction parallelisable. BM25 cache added. |
| R3 | Good UI/UX | Three type-segregated hub views replace generic dashboard. Type auto-detected from slug. Context upload panel integrated per meeting type. |
| R4 | Three call types | IS Call, Project/Consortium Meeting, Seminar each get dedicated extraction prompt, MoM template, and UI hub. |
| R5 | MoM + action points + todo with dates | Type-aware MoM templates. `due_date` field already in `TodoItem`. `target_date` alias exposed in UI. |
| R6 | Calendar & mail sync for context | Calendar event matcher (time-overlap) links recording ↔ Outlook event. Mail body extractor (COM, offline) fetched by date+subject match. |
| R7 | Transcript upload | New `POST /api/upload/transcript` endpoint + `meeting-agent import-transcript` CLI command. Parses .txt/.vtt/.srt/.json. Injects at TRANSCRIBED state, bypassing RECORDING/STOPPED. |
| R8 | Max 3h + document context (PPT/PDF) | Chunked extraction pipeline handles up to ~3h at qwen2.5-7B ctx-size. PDF/PPTX ingested offline via pdfplumber/python-pptx, summarised, stored as `.doc_context.txt`. |

---

## 2. State Machine Changes

Existing transitions are **unchanged**. New entry point added for transcript upload:

```
RECORDING → STOPPED → TRANSCRIBED → EXTRACTED → PROPOSED → REVIEWED → APPLIED
                ↑
    IMPORT (new, skips RECORDING/STOPPED, starts at TRANSCRIBED)
```

New state metadata fields (non-breaking additions):
- `meeting_type`: `"is-call"` | `"project-meeting"` | `"seminar"`
- `calendar_event_id`: Outlook event EntryID (optional, set by event matcher)
- `whisper_model`: model size used for this session (for audit trail)
- `chunk_count`: number of chunks used in extraction (if > 1)
- `doc_context_files`: list of uploaded document filenames

---

## 3. File Artefact Additions

All new files sit alongside existing ones under `data/meetings/<session_id>.*`:

| File | Content | When created |
|---|---|---|
| `.type` | One of: `is-call`, `project-meeting`, `seminar` | At recording start or import |
| `.doc_context.txt` | LLM-summarised text from uploaded PDF/PPTX (≤1,000 tokens) | After document upload |
| `.mail_context.txt` | Outlook mail body matched to this session (plain text) | After mail enrichment |
| `.mom.md` | Formatted Minutes of Meeting (type-aware template) | Written by extraction layer |
| `.chunk_N.json` | Per-chunk extraction result (N = 0, 1, 2…) | During chunked extraction |

`data/meetings/<session_id>.summary.md` now contains the **synthesis** of all chunks.  
`data/meetings/<session_id>.actions.json` now contains the **merged, deduplicated** action items.

---

## 4. Meeting Type Detection

Priority order (first match wins):

1. **Explicit UI selection** — user picks type from dropdown before recording
2. **Slug prefix** — `is-call-*` → IS Call; `seminar-*` → Seminar; everything else → Project Meeting
3. **Calendar event category** — if an Outlook event is matched and its category field is set
4. **Default** — Project Meeting

Auto-detection is stored in `.type` at recording start and can be overridden in the Needs Review UI before apply.

---

## 5. MoM Templates

### 5.1 — IS Progress Call (`is-call`)

```markdown
## Daily Progress Log — {YYYY-MM-DD}

**Session:** {session_id}  
**Duration:** {duration}  
**Call type:** IS Progress Review  

### Progress Reported
{bullet list of work completed since last call}

### New Targets & Instructions
{bullet list of targets given by IS, each with a due date if mentioned}

### Blockers & Issues Raised
{bullet list of blockers or concerns discussed}

### Action Items
| # | Task | Owner | Target Date |
|---|------|-------|-------------|
{table rows}

### Continuation Context (for next IS call)
{1-paragraph summary for session chaining — injected into next call's prompt}
```

### 5.2 — Project / Consortium Meeting (`project-meeting`)

```markdown
## Minutes of Meeting

**Project / Work Package:** {extracted or user-supplied}  
**Date:** {YYYY-MM-DD}  
**Time:** {HH:MM} – {HH:MM}  
**Attendees:** {list from diarisation or context}  
**Chaired by:** {if identifiable}  

### Agenda
{items, if provided in context}

### Discussion Summary
{section per agenda item or topic}

### Decisions Made
{numbered list — each decision is a complete, standalone sentence}

### Action Items
| # | Task | Assigned to | Deadline | Priority |
|---|------|-------------|----------|----------|
{table rows}

### Next Meeting
{date/time if mentioned, else TBD}

### Documents Referenced
{list of uploaded PDFs/PPTs with their summarised key points}
```

### 5.3 — Seminar / Knowledge Sharing (`seminar`)

```markdown
## Seminar Notes — {title}

**Date:** {YYYY-MM-DD}  
**Speaker(s):** {names if identifiable}  
**Topic / Title:** {extracted}  
**Duration:** {duration}  

### Abstract / Overview
{2–3 sentence summary of what was presented}

### Key Concepts Introduced
{bullet list — each bullet is a self-contained concept}

### Notable Insights & Quotes
{direct or near-direct quotes worth preserving}

### Open Questions Raised
{questions asked during Q&A or left unresolved}

### Follow-up Reading / References
{papers, tools, or resources mentioned}

### Action Items (if any)
{only if explicit actions were assigned — seminars often have none}
```

---

## 6. Chunked Extraction Pipeline

Used when transcript token count > 5,000 tokens (~35 minutes of speech at 130 wpm).

```
TranscriptionResult
    │
    ▼
chunk_transcript(segments, chunk_tokens=5000, overlap_tokens=400)
    │
    ├─► chunk_0: segments[0:N0]   → LLM → ChunkResult(summary, action_items)
    ├─► chunk_1: segments[N0-overlap:N1] → LLM → ChunkResult
    ├─► ...
    └─► chunk_K: segments[NK-overlap:end] → LLM → ChunkResult
    │
    ▼
merge_chunks(chunk_results)
    │  • Deduplicate action items by semantic similarity (exact description match first,
    │    then fuzzy match via difflib.SequenceMatcher > 0.85 threshold)
    │  • Concatenate per-chunk summaries chronologically
    │
    ▼
synthesis_pass(merged_summary, meeting_type)
    │  • One final LLM call: "Given these per-section summaries, produce the final MoM"
    │  • Uses the type-specific MoM template as the output schema
    │
    ▼
write artefacts: .mom.md, .summary.md (synthesis), .actions.json (merged)
```

**Token budget (qwen2_5_7b_gguf, ctx-size 8192):**

| Component | Tokens |
|---|---|
| System prompt (type-specific) | ~600 |
| Context (todo.md + doc_context + mail_context + chain) | ~1,200 |
| Chunk text (input) | ~5,000 |
| Output (actions + summary per chunk) | ~400 |
| Buffer | 992 |
| **Total** | **8,192** |

**Worst-case latency (3-hour meeting on RTX 5090, qwen2.5-7B-Q4):**

| Step | Calls | Time each | Total |
|---|---|---|---|
| Document context summarisation | 2–4 | ~8s | ~25s |
| Chunk extraction (6 chunks) | 6 | ~25s | ~150s |
| Synthesis pass | 1 | ~30s | ~30s |
| **Total** | | | **~3.5 min** |

---

## 7. Document Context Ingestion

### 7.1 Supported formats

| Format | Library | Notes |
|---|---|---|
| PDF | `pdfplumber` | Page-by-page text extraction; tables as text |
| PPTX | `python-pptx` | Title + body text from each slide |
| DOCX | `python-docx` | Paragraph text |
| TXT | built-in | Direct read |

### 7.2 Processing pipeline

```
Upload file → detect format → extract raw text
    ↓
chunk_text(raw_text, chunk_tokens=2000)
    ↓
for each chunk:
    LLM: "Summarise this section of the meeting document in 3–5 bullet points."
    ↓
concatenate summaries (≤1,000 tokens total)
    ↓
write data/meetings/<session_id>.doc_context.txt
```

The 1,000-token cap is enforced by truncating summary bullets if concatenated output exceeds it. This is a deliberate trade-off: complete coverage of a 200-slide PPT is less valuable than keeping the extraction context window clear for the actual transcript.

### 7.3 New API endpoint

```
POST /api/context/upload
Content-Type: multipart/form-data
Fields:
  - session_id: str   (required; session must exist)
  - file: UploadFile  (PDF, PPTX, DOCX, TXT)

Response:
  - {"status": "processed", "session_id": ..., "summary_tokens": ..., "filename": ...}
```

---

## 8. Transcript Upload / Import

### 8.1 Supported formats

| Format | Parser | Notes |
|---|---|---|
| `.json` | Native Whisper JSON | Direct use as TranscriptionResult |
| `.vtt` | WebVTT parser (stdlib regex) | Timestamp + text extraction |
| `.srt` | SRT parser (stdlib regex) | Sequence + timestamp + text |
| `.txt` | Plain text | No speaker labels; single "Speaker" |

### 8.2 State injection point

```
POST /api/upload/transcript
    ↓
parse → TranscriptionResult
    ↓
write_transcript() → .md + .json artefacts
    ↓
create_session(state=STOPPED) if not exists
    ↓
transition(STOPPED → TRANSCRIBED)
    ↓
asyncio.create_task(run_extraction_only(session_id))
    (skips recording; starts directly from agent-run)
```

### 8.3 New CLI command

```
meeting-agent import-transcript \
    --session-id "project-review-20260701-100000" \
    --file "/path/to/transcript.vtt" \
    --type project-meeting \
    [--whisper-model base]
```

---

## 9. Mail Body Context Extraction

### 9.1 Matching logic

```python
# cli/mail_sync.py (new)
def fetch_mail_context(session_id: str, session_start_dt: datetime, subject_hint: str) -> str | None:
    """
    Query Outlook COM for mail items within ±24h of session_start_dt whose
    subject contains any token from subject_hint (tokenised, ≥4 chars, case-insensitive).
    Returns the body of the best-matching mail item (highest subject token overlap),
    or None if no match found with overlap ≥ 0.3.
    """
```

### 9.2 Privacy constraint

Mail body is stored only in `data/meetings/<session_id>.mail_context.txt`. It is never uploaded, never sent to any network endpoint, and is subject to the same TTL sweep rules as audio files if the session fails before APPLIED. A user-facing privacy notice is shown in the UI when mail context is fetched.

---

## 10. Calendar Event Matching

### 10.1 Matching algorithm

```python
def match_calendar_event(session_start: datetime, session_end: datetime, calendar_cache: Path) -> dict | None:
    """
    Load data/calendar.json (populated by existing teams_sync.py COM call).
    Find event E where:
        overlap = max(0, min(session_end, E.end) - max(session_start, E.start))
        overlap / max(session_duration, E.duration) >= 0.5   (≥50% overlap)
    Return the event with the highest overlap ratio, or None.
    """
```

### 10.2 Stored metadata

```json
{
    "calendar_event_id": "00000000ABC123...",
    "calendar_subject": "Project Review — Work Package 3",
    "calendar_start": "2026-07-01T10:00:00",
    "calendar_organiser": "Prof. Smith"
}
```

---

## 11. Per-Session Whisper Model Override

### 11.1 CLI

```
meeting-agent process --session-id X --whisper-model large-v3
```

### 11.2 Web UI

Dropdown in recording controls: **Fast (base)** | **Balanced (small)** | **Accurate (large-v3)**. Stored in session metadata. Default: from `settings.toml`.

### 11.3 Trade-off

| Model | VRAM | WER (English) | Speed (RTX 5090) |
|---|---|---|---|
| base | 1 GB | ~8% | ~10× real-time |
| small | 2 GB | ~5% | ~6× real-time |
| large-v3 | 6 GB | ~2.5% | ~2× real-time |

large-v3 is recommended for seminars and clinical contexts; base is sufficient for IS calls where vocabulary is domain-specific but speaker is known.

---

## 12. UI / UX Redesign

### 12.1 Navigation structure

```
Sidebar
├── Dashboard (renamed from home — shows daily briefing + IS call quick-start)
├── IS Call Hub         ← NEW dedicated view
├── Project Meetings    ← NEW dedicated view
├── Seminars            ← NEW dedicated view
├── Past Meetings       (existing, now type-filtered)
├── Tasks               (existing)
└── Settings            ← NEW (Whisper model, context upload, privacy)
```

### 12.2 IS Call Hub (critical path for daily use)

```
┌─────────────────────────────────────────────────────────┐
│  IS CALL HUB                               [Start Call] │
├──────────────────────┬──────────────────────────────────┤
│  Yesterday's Targets │  Today's Progress                │
│  (from last IS call) │  (auto-filled after today's call)│
├──────────────────────┴──────────────────────────────────┤
│  IS Call History (chained, newest first)                │
│  [2026-07-01 09:00]  Review call — 3 actions ✓         │
│  [2026-06-30 09:30]  Review call — 2 actions ✓ 1 open  │
└─────────────────────────────────────────────────────────┘
```

One-tap start: bypasses title prompt, uses `is-call-{YYYYMMDD}-{HHMMSS}` slug automatically.

### 12.3 Context Upload Panel

Shown as a pre-meeting card when starting a Project Meeting or Seminar:

```
┌──────────────────────────────────────────────────────┐
│  Pre-Meeting Context                                  │
│  ┌────────────────────────────────────────────┐      │
│  │  Drop PDF or PPTX here (or click to browse)│      │
│  └────────────────────────────────────────────┘      │
│  Agenda / Notes (free text):                         │
│  ┌────────────────────────────────────────────┐      │
│  │                                            │      │
│  └────────────────────────────────────────────┘      │
│  [ Fetch mail context for this meeting ]  ← COM btn  │
└──────────────────────────────────────────────────────┘
```

### 12.4 Highlight with Note (enhanced)

Current: Highlight records only a timestamp.  
New: Highlight opens a small inline text field (optional, ≤80 chars) so the user can annotate the moment ("decision on caching layer", "IS gave target: 15% by Friday").

Stored as `{"timestamp": "...", "note": "...", "segment_offset_seconds": 142.3}` (segment offset computed by comparing wall-clock timestamp against recording start time).

---

## 13. Implementation Roadmap

### Phase 1 — Critical Fixes (1 day) — MUST land first
- C1: Replace `subprocess.run` in `run_pipeline` with `asyncio.create_subprocess_exec`
- C2: Add `esc()` XSS helper to `app.js`, apply at all innerHTML interpolation sites
- S1: Fix `lock_path` hardcode in `process` command
- S2: Add `trust_env=False` to `_wait_for_llm_ready`
- R8 (print → logger): Replace all `print()` calls in `run_pipeline`
- M1: Update logo from "Nemotron" to "Meeting Agent"

### Phase 2 — Core Extraction Features (2–3 days)
- Meeting type system: `.type` file, auto-detection, `create_session` metadata
- Type-aware extraction prompts in `mcp_server/tools/extraction.py`
- MoM templates: three template functions returning formatted markdown
- Chunked extraction: `transcribe/chunker.py` + updated `extract_action_items`
- Per-session Whisper model override in `process` command

### Phase 3 — Context Enrichment (2 days)
- `cli/doc_ingest.py`: PDF/PPTX/DOCX → text → LLM summary → `.doc_context.txt`
- `POST /api/context/upload` endpoint in `cli/web.py`
- `cli/mail_sync.py`: Outlook COM mail body fetcher + matcher
- Calendar event matcher: `cli/calendar_matcher.py`
- Wire all three into `extract_action_items` context assembly

### Phase 4 — Transcript Import (1 day)
- VTT/SRT/TXT parsers in `transcribe/import_parsers.py`
- `meeting-agent import-transcript` CLI command
- `POST /api/upload/transcript` endpoint
- Tests for all four formats

### Phase 5 — UI Redesign (2–3 days)
- New sidebar nav items (IS Call Hub, Project Meetings, Seminars, Settings)
- IS Call Hub view with one-tap start and chained history
- Pre-meeting context upload panel (file drop + free text + mail fetch)
- Highlight-with-note inline text field
- Type selector dropdown in recording controls
- Bulk accept/reject in Needs Review tab
- Whisper model selector in Settings view
- MoM preview in review modal (formatted, type-specific)
- Export button (copy as markdown / download .md) in detail modal

### Phase 6 — Polish (1 day)
- BM25 mtime cache (`cli/search.py`)
- Type-filtered search (filter by meeting type in Past Meetings)
- `switchTab` data-driven refactor (`app.js`)
- Signal handler context manager (`cli/main.py`)
- `_paths(settings)` helper (`cli/main.py`)
- Calendar card render deduplication (`app.js`)
- Remove `_pid_is_alive` duplication → shared `concurrency/utils.py`

---

## 14. New Dependencies

All pip-installable, pure Python, zero network at runtime:

```toml
# pyproject.toml additions
"pdfplumber>=0.10",          # PDF text extraction
"python-pptx>=0.6",          # PPTX text extraction
"python-docx>=1.1",          # DOCX text extraction
"python-multipart>=0.0.9",   # FastAPI file upload support
```

`rank-bm25` is already installed (added in Piece 2).  
`pyannote.audio` remains optional (diarisation, `[diarisation]` extra).

---

## 15. Invariants That Must Not Change

These are non-negotiable across all phases:

1. **Zero network egress at runtime.** Every new library listed above is pure Python, offline. The mail/calendar COM calls are local IPC, not network. `POST /api/context/upload` writes to disk only.
2. **Human-in-the-loop gate.** `data/todo.md` is only written by `apply_reviewed_update` gated by `CapabilityToken`. Chunked extraction produces chunks and a merged draft — it does not auto-apply. The user still reviews and applies.
3. **Plain-file storage.** No SQLite, no Postgres, no vector DB. New artefacts are files with new extensions alongside existing ones.
4. **`capability.py` docstring stays current.** Any new `mint_capability_token()` call site must be named in the docstring.
5. **State machine transitions via `transition()` only.** Import flow uses `create_session(STOPPED)` + `transition(STOPPED→TRANSCRIBED)` — it does not write `.json` state directly.
