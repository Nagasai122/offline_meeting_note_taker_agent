from __future__ import annotations

import json
import subprocess

import pytest

from cli.capability import mint_capability_token
from cli.review_apply import (
    ReviewDecision,
    apply_reviewed_update,
    complete_review,
    load_pending_items,
    load_reviewed_decisions,
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
