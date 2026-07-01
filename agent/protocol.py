"""
The ReAct turn protocol the agent loop imposes on the local LLM.

Why a hand-rolled JSON envelope rather than the backend's native OpenAI-style
`tools=[...]` function-calling parameter: native tool-calling quality varies
materially by backend (llama-server, vLLM) and by the served model's own
fine-tuning for that wire format, and llm/client.py's existing
`complete(system_prompt, user_prompt) -> str` contract (M3/M4) already covers
plain chat completions without needing to widen the LLMClient interface. A
single JSON object per turn -- {"thought", "action", "arguments"} or
{"thought", "action": "final", "summary"} -- is portable across both backends,
is easy to validate strictly (see parse_decision below), and is exactly as
testable with a FakeLLMClient as mcp_server/tools/extraction.py's existing
JSON-array contract already is. The tradeoff, made explicitly here rather than
left implicit: a model not instruction-tuned for this exact envelope may need
a stricter prompt or a few-shot example to comply reliably -- this is a real
risk to validate against the actual served model, not just this fake-LLM
suite.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class ProtocolError(RuntimeError):
    """Raised when a model turn cannot be parsed as a valid decision envelope."""


@dataclass
class AgentDecision:
    thought: str
    action: str
    arguments: dict = field(default_factory=dict)
    summary: str | None = None

    @property
    def is_final(self) -> bool:
        return self.action == "final"


def _strip_fences(raw: str) -> str:
    return _FENCE_RE.sub("", raw.strip()).strip()


def parse_decision(raw: str) -> AgentDecision:
    """
    Parse one model turn. Raises ProtocolError on anything that is not exactly
    a single JSON object with at least 'thought' and 'action' string keys --
    never silently coerced or partially accepted, mirroring the same
    fail-loudly stance already taken in mcp_server/tools/extraction.py and
    mcp_server/todo.py for malformed model/file input.
    """
    cleaned = _strip_fences(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Model turn was not valid JSON: {exc}. Raw turn: {raw!r}") from exc

    if not isinstance(payload, dict):
        raise ProtocolError(f"Model turn must be a single JSON object, got {type(payload).__name__}: {raw!r}")

    thought = payload.get("thought")
    action = payload.get("action")
    if not isinstance(thought, str) or not thought:
        raise ProtocolError(f"Model turn missing a non-empty 'thought' string: {payload!r}")
    if not isinstance(action, str) or not action:
        raise ProtocolError(f"Model turn missing a non-empty 'action' string: {payload!r}")

    arguments = payload.get("arguments", {})
    if action != "final" and not isinstance(arguments, dict):
        raise ProtocolError(f"'arguments' must be a JSON object when action != 'final': {payload!r}")

    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ProtocolError(f"'summary' must be a string when present: {payload!r}")

    return AgentDecision(
        thought=thought,
        action=action,
        arguments=arguments if isinstance(arguments, dict) else {},
        summary=summary,
    )
