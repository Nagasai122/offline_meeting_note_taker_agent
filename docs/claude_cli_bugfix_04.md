# Claude CLI Bug-Fix Prompt — Batch 04
# Session Boundary Hardening
**Use with:** `claude` in `D:\meeting-agent`
**Generated:** 2026-07-02

Paste everything below as a single prompt to Claude Code:

---

```
You are hardening session-to-session data boundaries in the Meeting Agent codebase.
No new features. Every change must be minimal and targeted.
Read each file in full before modifying it.

===========================================================================
FIX SB-1 — LLM KV CACHE: DISABLE CROSS-SESSION CACHE REUSE
===========================================================================

The llama-server (llama.cpp) maintains a KV cache between requests. Sequential
sessions sharing a common system prompt prefix will have their key-value pairs
mixed in the attention cache. Session B can be influenced by Session A's residual
cached computation.

--- FIX SB-1.1: Pass cache_prompt=false in all extraction LLM calls ---

File: Find wherever the LLM HTTP call is made for extraction.
Search: grep -rn "v1/chat/completions\|llm_call\|_call_llm\|chat_completion" llm/ agent/ mcp_server/

Read the function that constructs the request body for the LLM call.
Add "cache_prompt": false to the request JSON body for ALL extraction calls:

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "cache_prompt": False,   # ← ADD THIS: prevent KV cache reuse across sessions
    }

This is a llama.cpp-specific parameter. It instructs the server to not reuse
cached KV pairs from previous requests. It increases per-call latency slightly
(~5-10%) but eliminates cross-session context bleed.

Only apply this to extraction calls (where session isolation is critical).
Synthesis calls within the same session can still use caching for performance
(multiple chunks of the same session sharing a common prefix is safe).

To distinguish: pass a parameter `session_isolated: bool = True` to the LLM call
function. When True, add "cache_prompt": False to the payload.
Extraction calls: session_isolated=True
Synthesis/chunk calls within same session: session_isolated=False (default)

--- FIX SB-1.2: llama-server launch arg review ---

File: llm/model_profiles.py or llm/server_manager.py

Read the extra_launch_args for the active profile (qwen2_5_7b_gguf).
Verify it does NOT contain --cont-batching (continuous batching can mix KV cache
between concurrent requests — not applicable for single-user but good to confirm).

If --cont-batching is present, remove it from extra_launch_args.
If absent, add a comment confirming it is intentionally absent:
    # --cont-batching intentionally omitted: single-user, session isolation required

Also verify --parallel 1 is present or add it:
    "--parallel", "1",   # single request at a time, prevents slot mixing

--- VERIFY SB-1 ---

grep -rn "cache_prompt" llm/ agent/ mcp_server/
# Must show at least one occurrence with value False in extraction calls

python -c "
# Verify the LLM call function accepts session_isolated parameter
import ast
# Find the actual llm call file from the grep above
src = open('llm/server_manager.py').read()  # adjust if different file
ast.parse(src)
print('llm/server_manager.py parses OK')
"

===========================================================================
FIX SB-2 — GLOBAL STATE: CLEAR active_session_id ON PIPELINE END
===========================================================================

active_session_id and recording_processes are module-level globals in cli/web.py.
If a pipeline fails, these may not be cleared, causing the next session to see
stale state from the failed session.

--- FIX SB-2.1: Clear globals in pipeline finally block ---

File: cli/web.py

Read the run_pipeline function. Find where active_session_id is set and where
the pipeline ends (both success and failure paths).

Ensure that at the END of run_pipeline (success, failure, or exception), the
globals are cleaned up for this specific session:

    async def run_pipeline(session_id: str, ...) -> None:
        global active_session_id, recording_processes
        try:
            # ... existing pipeline logic ...
            pass
        except Exception as exc:
            # ... existing error handling + FAILED transition (from bugfix_02) ...
            raise
        finally:
            # Always clean up, regardless of success or failure
            if active_session_id == session_id:
                active_session_id = None
            recording_processes.pop(session_id, None)
            logger.info("Pipeline globals cleared for session %s", session_id)

The `if active_session_id == session_id` guard prevents accidentally clearing
a newer session's state if run_pipeline is somehow called concurrently.

--- FIX SB-2.2: Add session_id validation to stop endpoint ---

File: cli/web.py — the POST /api/record/stop endpoint

When a stop request arrives, verify that the session_id in the request matches
active_session_id:

    if session_id != active_session_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot stop session '{session_id}': "
                f"active session is '{active_session_id}'. "
                f"This may be a stale UI state — refresh the page."
            )
        )

This prevents a stale UI (after a page refresh) from accidentally stopping a
different session than the one currently recording.

--- VERIFY SB-2 ---

python -c "
import ast
src = open('cli/web.py').read()
ast.parse(src)
# Check that finally block exists in run_pipeline
assert 'finally' in src, 'No finally block found in cli/web.py'
assert 'active_session_id = None' in src, 'Global clear not found'
print('SB-2 globals cleanup: PRESENT')
"

===========================================================================
FIX SB-3 — todo.md CONTEXT INJECTION: BOUNDED, FILTERED, RECENCY-CAPPED
===========================================================================

The extraction prompt injects the full todo.md as context. After months of use,
this grows unbounded and injects stale/completed tasks from old sessions into
every new extraction — causing the LLM to anchor on historical items.

--- FIX SB-3.1: Filter and cap todo.md before injection ---

File: mcp_server/tools/extraction.py (or wherever todo.md is read for context)
Search: grep -rn "todo.md\|todo_path\|read_todo" mcp_server/ agent/

Find the function that reads todo.md and formats it as LLM context.
Replace the full-file read with a filtered, capped read:

    from datetime import datetime, timedelta, timezone

    def _build_todo_context(
        todo_path: Path,
        max_tokens: int = 500,
        recency_days: int = 30,
        meeting_type: str = "general",
    ) -> str:
        """Load todo.md and return a bounded, filtered context string.

        Filters:
        - Excludes items with status 'done' or 'deleted'
        - Excludes items older than recency_days (by due_date or creation date)
        - Caps output at max_tokens (approximated as max_tokens * 4 chars)
        - For IS calls: includes only items from IS call sessions or manual items
        - For project meetings: excludes IS-call-specific items

        Returns empty string if todo.md does not exist or has no matching items.
        """
        if not todo_path.exists():
            return ""

        raw = todo_path.read_text(encoding="utf-8")
        if not raw.strip():
            return ""

        # Parse items — adapt to the actual format used in todo.md
        # The format is likely markdown checklist with metadata comments
        lines = raw.splitlines()
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=recency_days)).date()

        filtered_lines = []
        char_budget = max_tokens * 4  # ~4 chars per token approximation
        chars_used = 0

        for line in lines:
            # Skip completed/deleted items
            if "status: done" in line.lower() or "status: deleted" in line.lower():
                continue
            if line.strip().startswith("- [x]"):  # checked checkbox = done
                continue

            # Skip items with due dates older than recency window
            # (look for due_date: YYYY-MM-DD in the line or nearby comment)
            import re
            date_match = re.search(r'due_date.*?(\d{4}-\d{2}-\d{2})', line)
            if date_match:
                try:
                    from datetime import date
                    item_date = date.fromisoformat(date_match.group(1))
                    if item_date < cutoff_date:
                        continue
                except ValueError:
                    pass  # unparseable date — include the item

            # Apply character budget cap
            if chars_used + len(line) > char_budget:
                filtered_lines.append(
                    f"... [{char_budget // 4} token limit reached — "
                    f"older items omitted] ..."
                )
                break

            filtered_lines.append(line)
            chars_used += len(line)

        if not filtered_lines:
            return "No open action items in the current period."

        return "## Open Action Items (last 30 days)\n\n" + "\n".join(filtered_lines)

Replace the existing todo.md context injection call with _build_todo_context().
Pass the current meeting_type so future type-filtering can be added without
changing the call sites.

--- FIX SB-3.2: Add token usage logging for context assembly ---

In the function that assembles the full LLM context (system prompt + chaining
context + todo context + doc context + transcript chunk), add a log line
showing the token budget breakdown for each session:

    logger.info(
        "Context assembly for session %s: "
        "system=%d chars, chain=%d chars, todo=%d chars, "
        "doc=%d chars, transcript=%d chars, total=%d chars (~%d tokens)",
        session_id,
        len(system_prompt), len(chain_context), len(todo_context),
        len(doc_context), len(transcript_chunk),
        total_chars, total_chars // 4,
    )

This makes context leaks immediately visible in the server log during debugging.

--- VERIFY SB-3 ---

python -c "
import tempfile
from pathlib import Path

# Create a test todo.md with old and new items
todo_content = '''## Tasks

- [ ] Fix the authentication bug <!-- meta: {\"id\": \"1\", \"due_date\": \"2025-01-01\", \"status\": \"todo\"} -->
- [x] Update documentation <!-- meta: {\"id\": \"2\", \"status\": \"done\"} -->
- [ ] Review pull request <!-- meta: {\"id\": \"3\", \"due_date\": \"2026-07-10\", \"status\": \"todo\"} -->
'''

with tempfile.TemporaryDirectory() as d:
    todo_path = Path(d) / 'todo.md'
    todo_path.write_text(todo_content)
    
    from mcp_server.tools.extraction import _build_todo_context
    result = _build_todo_context(todo_path, max_tokens=500, recency_days=30)
    
    # Old item (2025-01-01) should be excluded
    assert '2025-01-01' not in result, 'Old item leaked into context'
    # Done item should be excluded
    assert 'Update documentation' not in result, 'Done item leaked into context'
    # Recent open item should be included
    assert 'Review pull request' in result, 'Recent item incorrectly excluded'
    
    print('todo.md context filter: PASS')
    print('Result preview:', result[:200])
"

===========================================================================
FIX SB-4 — SESSION CHAINING: ENFORCE MEETING TYPE MATCH
===========================================================================

The session chaining glob uses slug prefix to find prior sessions. This is
correct for IS calls (is-call-*) but is not verified against the actual
.type file of matched sessions. A mis-named session could inject wrong context.

--- FIX SB-4.1: Verify meeting_type of chained sessions ---

File: mcp_server/tools/extraction.py — the session chaining function
Search: grep -n "prior.*session\|chain\|glob.*session\|slug" mcp_server/tools/extraction.py

Read the chaining logic. After finding candidate prior sessions via glob,
add a meeting_type verification step:

    def _load_prior_sessions(
        session_id: str,
        meetings_dir: Path,
        current_meeting_type: str,
        max_sessions: int = 3,
    ) -> list[str]:
        """Load prior session notes for chaining context.

        Only loads sessions whose .type file matches current_meeting_type.
        Falls back to slug-based inference if .type file is absent.
        Returns list of session note strings, newest first.
        """
        import re
        from mcp_server.meeting_type import MeetingType, load_meeting_type

        # Extract slug from session_id
        match = re.match(r'^(.*)-(\d{8})-(\d{6})$', session_id)
        if not match:
            return []
        slug = match.group(1)

        # Find all sessions with the same slug prefix
        candidates = sorted(
            meetings_dir.glob(f"{slug}-*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        prior_notes = []
        for candidate in candidates:
            cand_session_id = candidate.stem  # filename without extension

            # Skip the current session itself
            if cand_session_id == session_id:
                continue

            # Verify meeting type matches
            type_file = candidate.with_suffix('.type')
            try:
                candidate_type = load_meeting_type(type_file)
            except Exception:
                # If .type file is missing, infer from slug
                from mcp_server.meeting_type import detect_meeting_type
                candidate_type = detect_meeting_type(cand_session_id)

            if candidate_type.value != current_meeting_type:
                logger.debug(
                    "Skipping prior session %s for chaining: type mismatch "
                    "(%s != %s)",
                    cand_session_id, candidate_type.value, current_meeting_type,
                )
                continue

            try:
                notes = candidate.read_text(encoding="utf-8")
                prior_notes.append(notes)
            except OSError:
                continue

            if len(prior_notes) >= max_sessions:
                break

        return prior_notes

Replace the existing chaining function with this implementation.

--- VERIFY SB-4 ---

python -c "
from mcp_server.meeting_type import MeetingType
from mcp_server.tools.extraction import _load_prior_sessions
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as d:
    meetings = Path(d)
    # Create a seminar session and an IS call session with same date pattern
    seminar = meetings / 'seminar-llm-20260701-140000.md'
    seminar.write_text('Seminar notes about LLMs')
    seminar_type = meetings / 'seminar-llm-20260701-140000.type'
    seminar_type.write_text('seminar')

    iscall = meetings / 'is-call-20260701-090000.md'
    iscall.write_text('IS call notes about project')
    iscall_type = meetings / 'is-call-20260701-090000.type'
    iscall_type.write_text('is-call')

    # Request chaining for a new seminar session — should only get seminar
    prior = _load_prior_sessions(
        'seminar-llm-20260702-140000',
        meetings,
        'seminar',
        max_sessions=3,
    )
    assert len(prior) == 1, f'Expected 1 prior session, got {len(prior)}'
    assert 'Seminar notes' in prior[0], 'Wrong session type injected'
    assert 'IS call notes' not in prior[0], 'IS call leaked into seminar chaining'
    print('Session chaining type isolation: PASS')
"

===========================================================================
FIX SB-5 — FEEDBACK LOOP: FILTER REJECTIONS BY MEETING TYPE
===========================================================================

File: mcp_server/tools/extraction.py — _load_negative_examples function
Also: cli/feedback.py — record_rejection function

--- FIX SB-5.1: Store meeting_type in rejection records ---

In record_rejection(), add meeting_type to the JSON record written to rejections.jsonl:

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "item_id": item_id,
        "item_description": item_description,
        "rejection_reason": rejection_reason,
        "meeting_type": meeting_type,  # ← ADD THIS
        "quality_flags": quality_flags or [],
    }

--- FIX SB-5.2: Filter by meeting_type in _load_negative_examples ---

In _load_negative_examples(), add a meeting_type parameter and filter:

    def _load_negative_examples(
        feedback_dir: Path,
        meeting_type: str,         # ← ADD THIS PARAMETER
        max_examples: int = 3,
    ) -> str:
        jsonl = feedback_dir / "rejections.jsonl"
        if not jsonl.exists():
            return ""

        import json
        lines = jsonl.read_text().strip().splitlines()

        # Filter to same meeting type only
        matching = []
        for line in reversed(lines):  # newest first
            try:
                record = json.loads(line)
                if record.get("meeting_type") == meeting_type:
                    matching.append(record)
            except json.JSONDecodeError:
                continue
            if len(matching) >= max_examples:
                break

        if not matching:
            return ""

        examples = [
            f'- REJECTED: "{r.get(\"item_description\", \"\")[:100]}" '
            f'— {r.get(\"rejection_reason\") or \"marked incorrect\"}'
            for r in matching
        ]

        return (
            "\n## EXAMPLES OF INCORRECT EXTRACTION (DO NOT REPLICATE)\n\n"
            "These were rejected by the user in similar meetings. Avoid similar errors:\n"
            + "\n".join(examples)
            + "\n"
        )

Update the call site to pass meeting_type.

--- VERIFY SB-5 ---

python -c "
import json, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as d:
    feedback = Path(d)
    rejections = feedback / 'rejections.jsonl'
    rejections.write_text(
        json.dumps({'session_id': 's1', 'item_description': 'Seminar specific item', 'meeting_type': 'seminar', 'rejection_reason': 'wrong', 'quality_flags': []}) + '\n' +
        json.dumps({'session_id': 's2', 'item_description': 'IS call specific item', 'meeting_type': 'is-call', 'rejection_reason': 'hallucinated', 'quality_flags': []}) + '\n'
    )
    from mcp_server.tools.extraction import _load_negative_examples
    
    # IS call examples should not include seminar rejections
    result = _load_negative_examples(feedback, meeting_type='is-call')
    assert 'IS call specific item' in result, 'IS call rejection not included'
    assert 'Seminar specific item' not in result, 'Seminar rejection leaked into IS call prompt'
    print('Feedback type isolation: PASS')
"

===========================================================================
FIX SB-6 — BM25 INDEX: EXCLUDE IN-PROGRESS SESSIONS
===========================================================================

File: cli/search.py — search_meetings function (or wherever the BM25 index is built)

Read the file. Find where it globs for .summary.md or .md files to index.

Add a state filter: only index sessions that are in PROPOSED, REVIEWED, or APPLIED state.
Sessions in RECORDING, STOPPED, TRANSCRIBED, or EXTRACTED are in-progress and
their content should not appear in search results.

    from mcp_server.state import load_session_state, State

    INDEXABLE_STATES = {State.PROPOSED, State.REVIEWED, State.APPLIED}

    def _is_indexable(session_id: str, state_dir: Path) -> bool:
        """Return True only if the session has completed extraction."""
        try:
            session = load_session_state(state_dir, session_id)
            return session.state in INDEXABLE_STATES
        except FileNotFoundError:
            # No state file — legacy session, include it
            return True
        except Exception:
            return True  # fail open: include rather than exclude

In the index-building loop, filter candidate files:

    candidates = [
        p for p in meetings_dir.glob("*.summary.md")
        if _is_indexable(p.stem.replace(".summary", ""), state_dir)
    ]

--- VERIFY SB-6 ---

python -c "
import inspect
from cli.search import search_meetings
src = inspect.getsource(search_meetings)
assert 'INDEXABLE_STATES' in src or 'is_indexable' in src, \
    'In-progress session filter not found in search_meetings'
print('BM25 index state filter: PRESENT')
"

===========================================================================
FINAL VERIFICATION — ALL SESSION BOUNDARY FIXES
===========================================================================

1. python -m py_compile cli/web.py mcp_server/tools/extraction.py \
   cli/search.py cli/feedback.py
   All must exit 0.

2. python -c "
   # Import all modified modules
   from mcp_server.tools.extraction import _build_todo_context, _load_prior_sessions
   from mcp_server.quality_gate import score_extraction
   from cli.search import search_meetings
   print('All session boundary modules import OK')
   "

3. Run the SB-3 todo.md filter test (verify old/done items excluded)
4. Run the SB-4 chaining type isolation test (verify seminar ≠ IS call)
5. Run the SB-5 feedback type filter test (verify rejection type isolation)

6. Start meeting-agent serve. Check the server log for the new context assembly
   log line (from SB-3.2). Verify it shows token budget breakdown.

7. Run two sequential pipeline sessions (even short 10-second test recordings).
   After each: verify active_session_id is None in a debug endpoint or log.
   After the second: verify the server log does NOT show any reference to
   the first session's content in the second session's context assembly log.

Report:
- SB-1 through SB-6: PASS/FAIL
- The context assembly log output for one test session (shows budget breakdown)
- Confirmation that active_session_id is cleared after each pipeline run
- Any fix that could not be applied and why
```
