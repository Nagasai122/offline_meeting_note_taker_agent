from __future__ import annotations

import json
import subprocess

import pytest

from cli.capability import mint_capability_token
from cli.review_apply import (
    ReviewDecision,
    add_task_attachment,
    add_task_comment,
    apply_reviewed_update,
    complete_review,
    duplicate_task,
    load_pending_items,
    load_reviewed_decisions,
    update_task_status,
    write_manual_task,
    write_reviewed_decisions,
)
from mcp_server.state import InvalidTransitionError, State, create_session, load_session_state
from mcp_server.todo import TodoFileUnparsableError, parse_todo


def _dirs(tmp_path):
    return {
        "pending_review_dir": tmp_path / "pending_review",
        "todo_path": tmp_path / "todo.md",
        "data_dir": tmp_path,
        "state_dir": tmp_path / "state",
        "lock_path": tmp_path / ".lock",
    }


def _write_draft(pending_review_dir, session_id: str, items: list[dict]) -> None:
    pending_review_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# Proposed todo updates -- session {session_id}", ""]
    for item in items:
        meta = {
            "id": item["id"], "owner": item.get("owner"),
            "due_date": item.get("due_date"), "session_id": session_id,
        }
        lines.append(f"- [ ] {item['description']} <!-- meta: {json.dumps(meta)} -->")
    (pending_review_dir / f"{session_id}.md").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# load_pending_items
# ---------------------------------------------------------------------------

def test_load_pending_items_parses_the_propose_todo_update_draft_format(tmp_path):
    dirs = _dirs(tmp_path)
    _write_draft(dirs["pending_review_dir"], "s1", [{"id": "aaa11111", "description": "Send the report", "owner": "Naga"}])

    items = load_pending_items(dirs["pending_review_dir"] / "s1.md")

    assert len(items) == 1
    assert items[0].id == "aaa11111"
    assert items[0].description == "Send the report"
    assert items[0].owner == "Naga"


def test_load_pending_items_missing_file_raises(tmp_path):
    dirs = _dirs(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_pending_items(dirs["pending_review_dir"] / "nope.md")


# ---------------------------------------------------------------------------
# complete_review
# ---------------------------------------------------------------------------

def test_complete_review_writes_decisions_and_transitions_to_reviewed(tmp_path):
    dirs = _dirs(tmp_path)
    create_session(dirs["state_dir"], "s1", dirs["lock_path"], 1.0, initial_state=State.PROPOSED)
    decisions = [
        ReviewDecision(id="a", decision="accept", description="Do X", owner="Naga", due_date="2026-07-04", session_id="s1"),
        ReviewDecision(id="b", decision="reject", description="Do Y", owner=None, due_date=None, session_id="s1"),
    ]

    result = complete_review(
        "s1", decisions, dirs["pending_review_dir"], dirs["state_dir"], dirs["lock_path"], 1.0,
    )

    assert result["state"] == "REVIEWED"
    assert result["accepted_count"] == 1
    assert result["rejected_count"] == 1
    assert load_session_state(dirs["state_dir"], "s1").state == State.REVIEWED

    reloaded = load_reviewed_decisions(dirs["pending_review_dir"] / "s1.reviewed.json")
    assert [d.id for d in reloaded] == ["a", "b"]


# ---------------------------------------------------------------------------
# apply_reviewed_update
# ---------------------------------------------------------------------------

def _reviewed(dirs, session_id, decisions):
    write_reviewed_decisions(dirs["pending_review_dir"] / f"{session_id}.reviewed.json", decisions)


def test_apply_happy_path_writes_todo_and_transitions_to_applied(tmp_path):
    dirs = _dirs(tmp_path)
    create_session(dirs["state_dir"], "s1", dirs["lock_path"], 1.0, initial_state=State.REVIEWED)
    _reviewed(dirs, "s1", [
        ReviewDecision(id="a", decision="accept", description="Do X", owner="Naga", due_date="2026-07-04", session_id="s1"),
        ReviewDecision(id="b", decision="reject", description="Do Y", owner=None, due_date=None, session_id="s1"),
    ])
    token = mint_capability_token()

    result = apply_reviewed_update(
        token, "s1", dirs["pending_review_dir"], dirs["todo_path"], dirs["data_dir"],
        dirs["state_dir"], dirs["lock_path"], 1.0,
    )

    assert result["state"] == "APPLIED"
    assert result["applied_count"] == 1
    assert result["conflicts"] == []
    # pre_apply_commit may legitimately be a real hash here (it captures
    # whatever create_session() already wrote under state_dir, since ensure_repo
    # only initialises the git repo at the start of apply_reviewed_update, not
    # at session-creation time) -- the only hard guarantee is that the
    # post-apply commit exists and reflects the actual todo.md write.
    assert result["pre_apply_commit"] is None or isinstance(result["pre_apply_commit"], str)
    assert result["post_apply_commit"] is not None

    todo = parse_todo(dirs["todo_path"])
    assert len(todo.items) == 1
    assert todo.items[0].id == "a"
    assert todo.items[0].description == "Do X"
    assert load_session_state(dirs["state_dir"], "s1").state == State.APPLIED


def test_apply_rejects_a_forged_token_and_makes_no_changes(tmp_path):
    dirs = _dirs(tmp_path)
    create_session(dirs["state_dir"], "s1", dirs["lock_path"], 1.0, initial_state=State.REVIEWED)
    _reviewed(dirs, "s1", [ReviewDecision(id="a", decision="accept", description="Do X", owner=None, due_date=None, session_id="s1")])

    with pytest.raises(TypeError):
        apply_reviewed_update(
            "not-a-real-token", "s1", dirs["pending_review_dir"], dirs["todo_path"], dirs["data_dir"],
            dirs["state_dir"], dirs["lock_path"], 1.0,
        )

    assert not dirs["todo_path"].exists()
    assert load_session_state(dirs["state_dir"], "s1").state == State.REVIEWED  # unchanged


def test_apply_with_id_collision_is_a_partial_apply_conflict(tmp_path):
    dirs = _dirs(tmp_path)
    dirs["todo_path"].write_text('- [ ] Pre-existing item <!-- meta: {"id": "a", "owner": null, "due_date": null, "session_id": "earlier"} -->\n')
    create_session(dirs["state_dir"], "s2", dirs["lock_path"], 1.0, initial_state=State.REVIEWED)
    _reviewed(dirs, "s2", [
        ReviewDecision(id="a", decision="accept", description="Colliding item", owner="Naga", due_date=None, session_id="s2"),
        ReviewDecision(id="c", decision="accept", description="Clean item", owner=None, due_date=None, session_id="s2"),
    ])
    token = mint_capability_token()

    result = apply_reviewed_update(
        token, "s2", dirs["pending_review_dir"], dirs["todo_path"], dirs["data_dir"],
        dirs["state_dir"], dirs["lock_path"], 1.0,
    )

    assert result["applied_count"] == 1  # only "c" applied
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["id"] == "a"
    assert conflict["existing"]["description"] == "Pre-existing item"
    assert conflict["incoming"]["description"] == "Colliding item"

    todo = parse_todo(dirs["todo_path"])
    descriptions = {item.description for item in todo.items}
    assert descriptions == {"Pre-existing item", "Clean item"}  # collision skipped, both others present
    assert load_session_state(dirs["state_dir"], "s2").state == State.APPLIED


def test_apply_with_malformed_existing_todo_fails_loudly_and_transitions_to_failed(tmp_path):
    dirs = _dirs(tmp_path)
    dirs["todo_path"].write_text("- [?] a hand-edited, broken line\n")
    create_session(dirs["state_dir"], "s3", dirs["lock_path"], 1.0, initial_state=State.REVIEWED)
    _reviewed(dirs, "s3", [ReviewDecision(id="a", decision="accept", description="X", owner=None, due_date=None, session_id="s3")])
    token = mint_capability_token()

    with pytest.raises(TodoFileUnparsableError):
        apply_reviewed_update(
            token, "s3", dirs["pending_review_dir"], dirs["todo_path"], dirs["data_dir"],
            dirs["state_dir"], dirs["lock_path"], 1.0,
        )

    session = load_session_state(dirs["state_dir"], "s3")
    assert session.state == State.FAILED
    assert "TODO_FILE_UNPARSEABLE" in session.metadata["error"]


def test_apply_on_an_already_applied_session_is_rejected_with_no_side_effects(tmp_path):
    """Regression test for a bug found via manual stress testing: re-running
    apply against a session already in APPLIED state used to be discovered
    only by the final transition() call, by which point todo.md had already
    been rewritten and committed. Must now fail fast, before any mutation."""
    dirs = _dirs(tmp_path)
    create_session(dirs["state_dir"], "s5", dirs["lock_path"], 1.0, initial_state=State.REVIEWED)
    _reviewed(dirs, "s5", [ReviewDecision(id="a", decision="accept", description="Do X", owner=None, due_date=None, session_id="s5")])
    token = mint_capability_token()
    apply_reviewed_update(
        token, "s5", dirs["pending_review_dir"], dirs["todo_path"], dirs["data_dir"],
        dirs["state_dir"], dirs["lock_path"], 1.0,
    )
    todo_mtime_before = dirs["todo_path"].stat().st_mtime

    with pytest.raises(InvalidTransitionError):
        apply_reviewed_update(
            token, "s5", dirs["pending_review_dir"], dirs["todo_path"], dirs["data_dir"],
            dirs["state_dir"], dirs["lock_path"], 1.0,
        )

    assert dirs["todo_path"].stat().st_mtime == todo_mtime_before  # untouched second time
    assert load_session_state(dirs["state_dir"], "s5").state == State.APPLIED


def test_apply_leaves_the_git_working_tree_clean(tmp_path):
    """Regression test for a bug found via manual stress testing: the
    post-apply commit was taken BEFORE the state transition wrote
    state/<session_id>.json, leaving that write uncommitted (a dirty working
    tree) on return -- defeating amendment 5's git-based undo guarantee."""
    dirs = _dirs(tmp_path)
    create_session(dirs["state_dir"], "s6", dirs["lock_path"], 1.0, initial_state=State.REVIEWED)
    _reviewed(dirs, "s6", [ReviewDecision(id="a", decision="accept", description="Do X", owner=None, due_date=None, session_id="s6")])

    apply_reviewed_update(
        mint_capability_token(), "s6", dirs["pending_review_dir"], dirs["todo_path"],
        dirs["data_dir"], dirs["state_dir"], dirs["lock_path"], 1.0,
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=dirs["data_dir"], capture_output=True, text=True, check=True,
    )
    assert status.stdout == ""  # nothing left uncommitted


def test_apply_missing_reviewed_file_raises(tmp_path):
    dirs = _dirs(tmp_path)
    create_session(dirs["state_dir"], "s4", dirs["lock_path"], 1.0, initial_state=State.REVIEWED)
    token = mint_capability_token()

    with pytest.raises(FileNotFoundError):
        apply_reviewed_update(
            token, "s4", dirs["pending_review_dir"], dirs["todo_path"], dirs["data_dir"],
            dirs["state_dir"], dirs["lock_path"], 1.0,
        )


def test_write_manual_task_full_field_set(tmp_path):
    dirs = _dirs(tmp_path)
    token = mint_capability_token()

    task_id = write_manual_task(
        token,
        {
            "description": "Recruit new project staff",
            "title": "Recruit staff",
            "owner": "Professor Atta",
            "due_date": "2026-08-31",
            "priority": "HIGH",
            "status": "in_progress",
            "tag": "Recruitment",
            "progress_note": "Waiting for HR approval.",
            "project_id": "cybersec-proj",
            "reminder_date": "2026-08-25",
        },
        dirs["todo_path"], dirs["lock_path"], 1.0,
    )

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.id == task_id
    assert item.title == "Recruit staff"
    assert item.owner == "Professor Atta"
    # A manually-created task is, by definition, the user's own -- default
    # owner_type is "self", distinct from extraction's "unknown" default.
    assert item.owner_type == "self"
    assert item.status == "in_progress"
    assert item.project_id == "cybersec-proj"
    assert item.reminder_date == "2026-08-25"
    assert item.source == "manual"


def test_write_manual_task_defaults_status_to_todo_when_omitted(tmp_path):
    dirs = _dirs(tmp_path)
    token = mint_capability_token()

    write_manual_task(token, {"description": "Bare-minimum task"}, dirs["todo_path"], dirs["lock_path"], 1.0)

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.status == "todo"
    assert item.done is False


def test_write_manual_task_status_done_sets_done_flag(tmp_path):
    """Regression test: a manual task created with status="done" (e.g.
    logging already-completed work) used to leave `done` hardcoded False,
    producing an inconsistent status="done"/done=False item that
    cli/briefing.py's bucket_open_tasks (which filters on `done`, not
    `status`) would keep showing as an open task on the dashboard."""
    dirs = _dirs(tmp_path)
    token = mint_capability_token()

    write_manual_task(
        token, {"description": "Already finished this", "status": "done"},
        dirs["todo_path"], dirs["lock_path"], 1.0,
    )

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.status == "done"
    assert item.done is True
    assert item.title is None
    assert item.project_id is None


def test_update_task_status_widened_allow_list(tmp_path):
    """P1.5: update_task_status now accepts the full edit field set, not
    just the original status/due_date/progress_note/priority four."""
    dirs = _dirs(tmp_path)
    write_manual_task(mint_capability_token(), {"description": "Original"}, dirs["todo_path"], dirs["lock_path"], 1.0)
    task_id = parse_todo(dirs["todo_path"]).items[0].id

    update_task_status(
        mint_capability_token(), task_id,
        {
            "title": "New title", "description": "New description", "owner": "Naga",
            "project_id": "proj-1", "institution": "UREAD", "tag": "WP2",
            "reminder_date": "2026-09-01",
        },
        dirs["todo_path"], dirs["lock_path"], 1.0,
    )

    item = parse_todo(dirs["todo_path"]).items[0]
    assert item.title == "New title"
    assert item.description == "New description"
    assert item.owner == "Naga"
    assert item.project_id == "proj-1"
    assert item.institution == "UREAD"
    assert item.tag == "WP2"
    assert item.reminder_date == "2026-09-01"


def test_duplicate_task_clones_under_fresh_id_reset_to_todo(tmp_path):
    dirs = _dirs(tmp_path)
    write_manual_task(
        mint_capability_token(),
        {"description": "Recurring task", "owner": "Naga", "priority": "HIGH", "status": "done"},
        dirs["todo_path"], dirs["lock_path"], 1.0,
    )
    original = parse_todo(dirs["todo_path"]).items[0]
    # Simulate it having been completed before duplicating.
    update_task_status(mint_capability_token(), original.id, {"status": "done"}, dirs["todo_path"], dirs["lock_path"], 1.0)

    clone = duplicate_task(mint_capability_token(), original.id, dirs["todo_path"], dirs["lock_path"], 1.0)

    assert clone.id != original.id
    assert clone.description == "Recurring task"
    assert clone.owner == "Naga"
    assert clone.priority == "HIGH"
    # The clone starts fresh, not inheriting the original's completed state.
    assert clone.status == "todo"
    assert clone.done is False
    assert clone.source == f"duplicate-of-{original.id}"

    todo = parse_todo(dirs["todo_path"])
    assert len(todo.items) == 2  # original preserved alongside the clone


def test_duplicate_task_unknown_id_raises(tmp_path):
    dirs = _dirs(tmp_path)
    with pytest.raises(KeyError):
        duplicate_task(mint_capability_token(), "does-not-exist", dirs["todo_path"], dirs["lock_path"], 1.0)


def test_duplicate_task_does_not_inherit_comments_or_attachments(tmp_path):
    """Regression test: dataclasses.replace only overrides the fields passed
    to it -- comments/attachments were previously left unmentioned, so the
    clone shared the SAME list objects as the source, silently carrying over
    the original's comment thread and file references despite this
    function's own docstring saying a duplicate "should not inherit that
    history"."""
    dirs = _dirs(tmp_path)
    write_manual_task(
        mint_capability_token(), {"description": "Task with history"},
        dirs["todo_path"], dirs["lock_path"], 1.0,
    )
    original = parse_todo(dirs["todo_path"]).items[0]
    add_task_comment(mint_capability_token(), original.id, "Naga", "Old comment", dirs["todo_path"], dirs["lock_path"], 1.0)
    add_task_attachment(mint_capability_token(), original.id, "old.pdf", "task_attachments/x/old.pdf", dirs["todo_path"], dirs["lock_path"], 1.0)

    clone = duplicate_task(mint_capability_token(), original.id, dirs["todo_path"], dirs["lock_path"], 1.0)

    assert clone.comments is None
    assert clone.attachments is None
    # The original itself must be untouched by the duplication.
    original_after = next(i for i in parse_todo(dirs["todo_path"]).items if i.id == original.id)
    assert len(original_after.comments) == 1
    assert len(original_after.attachments) == 1


def test_add_task_comment_appends_without_overwriting(tmp_path):
    dirs = _dirs(tmp_path)
    write_manual_task(mint_capability_token(), {"description": "Task with comments"}, dirs["todo_path"], dirs["lock_path"], 1.0)
    task_id = parse_todo(dirs["todo_path"]).items[0].id

    add_task_comment(mint_capability_token(), task_id, "Naga", "First comment", dirs["todo_path"], dirs["lock_path"], 1.0)
    item = add_task_comment(mint_capability_token(), task_id, "Dave", "Second comment", dirs["todo_path"], dirs["lock_path"], 1.0)

    assert len(item.comments) == 2
    assert item.comments[0]["author"] == "Naga"
    assert item.comments[0]["text"] == "First comment"
    assert item.comments[1]["author"] == "Dave"
    assert item.comments[1]["text"] == "Second comment"
    assert "at" in item.comments[0]


def test_add_task_attachment_appends_without_overwriting(tmp_path):
    dirs = _dirs(tmp_path)
    write_manual_task(mint_capability_token(), {"description": "Task with files"}, dirs["todo_path"], dirs["lock_path"], 1.0)
    task_id = parse_todo(dirs["todo_path"]).items[0].id

    add_task_attachment(mint_capability_token(), task_id, "a.pdf", "task_attachments/x/a.pdf", dirs["todo_path"], dirs["lock_path"], 1.0)
    item = add_task_attachment(mint_capability_token(), task_id, "b.png", "task_attachments/x/b.png", dirs["todo_path"], dirs["lock_path"], 1.0)

    assert len(item.attachments) == 2
    assert item.attachments[0]["filename"] == "a.pdf"
    assert item.attachments[1]["filename"] == "b.png"
