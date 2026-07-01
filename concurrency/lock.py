"""
Minimal, portable, single-machine file lock.

Why hand-rolled rather than a `filelock`-style dependency: the only consumer is
a single-user local tool coordinating between its own CLI invocations and its
own MCP server process on one machine, so the cross-platform advisory-locking
guarantees a library like `filelock` provides (handling NFS, multiple hosts,
etc.) are not needed here. `os.open(..., O_CREAT | O_EXCL)` is atomic on both
POSIX and Windows and is sufficient for that narrower job.

Staleness handling: the lock file's content is the PID that created it (best
-effort -- written after the O_CREAT|O_EXCL open, so there is a narrow window
where a reader could see an empty file from a process that crashed between
open and write; that window is treated as "not yet stale" rather than
guessed at). On a contended acquire, if the existing lock's PID no longer
corresponds to a live process, the lock is treated as stale, removed, and
the acquire is retried immediately rather than waiting out the full timeout
-- this is what let an orphaned `data/state/probe1.json` session block a real
run with a `LockTimeoutError` even though the process that took the lock was
long dead. This is still a single-machine, single-user mechanism (PID
liveness is meaningless across hosts), consistent with the rest of this
module's stated scope.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a declared dependency elsewhere
    psutil = None


class LockTimeoutError(RuntimeError):
    """Raised when a lock could not be acquired within the configured timeout."""


def _pid_is_alive(pid: int) -> bool:
    if psutil is not None:
        return psutil.pid_exists(pid)
    # Fallback without psutil: POSIX-only liveness probe. Windows without
    # psutil cannot reliably check this, so err on the side of "alive" --
    # a false negative here (treating a live lock as stale) risks two
    # writers colliding, which is worse than occasionally waiting out a
    # lock that was actually already dead.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _clear_if_stale(lock_path: Path) -> None:
    try:
        content = lock_path.read_text().strip()
        pid = int(content)
    except (FileNotFoundError, ValueError):
        return
    if not _pid_is_alive(pid):
        lock_path.unlink(missing_ok=True)


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
        self._acquired = False

    def acquire(self) -> None:
        if self._acquired:
            raise RuntimeError(f"Lock {self.lock_path} already held by this FileLock instance.")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                self._acquired = True
                return
            except FileExistsError:
                _clear_if_stale(self.lock_path)
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"Could not acquire lock {self.lock_path} within {self.timeout_seconds}s. "
                        "If no other meeting-agent process is running, this may be a stale lock "
                        "left by a crash -- delete the file by hand to recover."
                    )
                time.sleep(self.poll_interval_seconds)

    def release(self) -> None:
        if self._acquired:
            self.lock_path.unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
