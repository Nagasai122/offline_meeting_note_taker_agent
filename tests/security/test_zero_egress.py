"""
Automated, repeatable verification of the zero-egress guarantee
(docs/architecture.md § Data-egress guarantee).

Three layers, each catching a different failure mode:

1. Static import gate: no production module may import a network-capable
   library outside an explicit, per-file allowlist. Catches "someone added
   `import requests` to a runtime module" at review time, in CI, forever.
2. trust_env gate: every httpx client/request constructed in production code
   must pass trust_env=False, so a configured HTTP(S)_PROXY can never route a
   loopback-intended request off-box (the S2 class of bug from
   docs/code_review_2026_07_01.md).
3. Runtime socket guard: monkey-patches socket connections to raise on any
   non-loopback address, then exercises real runtime code paths (the full
   review -> apply cycle, todo parsing/writing, transcript import parsing).
   Catches transitive egress the static gates cannot see.

scripts/network_audit.py (live psutil egress monitor) remains the
whole-process verification tool for a real record->apply cycle on the
workstation; this module is the CI-runnable, hardware-free complement.
"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

PRODUCTION_PACKAGES = [
    "agent",
    "audio_capture",
    "cli",
    "concurrency",
    "config",
    "llm",
    "mcp_server",
    "transcribe",
    "scripts",
]

# Libraries that can (or are commonly used to) open network connections.
NETWORK_CAPABLE_TOP_LEVEL = {
    "httpx",
    "requests",
    "aiohttp",
    "urllib",
    "urllib3",
    "websockets",
    "smtplib",
    "ftplib",
    "poplib",
    "imaplib",
    "telnetlib",
    "xmlrpc",
    "huggingface_hub",
    "socket",
    "ssl",
}

# The complete, deliberate allowlist. Every entry must have a documented
# loopback-only or setup-only justification. Anything not listed here fails
# the gate — additions require updating this table *with* a justification.
IMPORT_ALLOWLIST: dict[tuple[str, str], str] = {
    ("llm/client.py", "httpx"): "loopback llama-server only; trust_env=False on every request",
    ("llm/http_probe.py", "httpx"): "localhost health probe; AsyncClient(trust_env=False) hard-wired",
    ("llm/server_manager.py", "httpx"): "localhost health check; trust_env=False",
    ("cli/main.py", "huggingface_hub"): "lazy import inside setup() — the one permitted network command",
    ("cli/web.py", "socket"): "socket.create_connection to 127.0.0.1 only (LLM port probes)",
}


def _production_files() -> list[Path]:
    files: list[Path] = []
    for pkg in PRODUCTION_PACKAGES:
        pkg_dir = REPO_ROOT / pkg
        if pkg_dir.is_dir():
            files.extend(sorted(pkg_dir.rglob("*.py")))
    return [f for f in files if "__pycache__" not in f.parts]


def _imports_in(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_production_files_exist_sanity():
    files = _production_files()
    assert len(files) > 30, "production tree unexpectedly small — packaging changed?"


def test_no_network_capable_imports_outside_allowlist():
    violations: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path in _production_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for mod in _imports_in(tree) & NETWORK_CAPABLE_TOP_LEVEL:
            seen.add((rel, mod))
            if (rel, mod) not in IMPORT_ALLOWLIST:
                violations.append(f"{rel}: imports network-capable module '{mod}' (not in allowlist)")
    assert not violations, (
        "Zero-egress import gate failed. Either remove the import or add an "
        "allowlist entry WITH a loopback/setup-only justification:\n  "
        + "\n  ".join(violations)
    )
    # Stale allowlist entries are also failures: they hide future regressions.
    stale = set(IMPORT_ALLOWLIST) - seen
    assert not stale, f"Allowlist entries no longer present in code (remove them): {stale}"


def test_huggingface_hub_import_is_confined_to_setup_function():
    """`setup` is the only network-permitted command; the download library it
    uses must be imported inside that function body, unreachable from any
    other command's import graph."""
    tree = ast.parse((REPO_ROOT / "cli" / "main.py").read_text(encoding="utf-8"))
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    names = (
                        [a.name for a in child.names]
                        if isinstance(child, ast.Import)
                        else [child.module or ""]
                    )
                    if any(n.startswith("huggingface_hub") for n in names) and node.name != "setup":
                        offending.append(node.name)
    # Also: never at module top level.
    for node in tree.body:
        if isinstance(node, ast.Import) and any(a.name.startswith("huggingface_hub") for a in node.names):
            offending.append("<module level>")
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("huggingface_hub"):
            offending.append("<module level>")
    assert not offending, f"huggingface_hub imported outside setup(): {offending}"


def test_every_httpx_client_and_request_sets_trust_env_false():
    """The S2 bug class: an httpx call without trust_env=False will honour
    HTTP(S)_PROXY/ALL_PROXY and route a 'loopback' request through a proxy.
    Every httpx.AsyncClient/Client construction and every module-level
    httpx.get/post/... call in production must pass trust_env=False."""
    flagged: list[str] = []
    for path in _production_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        if "httpx" not in source:
            continue
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match httpx.<Name>(...) attribute calls only — precise, no
            # false positives on dict.get()/route decorators.
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "httpx"):
                continue
            if func.attr not in {"AsyncClient", "Client", "get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}:
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            te = kwargs.get("trust_env")
            ok = isinstance(te, ast.Constant) and te.value is False
            if not ok:
                flagged.append(f"{rel}:{node.lineno}: httpx.{func.attr}(...) without trust_env=False")
    assert not flagged, "httpx usage without trust_env=False:\n  " + "\n  ".join(flagged)


def test_socket_usage_in_web_dashboard_is_loopback_or_configured_llm_host():
    """cli/web.py's raw socket probes must target either a loopback literal or
    exactly `settings.llm.host` — the configured LLM endpoint, which is
    127.0.0.1 on bare metal and the compose service name on the
    internal-only (no-egress) container network. Anything else fails.
    The *listen* side is unaffected: llm/server_manager.py's
    UnsafeBindAddressError still rejects non-loopback binds."""

    def _is_settings_llm_host(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "host"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "llm"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "settings"
        )

    source = (REPO_ROOT / "cli" / "web.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    flagged: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "create_connection":
            if not node.args:
                flagged.append(f"cli/web.py:{node.lineno}: create_connection with no positional address")
                continue
            addr = node.args[0]
            host_ok = (
                isinstance(addr, ast.Tuple)
                and addr.elts
                and (
                    (isinstance(addr.elts[0], ast.Constant)
                     and addr.elts[0].value in ("127.0.0.1", "::1", "localhost"))
                    or _is_settings_llm_host(addr.elts[0])
                )
            )
            if not host_ok:
                flagged.append(f"cli/web.py:{node.lineno}: create_connection to unexpected host expression")
    assert not flagged, "\n".join(flagged)


# ---------------------------------------------------------------------------
# Runtime socket guard
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class _EgressAttempt(AssertionError):
    pass


@pytest.fixture
def no_egress(monkeypatch):
    """Fail the test if anything attempts a non-loopback socket connection."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def _check(address):
        if isinstance(address, tuple) and address and isinstance(address[0], str):
            host = address[0]
            if host not in _LOOPBACK_HOSTS and not host.startswith("127."):
                raise _EgressAttempt(f"Non-loopback connection attempted: {address!r}")

    def guarded_connect(self, address):
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        _check(address)
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    yield


def _write_draft(pending_review_dir: Path, session_id: str, items: list[dict]) -> None:
    pending_review_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# Proposed todo updates -- session {session_id}", ""]
    for item in items:
        meta = {
            "id": item["id"],
            "owner": item.get("owner"),
            "due_date": item.get("due_date"),
            "session_id": session_id,
        }
        lines.append(f"- [ ] {item['description']} <!-- meta: {json.dumps(meta)} -->")
    (pending_review_dir / f"{session_id}.md").write_text("\n".join(lines) + "\n")


def test_full_review_apply_cycle_makes_zero_network_calls(no_egress, tmp_path):
    """The strongest hardware-free runtime check available: a complete
    PROPOSED -> REVIEWED -> APPLIED cycle (including the git snapshot commits)
    under a socket guard that raises on any non-loopback connection."""
    from cli.capability import mint_capability_token
    from cli.review_apply import ReviewDecision, apply_reviewed_update, complete_review
    from mcp_server.state import State, create_session

    state_dir = tmp_path / "state"
    pending = tmp_path / "pending_review"
    todo_path = tmp_path / "todo.md"
    lock = tmp_path / ".lock"
    create_session(state_dir, "egress-test-1", lock, 1.0, initial_state=State.PROPOSED)
    _write_draft(pending, "egress-test-1", [{"id": "e1", "description": "verify egress"}])

    decisions = [
        ReviewDecision(
            id="e1", decision="accept", description="verify egress",
            owner=None, due_date=None, session_id="egress-test-1",
        )
    ]
    complete_review("egress-test-1", decisions, pending, state_dir, lock, 1.0)
    result = apply_reviewed_update(
        mint_capability_token(), "egress-test-1", pending, todo_path,
        tmp_path, state_dir, lock, 1.0,
    )
    assert result["applied_count"] == 1
    assert "verify egress" in todo_path.read_text()


def test_transcript_import_parsers_make_zero_network_calls(no_egress, tmp_path):
    from transcribe.import_parsers import parse_transcript_file, segments_to_text

    vtt = tmp_path / "t.vtt"
    vtt.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello from the egress test.\n",
        encoding="utf-8",
    )
    segments = parse_transcript_file(vtt)
    assert segments
    assert "egress test" in segments_to_text(segments)


def test_briefing_build_makes_zero_network_calls(no_egress, tmp_path):
    from cli.briefing import build_daily_briefing

    (tmp_path / "todo.md").write_text("# Todo\n\n- [ ] item one\n", encoding="utf-8")
    (tmp_path / "state").mkdir()
    briefing = build_daily_briefing(tmp_path / "todo.md", tmp_path / "state")
    assert briefing is not None
