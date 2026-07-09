from __future__ import annotations

import pytest

from cli.capability import CapabilityToken, mint_capability_token
from cli.project_apply import create_project, update_project
from mcp_server.project import parse_projects


def _paths(tmp_path):
    return tmp_path / "projects.md", tmp_path / ".lock"


def test_create_project_requires_genuine_token(tmp_path):
    projects_path, lock_path = _paths(tmp_path)
    with pytest.raises(TypeError):
        create_project("not-a-token", {"name": "X"}, projects_path, lock_path, 5.0)


def test_create_project_writes_new_record(tmp_path):
    projects_path, lock_path = _paths(tmp_path)
    token = mint_capability_token()

    project_id = create_project(
        token,
        {"name": "CyberSec Consortium", "institutions": ["UREAD"], "partners": ["Marmara University"]},
        projects_path, lock_path, 5.0,
    )

    result = parse_projects(projects_path)
    assert len(result.projects) == 1
    project = result.projects[0]
    assert project.id == project_id
    assert project.name == "CyberSec Consortium"
    assert project.institutions == ["UREAD"]
    assert project.partners == ["Marmara University"]
    assert project.status == "active"
    assert project.created_at is not None


def test_create_project_appends_to_existing_file(tmp_path):
    projects_path, lock_path = _paths(tmp_path)
    token = mint_capability_token()

    create_project(token, {"name": "First"}, projects_path, lock_path, 5.0)
    create_project(token, {"name": "Second"}, projects_path, lock_path, 5.0)

    result = parse_projects(projects_path)
    assert [p.name for p in result.projects] == ["First", "Second"]


def test_update_project_overwrites_allowed_fields(tmp_path):
    projects_path, lock_path = _paths(tmp_path)
    token = mint_capability_token()
    project_id = create_project(token, {"name": "Original Name"}, projects_path, lock_path, 5.0)

    updated = update_project(
        token, project_id,
        {"name": "Renamed", "description": "Now with a description"},
        projects_path, lock_path, 5.0,
    )

    assert updated.name == "Renamed"
    assert updated.description == "Now with a description"
    result = parse_projects(projects_path)
    assert result.projects[0].name == "Renamed"


def test_update_project_can_archive_soft_delete(tmp_path):
    projects_path, lock_path = _paths(tmp_path)
    token = mint_capability_token()
    project_id = create_project(token, {"name": "To Archive"}, projects_path, lock_path, 5.0)

    update_project(token, project_id, {"status": "archived"}, projects_path, lock_path, 5.0)

    result = parse_projects(projects_path)
    assert len(result.projects) == 1
    assert result.projects[0].status == "archived"


def test_update_project_missing_id_raises_keyerror(tmp_path):
    projects_path, lock_path = _paths(tmp_path)
    token = mint_capability_token()
    create_project(token, {"name": "Exists"}, projects_path, lock_path, 5.0)

    with pytest.raises(KeyError):
        update_project(token, "nonexistent", {"status": "archived"}, projects_path, lock_path, 5.0)


def test_update_project_requires_genuine_token(tmp_path):
    projects_path, lock_path = _paths(tmp_path)
    token = mint_capability_token()
    project_id = create_project(token, {"name": "X"}, projects_path, lock_path, 5.0)

    with pytest.raises(TypeError):
        update_project(object(), project_id, {"status": "archived"}, projects_path, lock_path, 5.0)
