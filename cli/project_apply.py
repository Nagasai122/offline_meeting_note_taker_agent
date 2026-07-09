"""
Core, testable logic for Project CRUD (P2.2 — structured Project/Institution
entity).

Mirrors cli/review_apply.py's write-safety discipline deliberately: same
CapabilityToken gating, same FileLock + atomic_write_text pattern, same
"soft delete via status field" convention as update_task_status's
status="deleted" path. A Project record is less sensitive than todo.md, but
reusing the identical pattern avoids introducing a second, weaker write
convention alongside the one cli/capability.py's docstring already audits.

Deliberately kept outside mcp_server/ for the same reason apply_reviewed_update
is (see cli/capability.py's docstring): the LLM extraction pipeline may READ
the active project list (mcp_server/project.py's parse_projects, a pure
parser with no write capability) to match items against, but must never be
able to create or rename a Project itself — creating one is always a human
action via this module's endpoints in cli/web.py.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from cli.capability import CapabilityToken, require_capability_token
from concurrency.atomic import atomic_write_text
from concurrency.lock import FileLock
from mcp_server.project import Project, ProjectFile, format_project_file, parse_projects

_UPDATABLE_FIELDS = ("name", "institutions", "partners", "status", "description")


def create_project(
    token: CapabilityToken,
    project_data: dict,
    projects_path: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> str:
    """Create a new Project record. Returns the freshly-minted project id.

    Args:
        project_data: dict with "name" (required) and optional
            "institutions"/"partners"/"description".
    """
    require_capability_token(token)

    projects_path = Path(projects_path)
    project_id = uuid4().hex[:8]
    project = Project(
        name=project_data["name"],
        id=project_id,
        institutions=project_data.get("institutions") or [],
        partners=project_data.get("partners") or [],
        status="active",
        description=project_data.get("description"),
        created_at=datetime.now().isoformat(),
    )

    with FileLock(lock_path, timeout_seconds=lock_timeout):
        existing = parse_projects(projects_path)
        merged = ProjectFile(projects=existing.projects + [project])
        atomic_write_text(projects_path, format_project_file(merged))

    return project_id


def update_project(
    token: CapabilityToken,
    project_id: str,
    updates: dict,
    projects_path: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> Project:
    """Update a subset of fields on an existing project by id, or archive it
    (soft delete — status="archived", same convention as update_task_status's
    status="deleted"; the record stays in data/projects.md for history/audit
    and so existing tasks' project_id references never dangle).

    Raises:
        KeyError: if no project with `project_id` exists.
    """
    require_capability_token(token)

    with FileLock(lock_path, timeout_seconds=lock_timeout):
        projects_path = Path(projects_path)
        existing = parse_projects(projects_path)
        target = next((p for p in existing.projects if p.id == project_id), None)
        if target is None:
            raise KeyError(f"No project with id '{project_id}' found in {projects_path}.")

        for field in _UPDATABLE_FIELDS:
            if field in updates and updates[field] is not None:
                setattr(target, field, updates[field])

        atomic_write_text(projects_path, format_project_file(existing))
        return target
