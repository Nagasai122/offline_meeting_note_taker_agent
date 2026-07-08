"""
Capability token for `apply_reviewed_update` (M6), implementing critique
amendment 2(a).

Why this exists at all, given amendment 2(b) already makes the function
structurally absent from mcp_server/server.py (never imported, never
registered as an MCP tool): the token is a second, independent layer of
defence-in-depth, not a duplicate of the same protection. If a future
refactor ever DID accidentally import and register `apply_reviewed_update`
as an MCP tool, FastMCP derives each tool's JSON schema from the function's
type hints so it can validate and route a JSON-RPC tool-call payload into a
Python call. `CapabilityToken` is a frozen dataclass wrapping an opaque
nonce -- it is not JSON-serialisable into a primitive an LLM could populate
from a tool-call argument, so any such accidental registration would fail
closed (FastMCP cannot construct the parameter from JSON) rather than open
(silently accepting a forged or guessed token). The two mechanisms protect
against two different failure modes: (b) against "the agent loop's model
decides to call it", (a) against "a future code change wires it up without
anyone noticing".

`mint_capability_token()` is deliberately called from exactly ten places in
the trusted CLI surface:
  1. cli/main.py `apply` command — the original call site.
  2. cli/web.py `POST /api/review/apply` endpoint — added when the web
     dashboard gained its own review/apply UI (both callers are in cli/,
     never in agent/ or mcp_server/).
  3. cli/web.py `run_pipeline`'s auto-accept path — the in-process apply for
     pipelines started with auto_accept=True, calling the same
     `apply_reviewed_update()` as the endpoint above.
  4. cli/web.py `POST /api/tasks/manual` endpoint — mints a token to call
     `write_manual_task()` (architecture_v2.md §Phase 7.2).
  5. cli/web.py `PATCH /api/tasks/{task_id}` endpoint — mints a token to call
     `update_task_status()`.
  6. cli/web.py `DELETE /api/tasks/{task_id}` endpoint — mints a token to call
     `update_task_status()` with a status="deleted" soft delete.
  7. cli/web.py `POST /api/todo/complete` endpoint — mints a token to call
     `update_task_status()` with a status="done" update. Previously wrote
     todo.md directly (no token, no FileLock, no atomic write) as a second,
     ungated path to the same file; reconciled to share call site 5/6's
     mechanism instead of maintaining a separate one (engineering-hardening
     pass, alongside the P0/roadmap bug fixes).
  8. cli/web.py `POST /api/tasks/{task_id}/duplicate` endpoint — mints a
     token to call `duplicate_task()` (P1.5, full task edit + additional ops).
  9. cli/web.py `POST /api/tasks/{task_id}/comments` endpoint — mints a token
     to call `add_task_comment()`, an append-only read-modify-write, not the
     generic update_task_status() allow-list.
  10. cli/web.py `POST /api/tasks/{task_id}/attachments` endpoint — mints a
     token to call `add_task_attachment()`, same append-only pattern as
     comments above; the endpoint itself saves the file to
     data/task_attachments/<task_id>/ before recording the reference.
Grep for `mint_capability_token()` call sites is itself a verification step
(docs/architecture.md should list this as part of the M6 verification
checklist); this docstring must be kept current whenever a new call site is
added.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityToken:
    """Opaque proof that a call originates from the CLI's own `apply` command."""

    _nonce: str


def mint_capability_token() -> CapabilityToken:
    return CapabilityToken(_nonce=secrets.token_hex(16))


def require_capability_token(token: object) -> None:
    """Raise TypeError unless `token` is a genuine CapabilityToken instance.

    A plain `isinstance` check is the entire enforcement mechanism here --
    deliberately simple, because the security property being relied upon is
    "this argument's type cannot be constructed from untrusted JSON", not any
    cryptographic property of the nonce itself.
    """
    if not isinstance(token, CapabilityToken):
        raise TypeError(
            "apply_reviewed_update requires a genuine CapabilityToken minted by "
            "cli.capability.mint_capability_token() -- got "
            f"{type(token).__name__}. This call is being rejected by design."
        )
