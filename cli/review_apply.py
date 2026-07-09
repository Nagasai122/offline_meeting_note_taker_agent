"""
Core, testable logic for the M6 `review` and `apply` CLI commands.

Deliberately kept outside mcp_server/ entirely (not merely outside server.py's
import graph) -- see cli/capability.py's docstring for why structural absence
from the agent-facing package is itself part of the enforcement story for
`apply_reviewed_update`, per critique amendment 2(b).

Split from cli/main.py so that the decision logic (parsing, conflict
detection, merging) is unit-testable without going through Typer's
interactive prompts -- `cli/main.py`'s `review` command owns the
human-interaction loop (accept/reject/edit prompts) and calls
`build_review_decision`/`write_reviewed_decisions` here; `apply` owns nothing
but argument wiring and calls `apply_reviewed_update` here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from cli.capability import CapabilityToken, require_capability_token
from cli.git_backup import commit_all, ensure_repo
from concurrency.atomic import atomic_write_text
from concurrency.lock import FileLock
from mcp_server import state as state_mod
from mcp_server.schemas import validate_session_id
from mcp_server.todo import TodoFile, TodoItem, TodoFileUnparsableError, format_todo_file, parse_todo


@dataclass
class ReviewDecision:
    id: str
    decision: str  # "accept" | "reject"
    description: str
    owner: str | None
    due_date: str | None
    session_id: str | None
    priority: str | None = None
    evidence: str | None = None


def load_pending_items(pending_review_path: Path | str) -> list[TodoItem]:
    """The draft written by propose_todo_update (M4) is already in the exact
    `- [ ] desc <!-- meta: {...} -->` checklist format todo.py understands, so
    parsing it is just parse_todo() against that path -- no separate parser
    needed. Every item in a fresh draft has done=False; that is not meaningful
    here and is ignored by the review step."""
    pending_review_path = Path(pending_review_path)
    if not pending_review_path.exists():
        raise FileNotFoundError(f"No pending review draft found at {pending_review_path}.")
    return parse_todo(pending_review_path).items


def write_reviewed_decisions(reviewed_path: Path | str, decisions: list[ReviewDecision]) -> None:
    reviewed_path = Path(reviewed_path)
    reviewed_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "id": d.id, "decision": d.decision, "description": d.description,
            "owner": d.owner, "due_date": d.due_date, "session_id": d.session_id,
            "priority": d.priority, "evidence": d.evidence,
        }
        for d in decisions
    ]
    atomic_write_text(reviewed_path, json.dumps(payload, indent=2))


def load_reviewed_decisions(reviewed_path: Path | str) -> list[ReviewDecision]:
    reviewed_path = Path(reviewed_path)
    if not reviewed_path.exists():
        raise FileNotFoundError(f"No reviewed-decisions file found at {reviewed_path}.")
    raw = json.loads(reviewed_path.read_text(encoding="utf-8"))
    return [ReviewDecision(**entry) for entry in raw]


def complete_review(
    session_id: str,
    decisions: list[ReviewDecision],
    pending_review_dir: Path | str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> dict:
    """Persist the human's accept/reject/edit decisions and transition
    PROPOSED -> REVIEWED. Pure state/IO -- the interactive prompting itself
    lives in cli/main.py's `review` command, not here."""
    validate_session_id(session_id)
    reviewed_path = Path(pending_review_dir) / f"{session_id}.reviewed.json"
    write_reviewed_decisions(reviewed_path, decisions)
    session = state_mod.transition(
        state_dir, session_id, state_mod.State.REVIEWED, lock_path, lock_timeout,
        reviewed_path=str(reviewed_path),
    )
    accepted = sum(1 for d in decisions if d.decision == "accept")
    return {
        "session_id": session_id, "state": session.state.value,
        "reviewed_path": str(reviewed_path), "accepted_count": accepted,
        "rejected_count": len(decisions) - accepted,
    }


def apply_reviewed_update(
    token: CapabilityToken,
    session_id: str,
    pending_review_dir: Path | str,
    todo_path: Path | str,
    data_dir: Path | str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> dict:
    """The only function in this project permitted to write data/todo.md.

    Gated by `token` (critique amendment 2(a)) and by being structurally
    absent from mcp_server/ (amendment 2(b) -- see cli/capability.py).

    Conflict semantics (PARTIAL_APPLY_CONFLICT, amendment 3): an accepted item
    is considered conflicting if-and-only-if its `id` already exists among the
    items currently in todo.md. Given ids are freshly minted uuids per
    proposal (mcp_server/tools/review.py), this is in practice an idempotent
    double-apply guard rather than a generic duplicate-content detector --
    deliberately so: detecting semantic duplicates (e.g. two differently-worded
    descriptions of "the same" action item) is a much harder, precision/recall
    trade-off-laden problem that risks silently dropping a legitimate item on
    a false-positive match. That is flagged here as a deliberately deferred
    enhancement, not an oversight, should the user later want it.

    On a conflict, that single item is skipped (not applied); the rest of the
    accepted items still apply; both the existing todo.md item and the skipped
    incoming item are returned under "conflicts" for manual reconciliation, per
    amendment 3's "both versions are shown" requirement -- this function does
    not attempt to reconcile them itself.
    """
    require_capability_token(token)
    validate_session_id(session_id)

    # Fail fast, before any I/O or git mutation, if the session is not in
    # REVIEWED state. Originally this was only discovered by the final
    # transition() call below, at which point todo.md had already been
    # rewritten and committed -- harmless by chance on a pure re-apply (every
    # decision collides, so the rewrite is a no-op), but not guaranteed
    # harmless in general (e.g. a stale reviewed.json left over after a
    # session moved on to FAILED some other way). Checked here explicitly so
    # an invalid-state apply is always a clean no-op, never a partially
    # executed one. Found via manual stress testing of a double-apply.
    state_dir = Path(state_dir)
    current = state_mod.load_session_state(state_dir, session_id)
    if current.state != state_mod.State.REVIEWED:
        raise state_mod.InvalidTransitionError(
            f"Cannot apply session '{session_id}': current state is "
            f"{current.state.value}, expected REVIEWED. No changes were made."
        )

    data_dir = Path(data_dir)
    todo_path = Path(todo_path)
    reviewed_path = Path(pending_review_dir) / f"{session_id}.reviewed.json"
    decisions = load_reviewed_decisions(reviewed_path)

    ensure_repo(data_dir)
    pre_hash = commit_all(data_dir, f"pre-apply snapshot: session '{session_id}'")

    try:
        existing = parse_todo(todo_path)
    except TodoFileUnparsableError as exc:
        state_mod.transition(
            state_dir, session_id, state_mod.State.FAILED, lock_path, lock_timeout,
            error=f"TODO_FILE_UNPARSEABLE: {exc}",
        )
        raise

    # Soft-deleted items (status="deleted", see update_task_status) are excluded
    # from the conflict check: a deleted record's id must not permanently block
    # a future item that happens to reuse it (vanishingly rare with fresh uuid4
    # ids, but correct behaviour matters more than the odds). The deleted row
    # itself is untouched -- this only changes what counts as "already present"
    # for PARTIAL_APPLY_CONFLICT purposes.
    existing_by_id = {
        item.id: item for item in existing.items
        if item.id is not None and item.status != "deleted"
    }

    applied: list[TodoItem] = []
    conflicts: list[dict] = []
    for decision in decisions:
        if decision.decision != "accept":
            continue
        if decision.id in existing_by_id:
            conflicts.append({
                "id": decision.id,
                "existing": _item_to_dict(existing_by_id[decision.id]),
                "incoming": {
                    "description": decision.description, "owner": decision.owner,
                    "due_date": decision.due_date, "session_id": decision.session_id,
                },
            })
            continue
        applied.append(
            TodoItem(
                description=decision.description, done=False, id=decision.id,
                owner=decision.owner, due_date=decision.due_date, session_id=decision.session_id,
                priority=decision.priority, status="todo", source=decision.session_id,
                evidence=decision.evidence,
            )
        )

    merged = TodoFile(items=existing.items + applied)
    atomic_write_text(todo_path, format_todo_file(merged))

    post_hash = commit_all(data_dir, f"post-apply: session '{session_id}' ({len(applied)} item(s))")

    session = state_mod.transition(
        state_dir, session_id, state_mod.State.APPLIED, lock_path, lock_timeout,
        applied_count=len(applied), conflict_count=len(conflicts),
        conflicts=conflicts, pre_apply_commit=pre_hash, post_apply_commit=post_hash,
    )

    # The transition() call above writes state_dir/<session_id>.json AFTER
    # the post-apply commit was taken, so that write is itself left
    # uncommitted by the commit above -- amendment 5's git undo path must
    # never leave data/ dirty on return. A trailing commit closes that gap.
    # Its hash is deliberately not threaded back into the session's own
    # metadata (that would need a second transition purely to record a hash
    # whose only consumer is `git log`/`git revert`, for marginal benefit).
    commit_all(data_dir, f"post-apply state record: session '{session_id}'")

    return {
        "session_id": session_id, "state": session.state.value,
        "applied_count": len(applied), "conflicts": conflicts,
        "pre_apply_commit": pre_hash, "post_apply_commit": post_hash,
    }


def write_manual_task(
    token: CapabilityToken,
    task_data: dict,
    todo_path: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> str:
    """Append a user-created manual task to todo.md (architecture_v2.md §Phase 7.2).

    Gated by `token` the same way `apply_reviewed_update` is -- this is the
    second function permitted to write data/todo.md, both living in cli/
    (never mcp_server/) per the capability-token invariant.

    Args:
        token: A genuine CapabilityToken from cli.capability.mint_capability_token().
        task_data: dict with "description" (required) and optional
            "title"/"owner"/"due_date"/"priority"/"tag"/"progress_note"/
            "project_id"/"reminder_date"/"status".
        todo_path: Path to data/todo.md.
        lock_path: Concurrency lock path.
        lock_timeout: Lock acquisition timeout in seconds.

    Returns:
        The freshly-minted task id.
    """
    require_capability_token(token)
    from uuid import uuid4

    todo_path = Path(todo_path)
    task_id = uuid4().hex[:8]
    # Bug fix: a caller-supplied status="done" (e.g. logging already-
    # completed work) used to leave `done` hardcoded to False, unlike
    # update_task_status's handling of the same status value -- the
    # resulting status="done"/done=False item was inconsistent state that
    # kept showing up in cli/briefing.py's open-task dashboard buckets
    # (which filter on `done`, not `status`) despite being marked done.
    requested_status = task_data.get("status") or "todo"
    item = TodoItem(
        description=task_data["description"],
        done=requested_status == "done",
        id=task_id,
        title=task_data.get("title"),
        owner=task_data.get("owner"),
        # A human typing a task in directly is, by definition, describing
        # their own work -- "self" is the sensible default owner_type for
        # every manually-created task, distinct from the "unknown" default
        # an LLM extraction falls back to when the transcript gives no clear
        # ownership signal (see mcp_server/tools/extraction.py).
        owner_type="self",
        due_date=task_data.get("due_date"),
        session_id=None,
        priority=task_data.get("priority") or "MEDIUM",
        status=requested_status,
        source="manual",
        progress_note=task_data.get("progress_note"),
        tag=task_data.get("tag"),
        project_id=task_data.get("project_id"),
        reminder_date=task_data.get("reminder_date"),
    )

    with FileLock(lock_path, timeout_seconds=lock_timeout):
        existing = parse_todo(todo_path)
        merged = TodoFile(items=existing.items + [item])
        atomic_write_text(todo_path, format_todo_file(merged))

    return task_id


def update_task_status(
    token: CapabilityToken,
    task_id: str,
    updates: dict,
    todo_path: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> TodoItem:
    """Update a subset of fields on an existing todo.md item by id, or mark
    it deleted (soft delete -- the record stays in the file with
    status="deleted", per architecture_v2.md §Phase 7.2's DELETE endpoint
    spec). Same CapabilityToken gating as write_manual_task/
    apply_reviewed_update.

    Widened for full task editing (P1.5) beyond the original status/
    due_date/progress_note/priority set -- title/description/owner/
    project_id/institution/tag/reminder_date are all plain scalar
    overwrites, same shape as the original four. `owner_type` is
    intentionally included here too (the allow-list itself doesn't need to
    change again once P2 lands its ownership vocabulary) even though the
    edit UI doesn't expose it yet. `comments`/`attachments` are deliberately
    NOT in this allow-list -- those are append-only lists, not scalar
    overwrites, and are handled by add_task_comment/add_task_attachment
    below instead, each under their own locked read-modify-write.

    Raises:
        KeyError: if no item with `task_id` exists.
    """
    require_capability_token(token)

    with FileLock(lock_path, timeout_seconds=lock_timeout):
        todo_path = Path(todo_path)
        existing = parse_todo(todo_path)
        target = next((item for item in existing.items if item.id == task_id), None)
        if target is None:
            raise KeyError(f"No task with id '{task_id}' found in {todo_path}.")

        for field in (
            "title", "description", "owner", "owner_type", "due_date", "priority",
            "status", "project_id", "institution", "tag", "progress_note", "reminder_date",
        ):
            if field in updates and updates[field] is not None:
                setattr(target, field, updates[field])
        if updates.get("status") == "done":
            target.done = True
        elif "status" in updates and updates["status"] is not None:
            target.done = False

        atomic_write_text(todo_path, format_todo_file(existing))
        return target


def duplicate_task(
    token: CapabilityToken,
    task_id: str,
    todo_path: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> TodoItem:
    """Clone an existing todo.md item under a fresh id. `source` is set to
    "duplicate-of-<original_id>" (not "manual" or the original session_id)
    so the duplicate's provenance isn't lost -- it's neither a fresh manual
    task nor an extraction from the original meeting, but a copy of one.
    The clone starts at status="todo"/done=False regardless of the
    original's state, since duplicating a done/blocked task to track a new
    occurrence of the same work should not inherit that history.

    Raises:
        KeyError: if no item with `task_id` exists.
    """
    require_capability_token(token)
    from uuid import uuid4

    with FileLock(lock_path, timeout_seconds=lock_timeout):
        todo_path = Path(todo_path)
        existing = parse_todo(todo_path)
        source_item = next((item for item in existing.items if item.id == task_id), None)
        if source_item is None:
            raise KeyError(f"No task with id '{task_id}' found in {todo_path}.")

        # Bug fix: dataclasses.replace only overrides the fields passed here
        # -- comments/attachments were left unmentioned, so the clone
        # inherited the SAME list objects (and hence the original's entire
        # comment thread and file references) from source_item, directly
        # contradicting this function's own "should not inherit that
        # history" reasoning above. Reset both explicitly.
        clone = replace(
            source_item,
            id=uuid4().hex[:8],
            done=False,
            status="todo",
            source=f"duplicate-of-{task_id}",
            comments=None,
            attachments=None,
        )
        merged = TodoFile(items=existing.items + [clone])
        atomic_write_text(todo_path, format_todo_file(merged))
        return clone


def add_task_comment(
    token: CapabilityToken,
    task_id: str,
    author: str | None,
    text: str,
    todo_path: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> TodoItem:
    """Append a comment to an existing task's `comments` list.

    Append-only read-modify-write, not a scalar overwrite -- kept separate
    from update_task_status's generic allow-list loop (which would replace
    the whole list rather than append to it) for the same reason
    add_task_attachment below is.

    Raises:
        KeyError: if no item with `task_id` exists.
    """
    require_capability_token(token)
    from datetime import datetime

    with FileLock(lock_path, timeout_seconds=lock_timeout):
        todo_path = Path(todo_path)
        existing = parse_todo(todo_path)
        target = next((item for item in existing.items if item.id == task_id), None)
        if target is None:
            raise KeyError(f"No task with id '{task_id}' found in {todo_path}.")

        comment = {"author": author, "text": text, "at": datetime.now().isoformat()}
        target.comments = (target.comments or []) + [comment]

        atomic_write_text(todo_path, format_todo_file(existing))
        return target


def add_task_attachment(
    token: CapabilityToken,
    task_id: str,
    filename: str,
    relative_path: str,
    todo_path: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
) -> TodoItem:
    """Append an attachment reference to an existing task's `attachments`
    list. The caller (cli/web.py's POST /api/tasks/{id}/attachments) is
    responsible for actually saving the file to disk (under
    data/task_attachments/<task_id>/) before calling this -- this function
    only records the reference in todo.md, under the same lock every other
    todo.md write uses.

    Raises:
        KeyError: if no item with `task_id` exists.
    """
    require_capability_token(token)
    from datetime import datetime

    with FileLock(lock_path, timeout_seconds=lock_timeout):
        todo_path = Path(todo_path)
        existing = parse_todo(todo_path)
        target = next((item for item in existing.items if item.id == task_id), None)
        if target is None:
            raise KeyError(f"No task with id '{task_id}' found in {todo_path}.")

        attachment = {"filename": filename, "path": relative_path, "added_at": datetime.now().isoformat()}
        target.attachments = (target.attachments or []) + [attachment]

        atomic_write_text(todo_path, format_todo_file(existing))
        return target


def _item_to_dict(item: TodoItem) -> dict:
    return {
        "id": item.id, "description": item.description, "done": item.done,
        "owner": item.owner, "due_date": item.due_date, "session_id": item.session_id,
        "priority": item.priority, "status": item.status,
    }
