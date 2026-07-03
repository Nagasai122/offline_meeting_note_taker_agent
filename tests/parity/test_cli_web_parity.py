"""CLI/web-endpoint parity tests (audit Strand C).

The invariant under test: the CLI (`meeting-agent review`/`apply`, which call
cli.review_apply functions in-process) and the web dashboard
(`POST /api/review/decide`, `POST /api/review/apply`) must produce
behaviourally identical results for the same inputs — same todo.md content,
same final state, same refusal behaviour on double-apply and on
malformed todo.md. Both paths funnel into the same
complete_review/apply_reviewed_update functions by design
(docs/architecture.md amendment 2); these tests pin that equivalence so a
future divergent reimplementation on either side fails loudly.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cli.capability import mint_capability_token
from cli.review_apply import ReviewDecision, apply_reviewed_update, complete_review
from mcp_server.state import InvalidTransitionError, State, create_session, load_session_state

ITEMS = [
    {"id": "par-001", "description": "Parity task one", "owner": "Naga", "due_date": "2026-07-10"},
    {"id": "par-002", "description": "Parity task two", "owner": None, "due_date": None},
]


def _mkdirs(root: Path) -> dict:
    d = {
        "data_dir": root / "data",
        "state_dir": root / "data" / "state",
        "pending_review_dir": root / "data" / "pending_review",
        "todo_path": root / "data" / "todo.md",
        "lock_path": root / "data" / "state" / ".lock",
    }
    d["state_dir"].mkdir(parents=True, exist_ok=True)
    d["pending_review_dir"].mkdir(parents=True, exist_ok=True)
    d["todo_path"].write_text("")
    return d


def _seed_proposed(d: dict, session_id: str) -> None:
    create_session(d["state_dir"], session_id, d["lock_path"], 1.0, initial_state=State.PROPOSED)
    lines = [f"# Proposed todo updates -- session {session_id}", ""]
    for item in ITEMS:
        meta = {"id": item["id"], "owner": item["owner"], "due_date": item["due_date"], "session_id": session_id}
        lines.append(f"- [ ] {item['description']} <!-- meta: {json.dumps(meta)} -->")
    (d["pending_review_dir"] / f"{session_id}.md").write_text("\n".join(lines) + "\n")


def _decisions(session_id: str) -> list[dict]:
    return [
        {
            "id": it["id"], "decision": "accept", "description": it["description"],
            "owner": it["owner"], "due_date": it["due_date"], "session_id": session_id,
        }
        for it in ITEMS
    ]


def _run_cli_path(d: dict, session_id: str) -> dict:
    """Exactly what cli/main.py's review+apply commands do, minus the prompts."""
    decisions = [ReviewDecision(**dec) for dec in _decisions(session_id)]
    complete_review(session_id, decisions, d["pending_review_dir"], d["state_dir"], d["lock_path"], 1.0)
    return apply_reviewed_update(
        mint_capability_token(), session_id, d["pending_review_dir"], d["todo_path"],
        d["data_dir"], d["state_dir"], d["lock_path"], 1.0,
    )


@pytest.fixture()
def web_client(tmp_path):
    import cli.web as web_module

    d = _mkdirs(tmp_path / "web")

    class _FakeSettings:
        class paths:
            data_dir = str(d["data_dir"])
            tmp_dir = str(d["data_dir"] / "tmp")
        class llm:
            host = "127.0.0.1"
            port = 8080
            health_check_path = "/health"
            startup_timeout_seconds = 5
        class concurrency:
            lock_path = str(d["lock_path"])
            lock_timeout_seconds = 1.0
        class privacy:
            tmp_audio_ttl_seconds = 3600
        class whisper:
            device = "cpu"
            compute_type = "int8"

    with patch.object(web_module, "settings", _FakeSettings()):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c, d


def _run_web_path(client: TestClient, session_id: str) -> dict:
    decide = client.post("/api/review/decide", json={"session_id": session_id, "decisions": _decisions(session_id)})
    assert decide.status_code == 200, decide.text
    apply_resp = client.post("/api/review/apply", json={"session_id": session_id})
    assert apply_resp.status_code == 200, apply_resp.text
    return apply_resp.json()


def _normalise_todo(text: str) -> list[dict]:
    """Compare todo.md content semantically: one meta dict + description per line."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("- ["):
            continue
        desc = line.split("<!--")[0].removeprefix("- [ ]").removeprefix("- [x]").strip()
        meta = json.loads(line.split("meta:", 1)[1].rsplit("-->", 1)[0])
        rows.append({"description": desc, **meta})
    return rows


def test_apply_via_cli_and_web_produce_identical_todo_content(tmp_path, web_client):
    client, web_dirs = web_client
    cli_dirs = _mkdirs(tmp_path / "cli")

    _seed_proposed(cli_dirs, "parity-1")
    _seed_proposed(web_dirs, "parity-1")

    cli_result = _run_cli_path(cli_dirs, "parity-1")
    web_result = _run_web_path(client, "parity-1")

    assert cli_result["applied_count"] == web_result["applied_count"] == 2
    assert cli_result["conflicts"] == web_result["conflicts"] == []
    assert load_session_state(cli_dirs["state_dir"], "parity-1").state == State.APPLIED
    assert load_session_state(web_dirs["state_dir"], "parity-1").state == State.APPLIED

    cli_rows = _normalise_todo(cli_dirs["todo_path"].read_text())
    web_rows = _normalise_todo(web_dirs["todo_path"].read_text())
    assert cli_rows == web_rows
    assert [r["description"] for r in cli_rows] == ["Parity task one", "Parity task two"]


def test_double_apply_refused_identically_on_both_paths(tmp_path, web_client):
    client, web_dirs = web_client
    cli_dirs = _mkdirs(tmp_path / "cli")

    _seed_proposed(cli_dirs, "parity-2")
    _seed_proposed(web_dirs, "parity-2")
    _run_cli_path(cli_dirs, "parity-2")
    _run_web_path(client, "parity-2")

    todo_before_cli = cli_dirs["todo_path"].read_text()
    todo_before_web = web_dirs["todo_path"].read_text()

    # CLI path: second apply raises InvalidTransitionError, todo.md untouched.
    with pytest.raises(InvalidTransitionError):
        apply_reviewed_update(
            mint_capability_token(), "parity-2", cli_dirs["pending_review_dir"],
            cli_dirs["todo_path"], cli_dirs["data_dir"], cli_dirs["state_dir"],
            cli_dirs["lock_path"], 1.0,
        )
    # Web path: same refusal surfaced as 409, todo.md untouched.
    resp = client.post("/api/review/apply", json={"session_id": "parity-2"})
    assert resp.status_code == 409

    assert cli_dirs["todo_path"].read_text() == todo_before_cli
    assert web_dirs["todo_path"].read_text() == todo_before_web


def test_malformed_todo_md_fails_session_identically_on_both_paths(tmp_path, web_client):
    """Amendment 8: a hand-corrupted todo.md must surface TODO_FILE_UNPARSEABLE
    on both interfaces and transition the session to FAILED, never silently
    corrupt the file further."""
    from mcp_server.todo import TodoFileUnparsableError

    client, web_dirs = web_client
    cli_dirs = _mkdirs(tmp_path / "cli")
    corrupt = '- [?] broken marker <!-- meta: {"id": "x"} -->\n'

    for d, sid in ((cli_dirs, "parity-3"), (web_dirs, "parity-3")):
        _seed_proposed(d, sid)
    # Advance both to REVIEWED first, then corrupt todo.md.
    decisions = [ReviewDecision(**dec) for dec in _decisions("parity-3")]
    complete_review("parity-3", decisions, cli_dirs["pending_review_dir"], cli_dirs["state_dir"], cli_dirs["lock_path"], 1.0)
    decide = client.post("/api/review/decide", json={"session_id": "parity-3", "decisions": _decisions("parity-3")})
    assert decide.status_code == 200

    cli_dirs["todo_path"].write_text(corrupt)
    web_dirs["todo_path"].write_text(corrupt)

    with pytest.raises(TodoFileUnparsableError):
        apply_reviewed_update(
            mint_capability_token(), "parity-3", cli_dirs["pending_review_dir"],
            cli_dirs["todo_path"], cli_dirs["data_dir"], cli_dirs["state_dir"],
            cli_dirs["lock_path"], 1.0,
        )
    resp = client.post("/api/review/apply", json={"session_id": "parity-3"})
    assert resp.status_code == 422  # TodoFileUnparsableError -> 422, pinned
    # Both sessions must be FAILED with the typed error recorded.
    for d in (cli_dirs, web_dirs):
        session = load_session_state(d["state_dir"], "parity-3")
        assert session.state == State.FAILED
        assert "TODO_FILE_UNPARSEABLE" in str(session.metadata.get("error", ""))
        # And the corrupted file was not rewritten by either path.
        assert d["todo_path"].read_text() == corrupt
