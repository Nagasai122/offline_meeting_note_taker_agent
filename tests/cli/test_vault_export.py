"""Tests for cli/vault_export.py (Obsidian/Markdown-vault export)."""

from __future__ import annotations

import pytest

from cli.vault_export import ExportError, export_all, export_session
from mcp_server.state import State, create_session


def _seed(tmp_path, session_id: str, state: State, *, with_artifacts: bool = True):
    meetings = tmp_path / "meetings"
    meetings.mkdir(exist_ok=True)
    create_session(tmp_path / "state", session_id, tmp_path / ".lock", 1.0,
                   initial_state=state, meeting_type="is-call")
    if with_artifacts:
        (meetings / f"{session_id}.md").write_text("transcript text", encoding="utf-8")
        (meetings / f"{session_id}.summary.md").write_text("- decided X\n", encoding="utf-8")
        (meetings / f"{session_id}.mom.md").write_text("# MoM\ncontent", encoding="utf-8")
    return meetings


@pytest.fixture()
def todo(tmp_path):
    todo_path = tmp_path / "todo.md"
    todo_path.write_text(
        '- [ ] Draft ADR <!-- meta: {"id": "a1", "session_id": "is-call-20260701-090000", '
        '"owner": "Naga", "due_date": "2026-07-10", "evidence": "please draft the ADR"} -->\n'
        '- [x] Unrelated <!-- meta: {"id": "b2", "session_id": "other"} -->\n',
        encoding="utf-8",
    )
    return todo_path


def test_export_session_writes_frontmatter_items_and_mom(tmp_path, todo):
    meetings = _seed(tmp_path, "is-call-20260701-090000", State.APPLIED)
    vault = tmp_path / "vault"

    out = export_session("is-call-20260701-090000", meetings, todo, tmp_path / "state", vault)

    assert out == vault / "Meetings" / "2026-07-01 is-call.md"
    text = out.read_text(encoding="utf-8")
    assert text.startswith("---\ndate: 2026-07-01\ntype: is-call\n")
    assert "- [ ] Draft ADR (owner: Naga, due: 2026-07-10)" in text
    assert '"please draft the ADR"' in text  # evidence provenance
    assert "Unrelated" not in text           # other sessions' items excluded
    assert "# MoM" in text


def test_export_links_chained_prior_sessions(tmp_path, todo):
    meetings = _seed(tmp_path, "is-call-20260701-090000", State.APPLIED)
    _seed(tmp_path, "is-call-20260630-090000", State.APPLIED)

    out = export_session("is-call-20260701-090000", meetings, todo, tmp_path / "state", tmp_path / "v")

    assert "[[2026-06-30 is-call]]" in out.read_text(encoding="utf-8")


def test_export_refuses_unreviewed_session(tmp_path, todo):
    meetings = _seed(tmp_path, "raw-20260701-090000", State.TRANSCRIBED)
    with pytest.raises(ExportError, match="human review"):
        export_session("raw-20260701-090000", meetings, todo, tmp_path / "state", tmp_path / "v")


def test_export_requires_vault_dir(tmp_path, todo):
    meetings = _seed(tmp_path, "is-call-20260701-090000", State.APPLIED)
    with pytest.raises(ExportError, match="vault"):
        export_session("is-call-20260701-090000", meetings, todo, tmp_path / "state", "")


def test_export_all_skips_unexportable_and_is_idempotent(tmp_path, todo):
    meetings = _seed(tmp_path, "is-call-20260701-090000", State.APPLIED)
    _seed(tmp_path, "raw-20260702-090000", State.TRANSCRIBED)
    vault = tmp_path / "vault"

    first = export_all(meetings, todo, tmp_path / "state", vault)
    second = export_all(meetings, todo, tmp_path / "state", vault)

    assert [p.name for p in first] == ["2026-07-01 is-call.md"]
    assert first == second
    assert len(list((vault / "Meetings").glob("*.md"))) == 1
