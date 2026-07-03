"""
File lock for cross-process mutual exclusion, backed by `portalocker`
(Win32 `LockFileEx` on Windows, `fcntl.flock` on POSIX) rather than a
hand-rolled `O_CREAT | O_EXCL` + PID-file staleness check.

Why the switch: the original approach (documented in git history) created the
lock file atomically via `O_CREAT|O_EXCL`, and treated it as stale if the PID
written inside it no longer corresponded to a live process, clearing it and
retrying. That staleness check had a genuine TOCTOU race between two *live*
contenders: if contender B reads a dead PID, gets pre-empted, and only then
calls `unlink()`, that unlink can fire *after* a third contender C has already
cleared the same stale lock and written its own (valid, current) PID into a
freshly-recreated file -- B's unlink would then delete C's active lock, and
both B and C could believe they hold it simultaneously. This is not a
hypothetical: it is most likely to trigger exactly when it matters most, i.e.
right after a crash left a stale lock and multiple processes (the web
dashboard, a CLI invocation) are contending for it at once.

`portalocker` sidesteps the entire class of bug: the OS itself enforces
exclusivity on the open file handle, and releases it automatically when the
owning process's handle closes -- including on a crash or `kill -9`, with no
PID file, no staleness heuristic, and no unlink race, because there is
nothing to unlink. The lock *file* now persists across acquire/release
cycles (it's just a lock target, not lock state) rather than being created
and deleted each time -- deliberately different from the previous behaviour,
which deleted the file on release; two tests asserted that deletion and have
been updated (tests/concurrency/test_lock.py) to reflect the new, more
correct semantics of "the file is a persistent lock target."

Public API (`FileLock(lock_path, timeout_seconds=..., poll_interval_seconds=...)`,
`.acquire()`, `.release()`, context manager, `LockTimeoutError`) is unchanged,
so every existing caller (mcp_server/state.py, cli/review_apply.py, etc.)
works without modification.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import portalocker
import portalocker.exceptions

logger = logging.getLogger(__name__)


class LockTimeoutError(RuntimeError):
    """Raised when a lock could not be acquired within the configured timeout."""


class FileLock:
    def __init__(
        self,
        lock_path: Path | str,
        timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._fh = None

    def acquire(self) -> None:
        if self._fh is not None:
            raise RuntimeError(f"Lock {self.lock_path} already held by this FileLock instance.")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 'a+' rather than 'w': never truncates an existing lock file's
        # content on open (matters once we write our PID below), and creates
        # it if it doesn't exist yet -- both without requiring exclusivity at
        # the filesystem-open level, since portalocker.lock() below is what
        # actually enforces exclusivity.
        fh = open(self.lock_path, "a+")
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
                break
            except portalocker.exceptions.LockException:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise LockTimeoutError(
                        f"Could not acquire lock {self.lock_path} within {self.timeout_seconds}s. "
                        "Another process is holding it. If you're certain no other "
                        "meeting-agent process is running, this may be a stale OS-level "
                        "lock from an unusual shutdown -- restarting should clear it, "
                        "since the lock is tied to the owning process's file handle."
                    )
                time.sleep(self.poll_interval_seconds)

        # Best-effort diagnostic only (not part of the correctness mechanism,
        # unlike the old PID-file approach): overwrite the file's content with
        # our PID so a human inspecting the lock file by hand during an
        # incident can see who holds it.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError as exc:  # noqa: BLE001 - diagnostic write only, never fatal
            logger.debug("Could not write PID into lock file %s: %s", self.lock_path, exc)

        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                portalocker.unlock(self._fh)
            except Exception as exc:  # noqa: BLE001 - releasing must never raise
                logger.warning("Lock release error for %s: %s", self.lock_path, exc)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
