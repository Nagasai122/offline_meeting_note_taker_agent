# Claude CLI Bug-Fix Prompt — Batch 02 (Gemini QA Findings)
**Use with:** `claude` in `D:\meeting-agent`
**Generated:** 2026-07-01
**Source:** Gemini QA report findings F-001 through F-010

Paste everything below as a single prompt to Claude Code:

---

```
You are fixing confirmed bugs from a third-party QA report on the Meeting Agent codebase
at D:\meeting-agent. Work in order. Read each file before modifying it.
Run each verification command before moving on.

===========================================================================
FIX A — ATOMIC WRITES FOR todo.md (CRITICAL DATA LOSS RISK)
===========================================================================

File: cli/review_apply.py

Read the file in full. Find every location where todo.md is written.
This will include at least:
  - apply_reviewed_update: the final merged content write
  - write_manual_task: the append/overwrite after adding a manual task
  - update_task_status: the write after updating status

For EVERY write to todo.md (and any other .md or .json file in data/),
replace the direct write pattern with an atomic write:

BEFORE (unsafe):
    todo_path.write_text(format_todo_file(merged))

AFTER (safe):
    tmp_path = todo_path.with_suffix('.md.tmp')
    try:
        tmp_path.write_text(format_todo_file(merged), encoding='utf-8')
        tmp_path.flush()  # not available on Path — use open() instead:
    finally:
        # ensure tmp is cleaned up even if replace fails
        pass

The correct pattern using open() for fsync:
    tmp_path = todo_path.with_suffix('.md.tmp')
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(format_todo_file(merged))
            f.flush()
            import os; os.fsync(f.fileno())
        tmp_path.replace(todo_path)  # atomic on NTFS (os.replace semantics)
    except Exception:
        tmp_path.unlink(missing_ok=True)  # clean up tmp on failure
        raise

Apply this pattern to EVERY write in cli/review_apply.py that touches:
  - data/todo.md
  - data/state/*.json (check if state.py already does this — if not, fix there too)
  - data/meetings/*.json or *.md (extraction outputs)

Also check mcp_server/state.py — the _write() function calls path.write_text().
Apply the same atomic pattern there.

VERIFY FIX A:
python -c "
from pathlib import Path
import tempfile, os

# Verify os.replace is atomic on NTFS
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / 'todo.md'
    p.write_text('original content')
    tmp = p.with_suffix('.md.tmp')
    tmp.write_text('new content')
    tmp.replace(p)
    assert p.read_text() == 'new content', 'replace failed'
    assert not tmp.exists(), 'tmp not cleaned up'
    print('Atomic write pattern PASS')
"

grep -n "write_text" cli/review_apply.py mcp_server/state.py
# ALL remaining write_text calls must be for non-critical temp files only,
# or must use the atomic pattern. Zero direct write_text to todo.md or state JSON.

===========================================================================
FIX B — PIPELINE EXCEPTIONS MUST TRANSITION SESSION TO FAILED
===========================================================================

File: cli/web.py

Read the run_pipeline function in full.

Find the main try/except block. The except clause currently logs the error but does NOT
call state transition to FAILED. This leaves the session in STOPPED, TRANSCRIBED, or
EXTRACTED permanently — users see a spinner forever and cannot retry.

Add a finally/except block that transitions the session to FAILED on any unhandled exception:

Pattern:
    try:
        # ... existing pipeline steps ...
    except Exception as exc:
        logger.error("Pipeline failed for session %s: %s", session_id, exc, exc_info=True)
        try:
            state_mod.transition(
                settings.state_dir,
                session_id,
                state_mod.State.FAILED,
                settings.concurrency.lock_path,
                settings.concurrency.lock_timeout,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        except Exception as state_exc:
            logger.error(
                "Failed to transition session %s to FAILED: %s",
                session_id, state_exc
            )
        raise  # re-raise so the asyncio task records the exception

The `raise` at the end is important: asyncio tasks swallow exceptions silently unless
the task result is awaited or the exception is re-raised. Re-raising ensures the error
is visible in task exception handlers.

Also wrap the pipeline in a broad outer try to catch errors in the cleanup/finally
logic itself without masking the original exception.

VERIFY FIX B:
python -c "
import ast
src = open('cli/web.py').read()
tree = ast.parse(src)
print('cli/web.py parses OK after Fix B')
"
grep -n "State.FAILED\|FAILED" cli/web.py
# Must show at least one transition to FAILED inside run_pipeline

===========================================================================
FIX C — asyncio.Lock ON /api/record/start (RACE CONDITION)
===========================================================================

File: cli/web.py

Read the /api/record/start endpoint (POST, likely called start_recording or similar).

Add a module-level asyncio.Lock:
    _recording_lock: asyncio.Lock = asyncio.Lock()

In the endpoint handler, wrap the entire body in:
    async with _recording_lock:
        if active_session_id is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Already recording session '{active_session_id}'. Stop it first."
            )
        # ... rest of existing handler ...

Important: asyncio.Lock() cannot be instantiated at module level in all FastAPI versions
because there may not be an event loop yet. Use a lazy initialisation pattern:

    _recording_lock: asyncio.Lock | None = None

    def _get_recording_lock() -> asyncio.Lock:
        global _recording_lock
        if _recording_lock is None:
            _recording_lock = asyncio.Lock()
        return _recording_lock

Then in the endpoint:
    async with _get_recording_lock():
        ...

This ensures the lock is created in the running event loop.

VERIFY FIX C:
grep -n "_recording_lock\|asyncio.Lock" cli/web.py
# Must show the lock definition and usage in the record/start handler.

===========================================================================
FIX D — FileLock STALE-LOCK RACE (TOCTOU)
===========================================================================

File: concurrency/lock.py

Read the file in full before making changes.

The issue: _clear_if_stale reads a dead PID and unlinks the lock file, but between
the read and the unlink another process may have already cleared it and written its
own PID. The unlink then removes the valid lock, allowing two processes to hold it.

The correct fix for Windows using portalocker:

First, install portalocker:
    pip install portalocker --break-system-packages

Then refactor FileLock to use portalocker as the underlying mechanism for the
critical section of stale lock clearing:

    import portalocker
    import portalocker.exceptions

The key invariant to preserve: only one process at a time can acquire the lock.
On Windows, portalocker uses LockFileEx (Win32 API) which is atomic and not racy.

Implement the new FileLock:

```python
"""
FileLock using portalocker for cross-process mutual exclusion on Windows.
Replaces the PID-file approach which has a TOCTOU race in _clear_if_stale.
"""
from __future__ import annotations

import os
import time
import logging
from pathlib import Path

import portalocker
import portalocker.exceptions

logger = logging.getLogger(__name__)


class LockTimeoutError(TimeoutError):
    """Raised when a FileLock cannot be acquired within the timeout."""


class FileLock:
    """Cross-process, re-entrant-safe file lock using portalocker (Win32 LockFileEx).
    
    Usage:
        with FileLock(lock_path, timeout_seconds=10):
            # exclusive access here
    """

    def __init__(self, lock_path: Path | str, timeout_seconds: float = 30.0) -> None:
        self._path = Path(lock_path)
        self._timeout = timeout_seconds
        self._fh = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout
        self._fh = open(self._path, 'a+')  # create if not exists
        while True:
            try:
                portalocker.lock(self._fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
                # Write own PID for diagnostics (not for correctness)
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(str(os.getpid()))
                self._fh.flush()
                return
            except portalocker.exceptions.LockException:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise LockTimeoutError(
                        f"Could not acquire lock at {self._path} within {self._timeout}s. "
                        f"Another process may be holding it. "
                        f"If the other process is dead, delete the lock file manually: "
                        f"del \"{self._path}\""
                    )
                time.sleep(0.05)

    def release(self) -> None:
        if self._fh is not None:
            try:
                portalocker.unlock(self._fh)
            except Exception as exc:
                logger.warning("Lock release error: %s", exc)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
```

After rewriting FileLock, verify that all existing callers pass only `lock_path` and
`timeout_seconds` — the constructor signature must remain compatible.

Remove _clear_if_stale and _pid_is_alive from concurrency/lock.py (they are no longer
needed — portalocker handles stale locks automatically via OS-level file locking).

Also update mcp_server/state.py: it duplicates _pid_is_alive. After this fix,
_pid_is_alive is no longer needed in state.py either (the reaper still needs to check
PIDs for RECORDING sessions, but that is separate from lock clearing). Keep
_pid_is_alive in state.py only for the reaper logic, but note in a comment that it
is no longer related to lock clearing.

VERIFY FIX D:
python -c "
import portalocker
print('portalocker version:', portalocker.__version__)
from concurrency.lock import FileLock, LockTimeoutError
import tempfile, os
from pathlib import Path

with tempfile.TemporaryDirectory() as d:
    lp = Path(d) / 'test.lock'
    
    # Basic acquire/release
    with FileLock(lp, timeout_seconds=5):
        assert lp.exists()
        print('Lock acquired OK')
    print('Lock released OK')
    
    # Timeout test
    import threading
    lock_held = threading.Event()
    lock_released = threading.Event()
    
    def hold_lock():
        with FileLock(lp, timeout_seconds=30):
            lock_held.set()
            lock_released.wait(timeout=5)
    
    t = threading.Thread(target=hold_lock, daemon=True)
    t.start()
    lock_held.wait(timeout=5)
    
    try:
        FileLock(lp, timeout_seconds=0.2).acquire()
        print('ERROR: should have timed out')
    except LockTimeoutError:
        print('LockTimeoutError raised correctly on contention')
    finally:
        lock_released.set()
        t.join(timeout=5)

print('Fix D PASS')
"

===========================================================================
FIX E — REAPER READS STATE WITHOUT LOCK
===========================================================================

File: mcp_server/state.py — reap_orphaned_recordings function

Read the function. It currently:
1. Calls list_session_ids (reads directory — no lock needed)
2. Calls load_session_state (reads JSON — no lock currently)
3. Calls transition (acquires lock internally)

The race: between step 2 and step 3, the session state could change (the recording
process successfully stops and transitions RECORDING→STOPPED). The reaper would then
try to transition STOPPED→FAILED (invalid), which transition() would correctly reject
with InvalidTransitionError. So in practice this is a LOW risk — transition() enforces
correctness. However, the reaper currently catches (FileNotFoundError, ValueError, KeyError)
but not InvalidTransitionError.

Fix: expand the except in the reaper to also catch InvalidTransitionError:

    try:
        transition(
            state_dir, session_id, State.FAILED, lock_path, lock_timeout,
            error="ORPHANED_RECORDING", ...
        )
    except (InvalidTransitionError, FileNotFoundError):
        # Session legitimately finished (RECORDING→STOPPED) between our read and
        # this transition call. Not a bug — skip it.
        logger.debug(
            "Session %s finished legitimately before reaper could mark it FAILED.",
            session_id
        )
        continue

This is the minimal correct fix. A full read-under-lock refactor would require
making list_session_ids + load_session_state atomic, which portalocker enables but
is heavier than needed for the reaper's use-case.

VERIFY FIX E:
python -c "
import ast
src = open('mcp_server/state.py').read()
ast.parse(src)
assert 'InvalidTransitionError' in src
print('Fix E: InvalidTransitionError handled in reaper OK')
"

===========================================================================
FIX F — BROWSER REFRESH RESETS RECORDING TIMER
===========================================================================

File: cli/web.py — GET /api/briefing endpoint
File: static/app.js — recording timer logic

The problem: recording start time is stored only in JavaScript memory (_recordingStartMs).
On browser refresh, this resets to Date.now(), making the elapsed timer start from 0.

Fix server side:
In GET /api/briefing, include the active session's recording start time if there is
an active recording:

    response["recording"] = {
        "active": True,
        "session_id": active_session_id,
        "started_at": session_state.metadata.get("started_at"),  # ISO string
        "meeting_type": session_state.metadata.get("meeting_type", "general"),
    }

The "started_at" field must be written to session metadata when recording begins.
In the record/start handler, when calling create_session(), pass:
    started_at=datetime.now(timezone.utc).isoformat()

Fix client side (static/app.js):
When the briefing response contains recording.active == true:
    const startedAt = new Date(data.recording.started_at).getTime();
    _recordingStartMs = startedAt;  // set from server, not Date.now()

This ensures a page refresh reads the correct start time from the server and resumes
the elapsed display correctly.

VERIFY FIX F:
grep -n "started_at" cli/web.py
# Must appear in create_session() call and in the briefing response
grep -n "started_at\|_recordingStartMs" static/app.js
# Must show _recordingStartMs being set from server value

===========================================================================
FIX G — SOFT-DELETED TASKS BLOCK UUID RE-APPLICATION
===========================================================================

File: cli/review_apply.py — apply_reviewed_update function

Find where existing_by_id is built (likely a dict comprehension over existing todo items).
Change it to exclude deleted items:

    existing_by_id = {
        item["id"]: item
        for item in existing_items
        if item.get("status") != "deleted"
    }

This ensures that if a task was soft-deleted and an AI extraction produces a new task
with the same UUID (vanishingly rare but correct behaviour is important), the new task
can be applied without being blocked by the deleted record.

VERIFY FIX G:
python -c "
# Check the fix is in place
src = open('cli/review_apply.py').read()
assert 'deleted' in src
print('Fix G: deleted filter in existing_by_id — present')
"

===========================================================================
FIX H — PIPE CHARACTERS IN TASK DESCRIPTIONS BREAK MoM MARKDOWN TABLES
===========================================================================

File: mcp_server/mom_writer.py (all three write_*_mom functions)

In every location where action item descriptions are written into a Markdown table row,
escape pipe characters:

    def _safe_cell(text: str | None) -> str:
        """Escape Markdown table cell content."""
        if text is None:
            return ''
        return str(text).replace('|', '\\|').replace('\n', ' ')

Apply _safe_cell() to every field rendered inside a `| ... |` table row:
    f"| {i} | {_safe_cell(item['description'])} | {_safe_cell(item.get('assignee'))} | ..."

Also apply to the MoM fields that go into table rows in all four templates
(IS Call, Project, Seminar, General).

VERIFY FIX H:
python -c "
# Simulate a pipe-containing task
task = {'id': '1', 'description': 'Fix Option A | Option B issue', 'assignee': None, 'due_date': None, 'priority': 'HIGH'}
# Manually test _safe_cell
desc = task['description'].replace('|', r'\|')
row = f'| 1 | {desc} | N/A | TBD | HIGH |'
assert '\\\\|' not in row or r'\|' in row  # escaped
print('Pipe escape OK:', row)
"

===========================================================================
FINAL VERIFICATION — ALL FIXES
===========================================================================

1. python -m py_compile cli/review_apply.py mcp_server/state.py concurrency/lock.py
   cli/web.py mcp_server/mom_writer.py
   All must exit 0.

2. python -c "
   from concurrency.lock import FileLock, LockTimeoutError
   from mcp_server.state import transition, InvalidTransitionError
   from cli.review_apply import apply_reviewed_update
   print('All critical module imports OK')
   "

3. grep -rn "\.write_text(" cli/review_apply.py mcp_server/state.py
   Review every match. Any write to todo.md or state JSON must use the atomic pattern.
   Writes to .tmp files are acceptable.

4. grep -n "subprocess.run" cli/web.py
   Must still return 0 matches (Fix C1 from bugfix_01.md should have removed these).
   If matches remain, Fix C1 was not applied — apply it now before proceeding.

5. Confirm portalocker is importable:
   python -c "import portalocker; print(portalocker.__version__)"

Report:
- PASS/FAIL per fix (A through H)
- Any module that failed to parse after modification
- The portalocker version installed
- Confirm grep for write_text in review_apply.py shows only atomic-pattern writes
- Any fix that could not be applied cleanly with the specific reason
```
