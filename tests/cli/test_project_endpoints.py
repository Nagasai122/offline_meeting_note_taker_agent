"""
Tests for the Project CRUD endpoints in cli/web.py (P2.2):
  GET    /api/projects
  POST   /api/projects
  PATCH  /api/projects/{project_id}
  DELETE /api/projects/{project_id}

Mirrors tests/cli/test_task_endpoints.py's fixture pattern (a fake `settings`
object patched onto cli.web, TestClient against the real app).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mcp_server.project import parse_projects


@pytest.fixture()
def dirs(tmp_path):
    d = {
        "data_dir": tmp_path / "data",
        "projects_path": tmp_path / "data" / "projects.md",
        "todo_path": tmp_path / "data" / "todo.md",
        "lock_path": tmp_path / "data" / "state" / ".lock",
    }
    d["lock_path"].parent.mkdir(parents=True, exist_ok=True)
    d["todo_path"].write_text("", encoding="utf-8")
    return d


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


def test_list_projects_empty_when_no_file(client):
    c, dirs = client
    resp = c.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == {"projects": []}


def test_create_project_then_list(client):
    c, dirs = client
    resp = c.post(
        "/api/projects",
        json={"name": "CyberSec Consortium", "institutions": ["UREAD"], "partners": ["Marmara University"]},
    )
    assert resp.status_code == 200
    project_id = resp.json()["project_id"]
    assert resp.json()["status"] == "created"

    resp = c.get("/api/projects")
    assert resp.status_code == 200
    projects = resp.json()["projects"]
    assert len(projects) == 1
    assert projects[0]["id"] == project_id
    assert projects[0]["name"] == "CyberSec Consortium"
    assert projects[0]["status"] == "active"


def test_create_project_rejects_empty_name(client):
    c, dirs = client
    resp = c.post("/api/projects", json={"name": "   "})
    assert resp.status_code == 422


def test_create_project_rejects_oversized_institutions_list(client):
    c, dirs = client
    resp = c.post("/api/projects", json={"name": "X", "institutions": [f"inst-{i}" for i in range(25)]})
    assert resp.status_code == 422


def test_patch_project_updates_fields(client):
    c, dirs = client
    project_id = c.post("/api/projects", json={"name": "Original"}).json()["project_id"]

    resp = c.patch(f"/api/projects/{project_id}", json={"name": "Renamed", "description": "Updated"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "Updated"

    project = parse_projects(dirs["projects_path"]).projects[0]
    assert project.name == "Renamed"


def test_patch_project_rejects_invalid_status(client):
    c, dirs = client
    project_id = c.post("/api/projects", json={"name": "X"}).json()["project_id"]

    resp = c.patch(f"/api/projects/{project_id}", json={"status": "deleted"})
    assert resp.status_code == 422


def test_patch_project_unknown_id_returns_404(client):
    c, dirs = client
    resp = c.patch("/api/projects/does-not-exist", json={"name": "X"})
    assert resp.status_code == 404


def test_delete_project_archives_rather_than_removes(client):
    c, dirs = client
    project_id = c.post("/api/projects", json={"name": "To Archive"}).json()["project_id"]

    resp = c.delete(f"/api/projects/{project_id}")
    assert resp.status_code == 200
    assert resp.json() == {"project_id": project_id, "status": "archived"}

    projects = parse_projects(dirs["projects_path"]).projects
    assert len(projects) == 1
    assert projects[0].status == "archived"


def test_delete_project_unknown_id_returns_404(client):
    c, dirs = client
    resp = c.delete("/api/projects/does-not-exist")
    assert resp.status_code == 404


def test_get_project_tasks_filters_by_project_id(client):
    c, dirs = client
    project_id = c.post("/api/projects", json={"name": "CyberSec Consortium"}).json()["project_id"]
    dirs["todo_path"].write_text(
        f'- [ ] In this project <!-- meta: {{"id": "a1", "project_id": "{project_id}"}} -->\n'
        '- [ ] In a different project <!-- meta: {"id": "a2", "project_id": "other-proj"} -->\n'
        '- [ ] No project at all <!-- meta: {"id": "a3"} -->\n',
        encoding="utf-8",
    )

    resp = c.get(f"/api/projects/{project_id}/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_id"] == project_id
    assert [t["id"] for t in body["tasks"]] == ["a1"]


def test_get_project_tasks_excludes_soft_deleted(client):
    c, dirs = client
    project_id = c.post("/api/projects", json={"name": "X"}).json()["project_id"]
    dirs["todo_path"].write_text(
        f'- [ ] Deleted task <!-- meta: {{"id": "a1", "project_id": "{project_id}", "status": "deleted"}} -->\n',
        encoding="utf-8",
    )

    resp = c.get(f"/api/projects/{project_id}/tasks")
    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


def test_get_project_tasks_empty_for_unknown_project(client):
    c, dirs = client
    resp = c.get("/api/projects/does-not-exist/tasks")
    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


def test_create_project_rejected_cross_origin(client):
    """CSRF hardening (H1) must also cover the new project endpoints."""
    c, dirs = client
    resp = c.post(
        "/api/projects",
        json={"name": "X"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 403
