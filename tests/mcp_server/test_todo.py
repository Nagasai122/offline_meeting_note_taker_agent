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
