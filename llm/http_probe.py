"""Tiny local-only HTTP probe helper.

This module exists so that cli/web.py does not need to touch httpx directly:
every HTTP client primitive the dashboard needs (a single localhost health
probe) is concentrated here, in one small module the INV-1 network audit can
reason about at a glance. The AsyncClient is always constructed with
trust_env=False, so no proxy or environment configuration can ever route
these 127.0.0.1 probes off-box (INV-1: zero network egress at runtime).
"""

from __future__ import annotations

import httpx


def make_local_client() -> httpx.AsyncClient:
    """Return an AsyncClient that ignores all proxy/env configuration."""
    return httpx.AsyncClient(trust_env=False)


async def probe_ok(client: httpx.AsyncClient, url: str, timeout: float = 2.0) -> bool:
    """GET `url` and return True iff the response is HTTP 200.

    Any transport-level failure (connection refused while the server is still
    loading the model, timeouts, resets) returns False rather than raising, so
    callers can poll in a loop without ceremony.
    """
    request = client.build_request("GET", url, timeout=timeout)
    try:
        response = await client.send(request)
    except httpx.RequestError:
        return False
    return response.status_code == 200
