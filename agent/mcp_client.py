"""
Thin wrapper around the official `mcp` SDK's stdio client, launching
mcp_server/server.py (M4) as a subprocess for the lifetime of one agent run.

This inherits M4's no-socket data-egress guarantee unmodified: stdio is the
only transport this wrapper knows how to speak, so there is no configuration
path here that could accidentally open a network listener.

Two response-shape details below were established empirically against a real
subprocess (not assumed from the SDK docs, per this project's established
"probe before you build" practice -- see tests/fixtures/fake_llama_server.py's
docstring for the precedent):

  1. Tools whose Python return type is `list[...]` (list_sessions) get a
     FastMCP-synthesised `structuredContent` of the form {"result": [...]} --
     a bare JSON array cannot itself be a top-level object schema.
  2. Tools whose Python return type is `dict` (every other tool here) get
     `structuredContent=None`, because FastMCP cannot synthesise an output
     schema from an untyped `dict` annotation; the same payload is, however,
     present as JSON text in `content[0].text`. call_tool() below reads
     structuredContent when present and falls back to parsing content[0].text
     otherwise, so callers see one consistent dict/list return regardless of
     which path produced it.
"""

from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPToolError(RuntimeError):
    """Raised when a tool call returns isError=True."""


class AgentMCPClient:
    """One instance per agent run; launches and owns exactly one server subprocess."""

    def __init__(self, settings_path: Path | str, cwd: Path | str = ".") -> None:
        self._settings_path = str(settings_path)
        self._cwd = str(cwd)
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> AgentMCPClient:
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "cli.main", "mcp-serve", "--settings-path", self._settings_path],
            cwd=self._cwd,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        assert self._stack is not None
        await self._stack.aclose()
        self._stack = None
        self._session = None

    async def list_tools(self) -> list[dict]:
        assert self._session is not None
        result = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict) -> Any:
        assert self._session is not None
        result = await self._session.call_tool(name, arguments)
        if result.isError:
            text = result.content[0].text if result.content else "unknown error"
            raise MCPToolError(f"Tool '{name}' returned an error: {text}")
        if result.structuredContent is not None:
            payload = result.structuredContent
            if isinstance(payload, dict) and set(payload) == {"result"}:
                return payload["result"]
            return payload
        if result.content:
            return json.loads(result.content[0].text)
        return {}
