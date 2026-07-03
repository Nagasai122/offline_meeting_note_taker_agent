"""
Atomic text-file writes: write to a sibling `.tmp` file in the same directory,
fsync it, then `os.replace()` onto the real path.

`os.replace()` is atomic on both POSIX and Windows/NTFS -- a concurrent reader
never observes a partially-written file, and a crash between the temp-file
write and the replace leaves the *original* file untouched (only the orphaned
`.tmp` is lost, not the data that was already durably on disk). This closes
the plain `path.write_text(...)` data-loss window used throughout this
project's artefact writers: without it, a process killed mid-write (a crashed
`meeting-agent` invocation, a Windows update forcing a reboot, `taskkill /F`)
can leave `todo.md` or a session's state JSON truncated or empty -- silent
data loss discovered by a QA pass on the file, not by design.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path | str, content: str, encoding: str = "utf-8") -> None:
    """Write `content` to `path` atomically.

    Args:
        path: Destination file path. Parent directory is created if missing.
        content: Text to write.
        encoding: Text encoding, default UTF-8.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / (path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
