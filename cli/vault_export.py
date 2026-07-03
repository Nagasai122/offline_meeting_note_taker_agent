"""
Obsidian / Markdown-vault export (audit 2026-07, Strand E candidate 4 — approved).

Renders one session's reviewed artefacts (MoM, summary, applied action items)
into `<vault>/Meetings/<yyyy-mm-dd> <slug>.md` with YAML frontmatter and
`[[wiki-links]]` to chained sessions of the same slug.

Design constraints, stated where they are enforced:
- One-way and idempotent: re-exporting the same session overwrites its own
  file and nothing else. No vault reads beyond existence checks, no sync.
- This is the only module that writes outside `data/` — the target path is
  user-chosen configuration ([export].vault_dir or an explicit --vault),
  never derived from content, and no MCP tool can reach it.
- Export only re-renders content a human already reviewed (PROPOSED or later
  sessions); it is not token-gated because it cannot touch todo.md.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from concurrency.atomic import atomic_write_text
from mcp_server import state as state_mod
from mcp_server.schemas import validate_session_id
from mcp_server.todo import parse_todo

_SESSION_TS_RE = re.compile(r"^(?P<slug>.*)-(?P<date>\d{8})-(?P<time>\d{6})$")

EXPORTABLE_STATES = {
    state_mod.State.PROPOSED,
    state_mod.State.REVIEWED,
    state_mod.State.APPLIED,
}


class ExportError(RuntimeError):
    pass


def _session_date_and_slug(session_id: str) -> tuple[str, str]:
    m = _SESSION_TS_RE.match(session_id)
    if m:
        d = m.group("date")
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}", m.group("slug")
    return datetime.now().date().isoformat(), session_id


def _chained_session_ids(session_id: str, meetings_dir: Path, limit: int = 3) -> list[str]:
    """Prior sessions of the same slug (same convention extraction chaining uses)."""
    m = _SESSION_TS_RE.match(session_id)
    if not m:
        return []
    slug = m.group("slug")
    prior = []
    for p in meetings_dir.glob(f"{slug}-*.md"):
        pm = _SESSION_TS_RE.match(p.stem)
        if pm and p.stem != session_id and pm.group("slug") == slug:
            if p.stem < session_id:
                prior.append(p.stem)
    return sorted(prior)[-limit:]


def export_session(
    session_id: str,
    meetings_dir: Path | str,
    todo_path: Path | str,
    state_dir: Path | str,
    vault_dir: Path | str,
) -> Path:
    """Render one session into the vault. Returns the written file path.

    Raises:
        ExportError: unknown session, not yet reviewed-grade, or no vault dir.
    """
    validate_session_id(session_id)
    if not str(vault_dir).strip():
        raise ExportError(
            "No vault directory configured. Set [export].vault_dir in "
            "settings.toml or pass --vault explicitly."
        )
    meetings_dir = Path(meetings_dir)
    vault_dir = Path(vault_dir)

    try:
        session = state_mod.load_session_state(state_dir, session_id)
    except FileNotFoundError as exc:
        raise ExportError(f"Unknown session '{session_id}'.") from exc
    if session.state not in EXPORTABLE_STATES:
        raise ExportError(
            f"Session '{session_id}' is {session.state.value}; only sessions that "
            "reached human review (PROPOSED/REVIEWED/APPLIED) are exportable."
        )

    date_str, slug = _session_date_and_slug(session_id)
    meeting_type = session.metadata.get("meeting_type", "general")

    mom_path = meetings_dir / f"{session_id}.mom.md"
    summary_path = meetings_dir / f"{session_id}.summary.md"

    items = [
        item for item in parse_todo(todo_path).items
        if item.session_id == session_id and item.status != "deleted"
    ]

    lines = [
        "---",
        f"date: {date_str}",
        f"type: {meeting_type}",
        f"session_id: {session_id}",
        f"pipeline_state: {session.state.value}",
        "source: meeting-agent",
        "---",
        "",
        f"# {slug} — {date_str}",
    ]

    chained = _chained_session_ids(session_id, meetings_dir)
    if chained:
        links = " · ".join(f"[[{_vault_basename(cid)}]]" for cid in chained)
        lines += ["", f"Previous sessions: {links}"]

    if summary_path.exists():
        lines += ["", "## Summary", "", summary_path.read_text(encoding="utf-8").strip()]

    if items:
        lines += ["", "## Action items (as applied to todo.md)", ""]
        for item in items:
            mark = "x" if item.done else " "
            detail = []
            if item.owner:
                detail.append(f"owner: {item.owner}")
            if item.due_date:
                detail.append(f"due: {item.due_date}")
            suffix = f" ({', '.join(detail)})" if detail else ""
            lines.append(f"- [{mark}] {item.description}{suffix}")
            if item.evidence:
                lines.append(f'    - "{item.evidence}"')

    if mom_path.exists():
        lines += ["", "## Minutes of Meeting", "", mom_path.read_text(encoding="utf-8").strip()]

    out_dir = vault_dir / "Meetings"
    out_path = out_dir / f"{_vault_basename(session_id)}.md"
    atomic_write_text(out_path, "\n".join(lines) + "\n")
    return out_path


def _vault_basename(session_id: str) -> str:
    date_str, slug = _session_date_and_slug(session_id)
    return f"{date_str} {slug}"


def export_all(
    meetings_dir: Path | str,
    todo_path: Path | str,
    state_dir: Path | str,
    vault_dir: Path | str,
) -> list[Path]:
    """Export every exportable session. Skips (never fails on) sessions that
    are not reviewed-grade; returns the list of written paths."""
    written: list[Path] = []
    for session_id in state_mod.list_session_ids(state_dir):
        try:
            written.append(
                export_session(session_id, meetings_dir, todo_path, state_dir, vault_dir)
            )
        except ExportError:
            continue
    return written
