from __future__ import annotations

import pytest

from mcp_server.project import Project, ProjectFileUnparsableError, format_project, parse_projects


def test_missing_file_returns_empty_project_file(tmp_path):
    result = parse_projects(tmp_path / "projects.md")
    assert result.projects == []


def test_parses_well_formed_projects(tmp_path):
    path = tmp_path / "projects.md"
    path.write_text(
        '## CyberSec Consortium <!-- meta: {"id": "a1", "institutions": ["UREAD"], '
        '"partners": ["Marmara University"], "status": "active"} -->\n'
        '## Archived Thing <!-- meta: {"id": "z9", "status": "archived"} -->\n'
    )
    result = parse_projects(path)
    assert len(result.projects) == 2
    assert result.projects[0] == Project(
        name="CyberSec Consortium",
        id="a1",
        institutions=["UREAD"],
        partners=["Marmara University"],
        status="active",
    )
    assert result.projects[1].status == "archived"


def test_ignores_non_heading_lines(tmp_path):
    path = tmp_path / "projects.md"
    path.write_text("# Projects\n\nSome commentary.\n## Real Project <!-- meta: {\"id\": \"a1\"} -->\n")
    result = parse_projects(path)
    assert len(result.projects) == 1
    assert result.projects[0].name == "Real Project"


def test_project_without_meta_comment_parses_with_defaults(tmp_path):
    path = tmp_path / "projects.md"
    path.write_text("## Plain Project, no metadata\n")
    result = parse_projects(path)
    assert result.projects[0].id is None
    assert result.projects[0].status == "active"
    assert result.projects[0].institutions == []
    assert result.projects[0].partners == []


def test_malformed_meta_json_raises_unparsable(tmp_path):
    path = tmp_path / "projects.md"
    path.write_text('## Broken meta <!-- meta: {"id": "a1", "status": } -->\n')
    with pytest.raises(ProjectFileUnparsableError, match="PROJECT_FILE_UNPARSEABLE"):
        parse_projects(path)


def test_meta_json_must_be_object(tmp_path):
    path = tmp_path / "projects.md"
    path.write_text('## Project <!-- meta: ["not", "an", "object"] -->\n')
    with pytest.raises(ProjectFileUnparsableError, match="PROJECT_FILE_UNPARSEABLE"):
        parse_projects(path)


def test_format_project_roundtrips_through_parse(tmp_path):
    project = Project(
        name="Roundtrip Project",
        id="z9",
        institutions=["UREAD", "Partner Uni"],
        partners=["External Co"],
        status="active",
        description="EU-funded consortium project",
        created_at="2026-07-01T10:00:00",
    )
    path = tmp_path / "projects.md"
    path.write_text(format_project(project) + "\n")
    parsed = parse_projects(path).projects[0]
    assert parsed == project


def test_format_project_omits_empty_optional_fields(tmp_path):
    project = Project(name="Bare Project", id="a1", status="active")
    rewritten = format_project(project)
    assert "institutions" not in rewritten
    assert "partners" not in rewritten
    assert "description" not in rewritten
    assert "created_at" not in rewritten


def test_archived_project_status_roundtrips(tmp_path):
    project = Project(name="Old Project", id="old1", status="archived")
    path = tmp_path / "projects.md"
    path.write_text(format_project(project) + "\n")
    parsed = parse_projects(path).projects[0]
    assert parsed.status == "archived"
