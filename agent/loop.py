"""
The ReAct orchestration loop (M5). Launches the M4 MCP server as a subprocess
for the lifetime of one run (via agent/mcp_client.py), drives one session_id
forward through the state machine by repeatedly asking the local LLM for a
decision (agent/protocol.py's envelope), executing at most one tool call per
turn, and logging every step via agent/trace.py.

Two safety nets are enforced by the loop itself, not left to the model's own
judgement -- a deliberate defence-in-depth choice consistent with this
project's "draft only, full human supervision" constraint:

  1. max_iterations is a hard ceiling. A model that fails to converge cannot
     run indefinitely or spin the GPU pointlessly -- the run ends with
     outcome="max_iterations_exceeded" rather than looping forever.
  2. FAILED is terminal in mcp_server/state.py's transition graph, and there
     are two distinct paths by which the loop can learn a session has reached
     it: (a) a tool call raises an MCPToolError -- this is how
     transcribe_meeting/extract_action_items/propose_todo_update actually
     surface their own failures, since each of them transitions to FAILED
     and then RE-RAISES the underlying exception rather than returning a
     dict (confirmed by reading mcp_server/tools/extraction.py directly,
     not assumed); or (b) a read-only query tool (get_session_status) simply
     returns {"state": "FAILED", ...}. Both paths halt the run immediately
     here, rather than feeding the error back to the model and trusting it
     to notice and stop on its own.

VRAM-aware sequencing: this loop is intentionally single-threaded and
processes one session_id per run, one tool call at a time -- there is no
internal concurrency to manage. The real GPU contention point on a 12-16GB
Blackwell card is between the already-running LLM server (continuous VRAM
allocation, started separately via `meeting-agent serve`) and faster-whisper's
transient allocation inside transcribe_meeting; this loop assumes the two have
been sized to coexist (config/settings.toml's compute_type guidance) and does
not attempt to pause/resume either process itself. Running multiple agent-loop
processes concurrently against the same GPU is out of scope and not guarded
against here -- flagged as a remaining unknown, not silently assumed safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from agent.mcp_client import AgentMCPClient, MCPToolError
from agent.protocol import ProtocolError, parse_decision
from agent.trace import TraceLogger, TraceStep
from llm.client import LLMClient

_PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"


class MaxIterationsExceededError(RuntimeError):
    """Raised when the loop reaches its iteration ceiling without a 'final' decision."""


@dataclass
class AgentRunResult:
    run_id: str
    session_id: str
    outcome: str  # "final" | "session_failed" | "max_iterations_exceeded"
    summary: str | None
    iterations: int


def _render_tool_catalogue(tools: list[dict]) -> str:
    return "\n".join(f"- `{tool['name']}`: {tool['description']}" for tool in tools)


def _render_system_prompt(tools: list[dict]) -> str:
    # A plain token replace() is deliberate, not str.format(): the prompt's
    # own example JSON envelopes are full of literal '{' / '}' characters that
    # .format() would misinterpret as placeholders (this broke on first test
    # run -- KeyError: '"thought"' -- a genuine bug, not the file-sync
    # anomaly, caught immediately by the test suite as intended).
    return _PROMPT_PATH.read_text(encoding="utf-8").replace("__TOOL_CATALOGUE__", _render_tool_catalogue(tools))


def _render_turn(session_id: str, transcript: list[str]) -> str:
    header = "session_id: " + session_id + "\n\nConversation so far (most recent last):\n"
    body = "\n".join(transcript) if transcript else "(this is your first turn)"
    return header + body


class AgentLoop:
    def __init__(
        self,
        llm_client: LLMClient,
        mcp_client: AgentMCPClient,
        trace_dir: Path | str,
        max_iterations: int = 12,
        filter_tools: frozenset[str] | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.mcp_client = mcp_client
        self.trace_dir = trace_dir
        self.max_iterations = max_iterations
        # Fix 1.3: tools to hide from the agent's tool catalogue. When the
        # pipeline orchestrator (cli/web.py run_pipeline) already performed
        # transcription before launching the agent, it passes
        # frozenset({"transcribe_meeting"}) so the agent never sees that tool
        # and cannot accidentally call it (defence-in-depth beyond the state
        # guard in mcp_server/tools/transcription.py and the system-prompt
        # dispatch table).
        self.filter_tools: frozenset[str] = filter_tools or frozenset()

    async def run(self, session_id: str) -> AgentRunResult:
        run_id = session_id + "-" + uuid.uuid4().hex[:8]
        trace = TraceLogger(self.trace_dir, run_id)
        goal = "Drive session '" + session_id + "' forward to PROPOSED."
        trace.start(goal, session_id)

        tools = await self.mcp_client.list_tools()
        if self.filter_tools:
            tools = [t for t in tools if t["name"] not in self.filter_tools]
        tool_names = {t["name"] for t in tools}
        system_prompt = _render_system_prompt(tools)
        transcript: list[str] = []

        for iteration in range(1, self.max_iterations + 1):
            user_prompt = _render_turn(session_id, transcript)
            raw = self.llm_client.complete(system_prompt, user_prompt)

            try:
                decision = parse_decision(raw)
            except ProtocolError as exc:
                transcript.append("[turn " + str(iteration) + "] your previous reply was rejected: " + str(exc))
                trace.step(TraceStep(iteration=iteration, thought="", action="", arguments={}, error=str(exc)))
                continue

            transcript.append("[turn " + str(iteration) + "] thought: " + decision.thought)

            if decision.is_final:
                trace.step(
                    TraceStep(iteration=iteration, thought=decision.thought, action="final", arguments={}, observation=decision.summary)
                )
                trace.finish("final", decision.summary)
                return AgentRunResult(run_id, session_id, "final", decision.summary, iteration)

            if decision.action not in tool_names:
                error = "UNKNOWN_TOOL: '" + decision.action + "' is not registered. Available tools: " + str(sorted(tool_names)) + "."
                transcript.append("[turn " + str(iteration) + "] observation: " + error)
                trace.step(
                    TraceStep(iteration=iteration, thought=decision.thought, action=decision.action, arguments=decision.arguments, error=error)
                )
                continue

            arguments = dict(decision.arguments)
            arguments.setdefault("session_id", session_id)

            try:
                observation = await self.mcp_client.call_tool(decision.action, arguments)
            except MCPToolError as exc:
                error_text = str(exc)
                trace.step(
                    TraceStep(iteration=iteration, thought=decision.thought, action=decision.action, arguments=arguments, error=error_text)
                )
                status = await self._safe_status(session_id)
                if status is not None and status.get("state") == "FAILED":
                    detail = status.get("metadata", {}).get("error", error_text)
                    trace.finish("session_failed", str(detail))
                    return AgentRunResult(run_id, session_id, "session_failed", str(detail), iteration)
                transcript.append("[turn " + str(iteration) + "] observation: TOOL_ERROR: " + error_text)
                continue

            trace.step(
                TraceStep(iteration=iteration, thought=decision.thought, action=decision.action, arguments=arguments, observation=observation)
            )
            transcript.append("[turn " + str(iteration) + "] observation: " + str(observation))

            if isinstance(observation, dict) and observation.get("state") == "FAILED":
                detail = observation.get("metadata", {}).get("error") or observation.get("error")
                trace.finish("session_failed", str(detail))
                return AgentRunResult(run_id, session_id, "session_failed", str(detail), iteration)

        trace.finish("max_iterations_exceeded", None)
        raise MaxIterationsExceededError(
            "Session '" + session_id + "' did not reach a terminal outcome within " + str(self.max_iterations) + " iterations."
        )

    async def _safe_status(self, session_id: str) -> dict | None:
        """Best-effort get_session_status -- swallow errors (e.g. session never created)."""
        try:
            return await self.mcp_client.call_tool("get_session_status", {"session_id": session_id})
        except MCPToolError:
            return None
