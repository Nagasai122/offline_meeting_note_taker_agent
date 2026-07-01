from __future__ import annotations

import subprocess

from cli.git_backup import commit_all, ensure_repo


def test_ensure_repo_is_idempotent(tmp_path):
    ensure_repo(tmp_path)
    assert (tmp_path / ".git").exists()
    ensure_repo(tmp_path)  # second call must not raise or re-init destructively
    assert (tmp_path / ".git").exists()


def test_commit_all_returns_hash_when_dirty_and_none_when_clean(tmp_path):
    ensure_repo(tmp_path)
    (tmp_path / "todo.md").write_text("- [ ] first item\n")

    first_hash = commit_all(tmp_path, "first commit")
    assert first_hash is not None
    assert len(first_hash) == 40  # full git SHA-1

    second_hash = commit_all(tmp_path, "nothing changed")
    assert second_hash is None


def test_commit_all_produces_a_real_revertable_history(tmp_path):
    ensure_repo(tmp_path)
    (tmp_path / "todo.md").write_text("v1\n")
    commit_all(tmp_path, "v1")
    (tmp_path / "todo.md").write_text("v2\n")
    commit_all(tmp_path, "v2")

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    lines = log.stdout.strip().splitlines()
    assert len(lines) == 2
    assert "v2" in lines[0]
    assert "v1" in lines[1]
