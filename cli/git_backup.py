"""
Git-based backup for `data/` (M6), implementing critique amendment 5.

Why git rather than a hand-rolled copy-on-write backup scheme: `apply_reviewed_update`
is the only code path in this project that mutates `data/todo.md` in place,
and the failure mode being defended against is "the apply logic itself had a
bug and corrupted todo.md" -- a plain `git revert`/`git checkout` on the
single-file repo this module maintains is a well-understood, battle-tested
undo path, cheaper to get right than reinventing snapshotting.

Scope note: this commits the whole `data/` tree (todo.md, projects/,
pending_review/, state/, meetings/ -- everything except tmp/, which lives
outside data/ entirely), not just todo.md, so that the pre/post-apply
snapshots also capture the session-state transition and the now-applied
reviewed-decisions file for full forensic reconstruction of what happened
in any one apply.

This module shells out to the system `git` binary via subprocess rather than
adding GitPython as a dependency -- a deliberate choice consistent with this
project's general bias (see concurrency/lock.py) towards the smallest
sufficient implementation over a new dependency, since the only operations
needed are `init`, `add -A`, `commit` and a dirty-check, all trivial via the
CLI. If `git` is not on PATH, this fails loudly (CalledProcessError /
FileNotFoundError) rather than silently skipping the backup -- the apply
flow treats backup failure as fatal, not best-effort, since "draft only,
full supervision" implies the undo path must actually exist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitBackupError(RuntimeError):
    """Raised when a git backup operation fails (binary missing, command error)."""


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        raise GitBackupError(
            "git is not available on PATH -- required for the apply_reviewed_update "
            "backup/undo path (critique amendment 5)."
        ) from exc


def ensure_repo(data_dir: Path | str) -> None:
    """Idempotently initialise `data_dir` as a git repository if it is not already one."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    probe = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=data_dir)
    if probe.returncode == 0 and probe.stdout.strip() == "true":
        return
    result = _run(["git", "init"], cwd=data_dir)
    if result.returncode != 0:
        raise GitBackupError(f"git init failed in {data_dir}: {result.stderr.strip()}")
    # A local-only, single-user repo: identity is required for `git commit` to
    # succeed, but its content is never published anywhere, so a fixed,
    # clearly-synthetic identity is sufficient and avoids depending on the
    # user's global git config being present in a fresh offline environment.
    _run(["git", "config", "user.email", "meeting-agent@localhost"], cwd=data_dir)
    _run(["git", "config", "user.name", "meeting-agent"], cwd=data_dir)


def commit_all(data_dir: Path | str, message: str) -> str | None:
    """Stage and commit every change under `data_dir`. Returns the new commit hash,
    or None if there was nothing to commit (working tree already clean)."""
    data_dir = Path(data_dir)
    add_result = _run(["git", "add", "-A"], cwd=data_dir)
    if add_result.returncode != 0:
        raise GitBackupError(f"git add failed in {data_dir}: {add_result.stderr.strip()}")

    status = _run(["git", "status", "--porcelain"], cwd=data_dir)
    if not status.stdout.strip():
        return None  # nothing changed since the last commit -- not an error

    commit_result = _run(["git", "commit", "-m", message], cwd=data_dir)
    if commit_result.returncode != 0:
        raise GitBackupError(f"git commit failed in {data_dir}: {commit_result.stderr.strip()}")

    rev = _run(["git", "rev-parse", "HEAD"], cwd=data_dir)
    return rev.stdout.strip()
