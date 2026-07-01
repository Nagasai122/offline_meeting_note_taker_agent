from __future__ import annotations

from datetime import date

from cli.briefing import build_daily_briefing, render_briefing
from mcp_server.state import State, create_session, transition


def _dirs(tmp_path):
    return {
        "todo_path": tmp_path / "todo.md",
        "state_dir": tmp_path / "state",
        "lock_path": tmp_path / ".lock",
    }


def _write_todo(todo_path, lines: list[str]) -> None:
    todo_path.write_text("\n".join(lines) + "\n")


def test_buckets_overdue_due_today_due_this_week_and_later_correctly(tmp_path):
    dirs = _dirs(tmp_path)
    today = date(2026, 6, 30)
    _write_todo(dirs["todo_path"], [
        '- [ ] Overdue item <!-- meta: {"id": "a", "owner": null, "due_date": "2026-06-28", "session_id": "s"} -->',
        '- [ ] Due today item <!-- meta: {"id": "b", "owner": null, "due_date": "2026-06-30", "session_id": "s"} -->',
        '- [ ] Due this week item <!-- meta: {"id": "c", "owner": null, "due_date": "2026-07-03", "session_id": "s"} -->',
        '- [ ] Due later item <!-- meta: {"id": "d", "owner": null, "due_date": "2026-08-01", "session_id": "s"} -->',
        '- [ ] No date item <!-- meta: {"id": "e", "owner": null, "due_date": null, "session_id": "s"} -->',
        '- [x] Done item, must be excluded <!-- meta: {"id": "f", "owner": null, "due_date": "2026-06-01", "session_id": "s"} -->',
    ])

    result = build_daily_briefing(dirs["todo_path"], dirs["state_dir"], today=today)
    tasks = result["tasks"]

    assert [i.id for i in tasks.overdue] == ["a"]
    assert [i.id for i in tasks.due_today] == ["b"]
    assert [i.id for i in tasks.due_this_week] == ["c"]
    assert [i.id for i in tasks.later] == ["d"]
    assert [i.id for i in tasks.no_date] == ["e"]
    assert tasks.unparsable_dates == []
    # the done item must not appear in any bucket
    all_ids = {i.id for i in tasks.overdue + tasks.due_today + tasks.due_this_week + tasks.later + tasks.no_date}
    assert "f" not in all_ids


def test_unparsable_due_date_is_flagged_not_fatal_and_not_dropped(tmp_path):
    dirs = _dirs(tmp_path)
    _write_todo(dirs["todo_path"], [
        '- [ ] Bad date item <!-- meta: {"id": "z", "owner": null, "due_date": "next Tuesday", "session_id": "s"} -->',
    ])

    result = build_daily_briefing(dirs["todo_path"], dirs["state_dir"], today=date(2026, 6, 30))

    assert [i.id for i in result["tasks"].unparsable_dates] == ["z"]
    rendered = render_briefing(result)
    assert "UNPARSABLE DUE DATE" in rendered
    assert "Bad date item" in rendered


def test_missing_todo_file_renders_cleanly_as_no_open_tasks(tmp_path):
    dirs = _dirs(tmp_path)
    # todo_path is never created -- parse_todo() already treats a missing
    # file as "no items", not an error (first-run case).

    result = build_daily_briefing(dirs["todo_path"], dirs["state_dir"], today=date(2026, 6, 30))

    rendered = render_briefing(result)
    assert "No open tasks" in rendered


def test_pipeline_status_groups_sessions_by_state_and_recency(tmp_path):
    dirs = _dirs(tmp_path)
    today = date(2026, 6, 30)
    create_session(dirs["state_dir"], "proposed-1", dirs["lock_path"], 1.0, initial_state=State.PROPOSED)
    create_session(dirs["state_dir"], "reviewed-1", dirs["lock_path"], 1.0, initial_state=State.REVIEWED)
    create_session(dirs["state_dir"], "failed-1", dirs["lock_path"], 1.0, initial_state=State.FAILED)
    # An old, already-finished session from a previous day must NOT clutter today's briefing.
    old = create_session(dirs["state_dir"], "applied-old", dirs["lock_path"], 1.0, initial_state=State.APPLIED)
    old.history[0]["at"] = "2020-01-01T00:00:00+00:00"
    import json
    (dirs["state_dir"] / "applied-old.json").write_text(json.dumps({
        "session_id": "applied-old", "state": "APPLIED", "history": old.history, "metadata": {},
    }))

    result = build_daily_briefing(dirs["todo_path"], dirs["state_dir"], today=today)
    sessions = result["sessions"]

    assert sessions["awaiting_review"] == ["proposed-1"]
    assert sessions["awaiting_apply"] == ["reviewed-1"]
    assert sessions["failed_today"] == ["failed-1"]
    assert sessions["applied_today"] == []  # the old one is excluded

    rendered = render_briefing(result)
    assert "proposed-1" in rendered
    assert "applied-old" not in rendered
