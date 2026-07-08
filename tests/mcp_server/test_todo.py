from __future__ import annotations

import pytest

from mcp_server.todo import TodoFileUnparsableError, format_item, parse_todo, TodoItem


def test_missing_file_returns_empty_todo(tmp_path):
    result = parse_todo(tmp_path / "todo.md")
    assert result.items == []


def test_parses_well_formed_items(tmp_path):
    path = tmp_path / "todo.md"
    path.write_text(
        '- [ ] Buy milk <!-- meta: {"id": "a1", "owner": "Naga", "due_date": "2026-07-04", "session_id": "s1"} -->\n'
        '- [x] Send agenda <!-- meta: {"id": "f9"} -->\n'
    )
    result = parse_todo(path)
    assert len(result.items) == 2
    assert result.items[0] == TodoItem(
        description="Buy milk", done=False, id="a1", owner="Naga", due_date="2026-07-04", session_id="s1",
        # This fixture's meta JSON predates priority/status/source/progress_note/tag,
        # so status/source parse to their documented "old item" defaults.
        status="todo", source="legacy",
    )
    assert result.items[1].done is True


def test_ignores_non_checklist_lines(tmp_path):
    path = tmp_path / "todo.md"
    path.write_text("# Todo\n\nSome commentary.\n- [ ] Real item\n")
    result = parse_todo(path)
    assert len(result.items) == 1
    assert result.items[0].description == "Real item"


def test_item_without_meta_comment_parses_with_nulls(tmp_path):
    path = tmp_path / "todo.md"
    path.write_text("- [ ] Plain item, no metadata\n")
    result = parse_todo(path)
    assert result.items[0].id is None
    assert result.items[0].owner is None


def test_malformed_checklist_marker_raises_unparsable(tmp_path):
    # Per critique amendment 8: a hand-edited, broken file must surface as a
    # typed error, not be silently dropped or silently corrupted on rewrite.
    path = tmp_path / "todo.md"
    path.write_text("- [?] Bad marker\n")
    with pytest.raises(TodoFileUnparsableError, match="TODO_FILE_UNPARSEABLE"):
        parse_todo(path)


def test_malformed_meta_json_raises_unparsable(tmp_path):
    path = tmp_path / "todo.md"
    path.write_text('- [ ] Item with broken meta <!-- meta: {"id": "a1", "owner": } -->\n')
    with pytest.raises(TodoFileUnparsableError, match="TODO_FILE_UNPARSEABLE"):
        parse_todo(path)


def test_meta_json_must_be_object(tmp_path):
    path = tmp_path / "todo.md"
    path.write_text('- [ ] Item <!-- meta: ["not", "an", "object"] -->\n')
    with pytest.raises(TodoFileUnparsableError, match="TODO_FILE_UNPARSEABLE"):
        parse_todo(path)


def test_format_item_roundtrips_through_parse(tmp_path):
    item = TodoItem(description="Roundtrip me", done=True, id="z9", owner=None, due_date=None, session_id="s2")
    path = tmp_path / "todo.md"
    path.write_text(format_item(item) + "\n")
    parsed = parse_todo(path).items[0]
    assert parsed == item


def test_legacy_item_parses_with_all_new_fields_none(tmp_path):
    # An item whose meta JSON predates title/owner_type/project_id/etc. must
    # parse with all of them None -- never a KeyError -- exactly like the
    # existing priority/status/source/progress_note/tag guarantee above.
    path = tmp_path / "todo.md"
    path.write_text('- [ ] Old-format item <!-- meta: {"id": "a1"} -->\n')
    item = parse_todo(path).items[0]
    assert item.title is None
    assert item.owner_type is None
    assert item.confidence is None
    assert item.project_id is None
    assert item.institution is None
    assert item.reminder_date is None
    assert item.comments is None
    assert item.attachments is None


def test_new_fields_roundtrip_through_parse(tmp_path):
    item = TodoItem(
        description="Recruit new project staff",
        id="a1b2c3",
        title="Recruit staff",
        owner="Professor Atta",
        owner_type="self",
        confidence=0.92,
        project_id="proj-42",
        institution="UREAD",
        reminder_date="2026-08-25",
        comments=[{"author": "Naga", "text": "Waiting on HR", "at": "2026-07-08T10:00:00"}],
        attachments=[{"filename": "job_spec.pdf", "path": "data/task_attachments/a1b2c3/job_spec.pdf", "added_at": "2026-07-08T10:00:00"}],
    )
    path = tmp_path / "todo.md"
    path.write_text(format_item(item) + "\n")
    parsed = parse_todo(path).items[0]
    assert parsed == item


def test_format_item_omits_new_fields_when_unset_keeping_old_rows_unchanged(tmp_path):
    # Conditional-include (same pattern as `evidence`): a plain manual task
    # that doesn't use any of the new fields must not grow
    # '"comments": null, "attachments": null, ...' clutter on every rewrite,
    # and an existing legacy row must come back out byte-identical.
    original_line = '- [ ] Old-format item <!-- meta: {"id": "a1"} -->'
    path = tmp_path / "todo.md"
    path.write_text(original_line + "\n")
    todo_file = parse_todo(path)
    rewritten = format_item(todo_file.items[0])
    assert "title" not in rewritten
    assert "owner_type" not in rewritten
    assert "confidence" not in rewritten
    assert "project_id" not in rewritten
    assert "institution" not in rewritten
    assert "reminder_date" not in rewritten
    assert "comments" not in rewritten
    assert "attachments" not in rewritten
