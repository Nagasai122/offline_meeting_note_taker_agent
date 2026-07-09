"""
Parser/writer for `data/projects.md` -- the structured Project/Institution
entity (roadmap items 7-8, P2).

Deliberately supersedes a prior internal recommendation
(docs/todo-system-research-2026-07.md) against building a full projects
layer for a single user: that guidance was reasonable for a flat todo list
with no consortium/partner dimension, but doesn't hold once cross-session,
cross-institution task tracking is an explicit goal -- free-text project
names drift (typos, inconsistent naming) in exactly the reporting scenarios
this feature exists for. `TodoItem.project_id` (mcp_server/todo.py)
references `Project.id` here by reference, not by free-text name, so "every
task under Project X" is reliable regardless of how the name was typed in
any given meeting.

Format mirrors `data/todo.md`'s own human-editable-Markdown-with-JSON-meta
philosophy (see mcp_server/todo.py's module docstring), swapping the
checklist-item convention for a heading-per-project one, since a project has
no done/undone binary state the way a task does -- `status` (active/
archived) lives in the meta JSON instead:

    ## CyberSec Consortium Project <!-- meta: {"id": "a1b2c3",
      "institutions": ["UREAD"], "partners": ["Marmara University"],
      "status": "active", "description": "EU-funded...",
      "created_at": "2026-07-01T10:00:00"} -->

Same legacy-tolerance and malformed-file guarantees as todo.py: an item
whose meta JSON is missing an optional field parses with a sensible default,
never a KeyError; a genuinely malformed line raises a typed
`ProjectFileUnparsableError` rather than being silently dropped or
corrupted on the next write.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_RE = re.compile(
    r"^##\s+(?P<name>.*?)"
    r"(?:\s*<!--\s*meta:\s*(?P<meta_json>[\{\[].*[\}\]])\s*-->)?\s*$"
)


class ProjectFileUnparsableError(RuntimeError):
    """Raised when data/projects.md cannot be parsed. Error code: PROJECT_FILE_UNPARSEABLE."""


@dataclass
class Project:
    name: str
    id: str | None = None
    institutions: list[str] = field(default_factory=list)
    partners: list[str] = field(default_factory=list)
    status: str = "active"  # "active" | "archived"
    description: str | None = None
    created_at: str | None = None


@dataclass
class ProjectFile:
    projects: list[Project] = field(default_factory=list)


def parse_projects(path: Path | str) -> ProjectFile:
    path = Path(path)
    if not path.exists():
        return ProjectFile(projects=[])  # first run: no projects.md yet is not an error

    projects: list[Project] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("## "):
            # Not a project heading (e.g. a top-level "# Projects" title or
            # free-text commentary a human added) -- ignore it rather than
            # treating every non-heading line as malformed.
            continue
        match = _PROJECT_RE.match(stripped)
        if not match:
            raise ProjectFileUnparsableError(
                f"{path}:{lineno}: malformed project heading: {line!r} "
                "(PROJECT_FILE_UNPARSEABLE)"
            )
        meta: dict = {}
        meta_json = match.group("meta_json")
        if meta_json:
            try:
                meta = json.loads(meta_json)
            except json.JSONDecodeError as exc:
                raise ProjectFileUnparsableError(
                    f"{path}:{lineno}: malformed meta JSON ({exc}) (PROJECT_FILE_UNPARSEABLE)"
                ) from exc
            if not isinstance(meta, dict):
                raise ProjectFileUnparsableError(
                    f"{path}:{lineno}: meta JSON must be an object, got {type(meta).__name__} "
                    "(PROJECT_FILE_UNPARSEABLE)"
                )
        projects.append(
            Project(
                name=match.group("name").strip(),
                id=meta.get("id"),
                institutions=meta.get("institutions") or [],
                partners=meta.get("partners") or [],
                status=meta.get("status", "active"),
                description=meta.get("description"),
                created_at=meta.get("created_at"),
            )
        )
    return ProjectFile(projects=projects)


def format_project(project: Project) -> str:
    meta = {
        "id": project.id,
        "status": project.status,
    }
    # Conditional-include for everything else (same pattern as todo.py's
    # `evidence` field) -- keeps a bare project's line free of clutter and
    # keeps existing rows byte-identical on rewrite when nothing changed.
    if project.institutions:
        meta["institutions"] = project.institutions
    if project.partners:
        meta["partners"] = project.partners
    if project.description:
        meta["description"] = project.description
    if project.created_at:
        meta["created_at"] = project.created_at
    meta_json = json.dumps(meta)
    return f"## {project.name} <!-- meta: {meta_json} -->"


def format_project_file(file: ProjectFile) -> str:
    """Render a ProjectFile back to the on-disk Markdown format.

    Same known, deliberately-flagged limitation as todo.py's
    format_todo_file: `parse_projects` above only retains project headings --
    any free-text commentary a human has added by hand is silently skipped
    during parsing and therefore NOT round-tripped by a
    parse_projects -> format_project_file cycle. Only cli/project_apply.py
    calls this function, always immediately after reading the file within
    the same locked critical section, for the same reason that is an
    acceptable trade-off there.
    """
    header = "# Projects\n\n" if file.projects else ""
    return header + "\n".join(format_project(p) for p in file.projects) + ("\n" if file.projects else "")
