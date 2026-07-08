"""
Tests for the task CRUD endpoints in cli/web.py:
  PATCH  /api/tasks/{task_id}
  DELETE /api/tasks/{task_id}
  POST   /api/todo/complete

POST /api/todo/complete used to write data/todo.md directly (no FileLock, no
CapabilityToken, no atomic write) -- a second, ungated path to the same file
alongside PATCH /api/tasks/{id}, which goes through all three via
update_task_status(). These tests lock in the reconciled behavior: both
endpoints now produce the same on-disk result for "mark done".
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcp_server.todo import parse_todo


@pytest.fixture()
def dirs(tmp_path):
    d = {
        "data_dir": tmp_path / "data",
        "todo_path": tmp_path / "data" / "todo.md",
        "lock_path": tmp_path / "data" / "state" / ".lock",
    }
    d["lock_path"].parent.mkdir(parents=True, exist_ok=True)
    return d


def _todo_line(task_id: str, status: str = "todo", done: bool = False) -> str:
    return (
        f'- [{"x" if done else " "}] Do the thing '
        f'<!-- meta: {{"id": "{task_id}", "owner": null, "due_date": null, '
        f'"session_id": null, "priority": null, "status": "{status}", '
        f'"source": "manual", "progress_note": null, "tag": null}} -->\n'
    )


def _write_todo(todo_path, task_id: str, status: str = "todo", done: bool = False) -> None:
    todo_path.write_text(_todo_line(task_id, status, done), encoding="utf-8")


@pytest.fixture()
def client(dirs):
    import cli.web as web_module

    class _FakeSettings:
        class paths:
            data_dir = str(dirs["data_dir"])
            tmp_dir = str(dirs["data_dir"] / "tmp")

        class llm:
            host = "127.0.0.1"
            port = 8080
            health_check_path = "/health"
            startup_timeout_seconds = 5

        class concurrency:
            lock_path = str(dirs["lock_path"])
            lock_timeout_seconds = 1.0

        class privacy:
            tmp_audio_ttl_seconds = 3600

        class whisper:
            device = "cpu"
            compute_type = "int8"

    with patch.object(web_module, "settings", _FakeSettings()):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c, dirs


def test_todo_complete_sets_status_done_and_done_flag(client):
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123", status="in_progress", done=False)

    resp = c.post("/api/todo/complete", json={"task_id": "abc123"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "success"}

    todo = parse_todo(dirs["todo_path"])
    item = todo.items[0]
    assert item.done is True
    assert item.status == "done"  # reconciled: previously stayed "in_progress"


def test_todo_complete_unknown_task_id_returns_not_found(client):
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123")

    resp = c.post("/api/todo/complete", json={"task_id": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json() == {"status": "not_found"}

    # Must not have touched the existing item.
    todo = parse_todo(dirs["todo_path"])
    assert todo.items[0].done is False


def test_patch_status_done_matches_todo_complete_result(client):
    """The two 'mark done' endpoints must now agree on the resulting item
    shape -- this is the whole point of the reconciliation."""
    c, dirs = client
    dirs["todo_path"].write_text(
        _todo_line("task-a") + _todo_line("task-b"), encoding="utf-8"
    )

    resp_patch = c.patch("/api/tasks/task-a", json={"status": "done"})
    assert resp_patch.status_code == 200

    resp_complete = c.post("/api/todo/complete", json={"task_id": "task-b"})
    assert resp_complete.status_code == 200

    todo = parse_todo(dirs["todo_path"])
    by_id = {item.id: item for item in todo.items}
    assert by_id["task-a"].status == "done"
    assert by_id["task-a"].done is True
    assert by_id["task-b"].status == "done"
    assert by_id["task-b"].done is True


def test_todo_complete_rejected_cross_origin(client):
    """CSRF hardening (H1): a mutating request whose Origin does not resolve
    to 127.0.0.1/localhost must be rejected, even though the endpoint itself
    would otherwise succeed."""
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123")

    resp = c.post(
        "/api/todo/complete",
        json={"task_id": "abc123"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403

    todo = parse_todo(dirs["todo_path"])
    assert todo.items[0].done is False


def test_mutating_request_allowed_from_localhost_origin(client):
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123")

    resp = c.post(
        "/api/todo/complete",
        json={"task_id": "abc123"},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "success"}
