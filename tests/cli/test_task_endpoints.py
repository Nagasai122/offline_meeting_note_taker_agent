"""
Tests for the task CRUD endpoints in cli/web.py:
  GET    /api/tasks/{task_id}
  PATCH  /api/tasks/{task_id}
  DELETE /api/tasks/{task_id}
  POST   /api/tasks/{task_id}/duplicate
  POST   /api/tasks/{task_id}/comments
  POST   /api/tasks/{task_id}/attachments
  POST   /api/todo/complete
  POST   /api/tasks/manual

POST /api/todo/complete used to write data/todo.md directly (no FileLock, no
CapabilityToken, no atomic write) -- a second, ungated path to the same file
alongside PATCH /api/tasks/{id}, which goes through all three via
update_task_status(). These tests lock in the reconciled behavior: both
endpoints now produce the same on-disk result for "mark done".

Also covers POST /api/tasks/manual's full field set (P1.3): title, owner,
project_id, status, reminder_date, on top of the fields it already supported;
and P1.5's full edit/duplicate/comments/attachments surface.
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


def test_create_manual_task_full_field_set(client):
    c, dirs = client

    resp = c.post("/api/tasks/manual", json={
        "description": "Recruit new project staff",
        "title": "Recruit staff",
        "owner": "Professor Atta",
        "due_date": "2026-08-31",
        "reminder_date": "2026-08-25",
        "priority": "HIGH",
        "status": "in_progress",
        "tag": "Recruitment",
        "progress_note": "Waiting for HR approval.",
        "project_id": "cybersec-proj",
    })
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.id == task_id
    assert item.title == "Recruit staff"
    assert item.owner == "Professor Atta"
    assert item.owner_type == "self"
    assert item.status == "in_progress"
    assert item.project_id == "cybersec-proj"
    assert item.reminder_date == "2026-08-25"


def test_create_manual_task_rejects_invalid_status(client):
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "X", "status": "not-a-real-status"})
    assert resp.status_code == 422


def test_create_manual_task_minimal_payload_still_works(client):
    """Only `description` is required -- every new field must stay optional
    so the existing minimal-payload callers (if any) don't break."""
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "Bare-minimum task"})
    assert resp.status_code == 200

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.title is None
    assert item.project_id is None
    assert item.status == "todo"


def test_get_task_returns_full_detail(client):
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "Detail me", "title": "Title", "owner": "Naga"})
    task_id = resp.json()["task_id"]

    resp = c.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == task_id
    assert data["title"] == "Title"
    assert data["owner"] == "Naga"
    assert data["owner_type"] == "self"
    assert data["comments"] == []
    assert data["attachments"] == []


def test_get_task_unknown_id_returns_404(client):
    c, dirs = client
    resp = c.get("/api/tasks/does-not-exist")
    assert resp.status_code == 404


def test_patch_task_full_edit(client):
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123")

    resp = c.patch("/api/tasks/abc123", json={
        "title": "New title", "description": "New description", "owner": "Dave",
        "project_id": "proj-9", "institution": "UREAD", "tag": "WP1",
    })
    assert resp.status_code == 200

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.title == "New title"
    assert item.description == "New description"
    assert item.owner == "Dave"
    assert item.project_id == "proj-9"
    assert item.institution == "UREAD"
    assert item.tag == "WP1"


def test_patch_task_rejects_empty_description(client):
    """Regression test: `if req.description and ...` is a falsy check, so
    description="" used to skip validation entirely and silently blank the
    task's description -- unlike task creation, which requires non-empty."""
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123")

    resp = c.patch("/api/tasks/abc123", json={"description": ""})
    assert resp.status_code == 422

    resp_whitespace = c.patch("/api/tasks/abc123", json={"description": "   "})
    assert resp_whitespace.status_code == 422

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.description == "Do the thing"  # untouched by the rejected PATCH


def test_patch_task_strips_description_whitespace(client):
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123")

    resp = c.patch("/api/tasks/abc123", json={"description": "  Trimmed  "})
    assert resp.status_code == 200

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.description == "Trimmed"


def test_patch_task_rejects_invalid_priority(client):
    """Regression test: patch_task had no priority allow-list check, unlike
    create_manual_task, letting arbitrary strings be written to todo.md."""
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123")

    resp = c.patch("/api/tasks/abc123", json={"priority": "URGENT"})
    assert resp.status_code == 422

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.priority is None  # untouched by the rejected PATCH


def test_patch_task_accepts_valid_priority(client):
    c, dirs = client
    _write_todo(dirs["todo_path"], "abc123")

    resp = c.patch("/api/tasks/abc123", json={"priority": "HIGH"})
    assert resp.status_code == 200

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.priority == "HIGH"


def test_duplicate_task_endpoint(client):
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "Original task", "owner": "Naga"})
    original_id = resp.json()["task_id"]

    resp = c.post(f"/api/tasks/{original_id}/duplicate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["duplicate_of"] == original_id
    clone_id = data["task_id"]
    assert clone_id != original_id

    todo = parse_todo(dirs["todo_path"])
    assert len(todo.items) == 2
    clone = next(i for i in todo.items if i.id == clone_id)
    assert clone.description == "Original task"
    assert clone.owner == "Naga"


def test_duplicate_task_endpoint_unknown_id_returns_404(client):
    c, dirs = client
    resp = c.post("/api/tasks/does-not-exist/duplicate")
    assert resp.status_code == 404


def test_add_task_comment_endpoint(client):
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "Task with comments"})
    task_id = resp.json()["task_id"]

    resp = c.post(f"/api/tasks/{task_id}/comments", json={"text": "Waiting on HR", "author": "Naga"})
    assert resp.status_code == 200
    assert resp.json()["comments"][0]["text"] == "Waiting on HR"

    item = parse_todo(dirs["todo_path"]).items[0]
    assert len(item.comments) == 1


def test_add_task_comment_rejects_empty_text(client):
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "Task"})
    task_id = resp.json()["task_id"]

    resp = c.post(f"/api/tasks/{task_id}/comments", json={"text": "   "})
    assert resp.status_code == 422


def test_add_task_attachment_endpoint(client):
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "Task with files"})
    task_id = resp.json()["task_id"]

    resp = c.post(
        f"/api/tasks/{task_id}/attachments",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["attachments"][0]["filename"] == "notes.txt"

    saved_path = dirs["data_dir"] / "task_attachments" / task_id / "notes.txt"
    assert saved_path.exists()
    assert saved_path.read_bytes() == b"hello world"


def test_add_task_attachment_rejects_unsupported_extension(client):
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "Task"})
    task_id = resp.json()["task_id"]

    resp = c.post(
        f"/api/tasks/{task_id}/attachments",
        files={"file": ("virus.exe", b"nope", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert not (dirs["data_dir"] / "task_attachments" / task_id).exists()


def test_add_task_attachment_same_filename_twice_does_not_overwrite(client):
    """Regression test: uploading two different files with the same name to
    one task used to silently overwrite the first file's bytes on disk while
    todo.md ended up with two attachment records both pointing at the same
    (now-wrong) path -- the first upload's content was permanently lost with
    no error surfaced."""
    c, dirs = client
    resp = c.post("/api/tasks/manual", json={"description": "Task with two same-named files"})
    task_id = resp.json()["task_id"]

    resp1 = c.post(
        f"/api/tasks/{task_id}/attachments",
        files={"file": ("notes.txt", b"first upload", "text/plain")},
    )
    assert resp1.status_code == 200
    filename1 = resp1.json()["attachments"][0]["filename"]

    resp2 = c.post(
        f"/api/tasks/{task_id}/attachments",
        files={"file": ("notes.txt", b"second upload", "text/plain")},
    )
    assert resp2.status_code == 200
    attachments = resp2.json()["attachments"]
    assert len(attachments) == 2
    filename2 = attachments[1]["filename"]

    # The second upload must have been disambiguated, not overwritten the first.
    assert filename1 != filename2
    attachments_dir = dirs["data_dir"] / "task_attachments" / task_id
    assert (attachments_dir / filename1).read_bytes() == b"first upload"
    assert (attachments_dir / filename2).read_bytes() == b"second upload"
