"""
Parser/writer for `data/todo.md`.

Format (a deliberately human-editable Markdown checklist, since the user
reviews and may hand-edit this file directly, per the draft-only-supervision
constraint):

    - [ ] Buy projector bulb <!-- meta: {"id": "a1b2c3", "owner": null,
      "due_date": "2026-07-04", "session_id": "standup-2026-06-30",
      "priority": "MEDIUM", "status": "todo", "source": "standup-2026-06-30",
      "progress_note": null, "tag": null} -->
    - [x] Send agenda <!-- meta: {"id": "f9e8d7"} -->

`priority`/`status`/`source`/`progress_note`/`tag` were added for meeting-type-
aware extraction and manual task tracking (architecture_v2.md). An item whose
meta JSON predates these fields parses with `status="todo"` and
`source="legacy"` -- see `parse_todo` -- never a KeyError.

`title`/`owner_type`/`project_id`/`institution`/`confidence`/`reminder_date`/
`comments`/`attachments` were added for the task-management-platform roadmap
(manual task editing, ownership classification, structured projects). Same
guarantee as the fields above: an item whose meta JSON predates these parses
with all of them `None` (or `[]` for `comments`/`attachments`), never a
KeyError, and `format_item` only ever emits them when non-null/non-empty (the
same conditional-include pattern already used for `evidence`) so that
existing rows are rewritten byte-for-byte unchanged and simple manual tasks
that don't use these fields don't accumulate `"comments": null` clutter.

Per critique amendment 8, a malformed line (bad checklist marker, or an
unparsable JSON `meta` comment) must surface as a typed `TodoFileUnparsableError`
-- never silently dropped or silently corrupted on a subsequent write.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_ITEM_RE = re.compile(
    r"^- \[(?P<mark>[ xX])\]\s+(?P<description>.*?)"
    # meta_json accepts either a JSON object or array literal: an array is
    # valid JSON but not a valid meta payload, and must be rejected by the
    # isinstance(meta, dict) check below rather than silently failing to
    # match this group and passing through unflagged.
    r"(?:\s*<!--\s*meta:\s*(?P<meta_json>[\{\[].*[\}\]])\s*-->)?\s*$"
)


class TodoFileUnparsableError(RuntimeError):
    """Raised when data/todo.md cannot be parsed. Error code: TODO_FILE_UNPARSEABLE."""


@dataclass
class TodoItem:
    description: str
    done: bool = False
    id: str | None = None
    owner: str | None = None
    due_date: str | None = None
    session_id: str | None = None
    # Added for meeting-type-aware extraction (priority) and manual task
    # tracking (status/source/progress_note/tag). All optional and absent from
    # older todo.md entries parse to their defaults below -- no migration step
    # is needed, per parse_todo's existing meta.get(...) degrade-gracefully
    # design.
    priority: str | None = None
    status: str = "todo"
    source: str | None = None
    progress_note: str | None = None
    tag: str | None = None
    # Verbatim transcript quote captured at extraction time -- the provenance
    # answer to "why does this task exist". Optional; absent on manual tasks
    # and anything extracted before this field existed.
    evidence: str | None = None
    # Added for the task-management-platform roadmap (see module docstring).
    # `title` is the short name shown on task cards/lists; `description`
    # remains the longer body/detail text and stays required -- `title` is
    # optional and falls back to a truncated `description` in the UI when
    # absent, so existing rows with no title still render sensibly.
    title: str | None = None
    # Ownership classification (see mcp_server/tools/extraction.py's
    # USER IDENTITY block): one of self/institution/partner/organisation/
    # consortium/all_partners/external/unknown. `confidence` is the LLM's
    # own confidence (0.0-1.0) in that classification, for the review UI to
    # surface a low-confidence item for extra scrutiny.
    owner_type: str | None = None
    confidence: float | None = None
    # References Project.id in the structured Project entity (mcp_server/
    # project.py, data/projects.md) -- not a free-text project name, so that
    # "every task under Project X" is reliable regardless of how the name
    # was typed in any given meeting.
    project_id: str | None = None
    institution: str | None = None
    reminder_date: str | None = None
    # Append-only lists (see cli/review_apply.py's add_task_comment/
    # add_task_attachment): each comment is {"author": str|None, "text": str,
    # "at": iso-ts}; each attachment is {"filename": str, "path": str,
    # "added_at": iso-ts}. None (not []) when absent from an older item's meta
    # JSON, so `if item.comments:` style checks work the same as every other
    # optional field here.
    comments: list[dict] | None = None
    attachments: list[dict] | None = None


@dataclass
class TodoFile:
    items: list[TodoItem] = field(default_factory=list)


def parse_todo(path: Path | str) -> TodoFile:
    path = Path(path)
    if not path.exists():
        return TodoFile(items=[])  # first run: no todo.md yet is not an error

    items: list[TodoItem] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("- ["):
            # Not a checklist line (e.g. a heading or commentary) -- ignore it
            # rather than treating every non-item line as malformed.
            continue
        match = _ITEM_RE.match(stripped)
        if not match:
            raise TodoFileUnparsableError(
                f"{path}:{lineno}: malformed checklist line: {line!r} "
                "(TODO_FILE_UNPARSEABLE)"
            )
        meta: dict = {}
        meta_json = match.group("meta_json")
        if meta_json:
            try:
                meta = json.loads(meta_json)
            except json.JSONDecodeError as exc:
                raise TodoFileUnparsableError(
                    f"{path}:{lineno}: malformed meta JSON ({exc}) (TODO_FILE_UNPARSEABLE)"
                ) from exc
            if not isinstance(meta, dict):
                raise TodoFileUnparsableError(
                    f"{path}:{lineno}: meta JSON must be an object, got {type(meta).__name__} "
                    "(TODO_FILE_UNPARSEABLE)"
                )
        items.append(
            TodoItem(
                description=match.group("description").strip(),
                done=match.group("mark").lower() == "x",
                id=meta.get("id"),
                owner=meta.get("owner"),
                due_date=meta.get("due_date"),
                session_id=meta.get("session_id"),
                priority=meta.get("priority"),
                status=meta.get("status", "todo"),
                # An item written before this field existed has no "source" key
                # at all (not just a null value) -- that absence is exactly what
                # "legacy" is meant to flag, per architecture_v2.md's todo.md
                # extension notes.
                source=meta.get("source", "legacy"),
                progress_note=meta.get("progress_note"),
                tag=meta.get("tag"),
                evidence=meta.get("evidence"),
                title=meta.get("title"),
                owner_type=meta.get("owner_type"),
                confidence=meta.get("confidence"),
                project_id=meta.get("project_id"),
                institution=meta.get("institution"),
                reminder_date=meta.get("reminder_date"),
                comments=meta.get("comments"),
                attachments=meta.get("attachments"),
            )
        )
    return TodoFile(items=items)


def format_item(item: TodoItem) -> str:
    mark = "x" if item.done else " "
    meta = {
        "id": item.id,
        "owner": item.owner,
        "due_date": item.due_date,
        "session_id": item.session_id,
        "priority": item.priority,
        "status": item.status,
        "source": item.source,
        "progress_note": item.progress_note,
        "tag": item.tag,
    }
    if item.evidence:
        meta["evidence"] = item.evidence
    if item.title:
        meta["title"] = item.title
    if item.owner_type:
        meta["owner_type"] = item.owner_type
    if item.confidence is not None:
        meta["confidence"] = item.confidence
    if item.project_id:
        meta["project_id"] = item.project_id
    if item.institution:
        meta["institution"] = item.institution
    if item.reminder_date:
        meta["reminder_date"] = item.reminder_date
    if item.comments:
        meta["comments"] = item.comments
    if item.attachments:
        meta["attachments"] = item.attachments
    meta_json = json.dumps(meta)
    return f"- [{mark}] {item.description} <!-- meta: {meta_json} -->"


def format_todo_file(file: TodoFile) -> str:
    """Render a TodoFile back to the on-disk Markdown checklist format (M6).

    Known, deliberately-flagged limitation: `parse_todo` above only retains
    checklist lines -- any heading or free-text commentary a human has added
    to `todo.md` by hand is silently skipped during parsing, and is therefore
    NOT round-tripped by a parse_todo -> format_todo_file cycle. The reviewed-update
    applier in cli/review_apply.py (M6) is the only caller of this function, and it is acceptable for that
    narrow use because it always reads the file immediately before writing it
    back within the same locked critical section -- but this function must
    not be reused anywhere a human's surrounding prose needs preserving
    without first extending TodoFile to retain non-item lines.
    """
    return "\n".join(format_item(item) for item in file.items) + ("\n" if file.items else "")
