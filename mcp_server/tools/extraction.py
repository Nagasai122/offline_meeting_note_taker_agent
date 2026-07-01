"""
extract_action_items: the one LLM-backed step in the pipeline. Reads the
structured transcript JSON written by M2, asks the local model for a JSON
array of action items, and persists the parsed result alongside the
transcript before transitioning TRANSCRIBED -> EXTRACTED (or -> FAILED).

`llm_client` is injected (an `LLMClient`, see llm/client.py) rather than
constructed internally, specifically so the M4 fake-LLM smoke test (critique
amendment 6) can exercise this whole path -- including the JSON-parsing
contract between this tool and the model -- without a GPU or a running
llama-server/vLLM process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from llm.client import LLMClient
from mcp_server import state as state_mod
from mcp_server.schemas import validate_session_id

ACTION_ITEM_SYSTEM_PROMPT = """You are extracting notes and action items from a meeting transcript.
Respond with ONLY a JSON object, no commentary and no markdown code fence.
The object must have two keys: "summary" and "action_items".
"summary" should be a 3-5 bullet point markdown string summarizing the discussion.
"action_items" must be a JSON array of objects: {"description": string, "owner": string|null, "due_date": string|null}.
due_date, if present, must be an ISO-8601 date (YYYY-MM-DD).
If there are no action items, "action_items" should be [].

INTELLIGENT CONTEXT INFERENCE:
You will be provided with the user's CURRENT TASK CONTEXT (their existing todo list). Use this context to intelligently infer the ownership of new action items, identify key projects, and understand team member roles based on precedent, rather than relying on rigid rules."""


class ExtractionError(RuntimeError):
    """Raised when the model's response cannot be interpreted as action items."""


def extract_action_items(
    session_id: str,
    meetings_dir: Path | str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
    llm_client: LLMClient,
) -> dict:
    validate_session_id(session_id)
    meetings_dir = Path(meetings_dir)
    transcript_path = meetings_dir / f"{session_id}.json"
    if not transcript_path.exists():
        raise FileNotFoundError(f"No transcript found for session '{session_id}' at {transcript_path}.")

    context_path = meetings_dir / f"{session_id}.context.txt"
    highlight_path = meetings_dir / f"{session_id}.highlights.json"
    
    additional_context = ""
    if context_path.exists():
        additional_context += f"\n\nMEETING CONTEXT:\n{context_path.read_text()}\n"
        
    todo_path = meetings_dir.parent / "todo.md"
    if todo_path.exists():
        additional_context += f"\n\nCURRENT TASK CONTEXT (todo.md):\n{todo_path.read_text()}\n"
        
    # Implement Session Chaining: look for previous recordings of the same meeting across all days

    match = re.match(r'^(.*)-(\d{8})-(\d{6})$', session_id)
    if match:
        slug = match.group(1)
        date_str = match.group(2)
        time_str = match.group(3)
        
        # Find all .md notes across all days for this slug
        prev_notes = []
        for p in meetings_dir.glob(f"{slug}-*.md"):
            m2 = re.match(r'^(.*)-(\d{8})-(\d{6})\.md$', p.name)
            if m2:
                prev_date = m2.group(2)
                prev_time = m2.group(3)
                # Ensure it is strictly before the current meeting
                if prev_date < date_str or (prev_date == date_str and prev_time < time_str):
                    prev_notes.append(p)
                
        if prev_notes:
            prev_notes.sort() # chronological
            # Take only the last 3 meetings to prevent token overflow
            prev_notes = prev_notes[-3:]
            additional_context += "\n\nPREVIOUS SESSIONS OF THIS MEETING:\n"
            for p in prev_notes:
                additional_context += f"--- Session {p.stem} ---\n{p.read_text()}\n"
        
    if highlight_path.exists():
        highlights = json.loads(highlight_path.read_text())
        timestamps = [h["timestamp"] for h in highlights]
        additional_context += f"\n\nIMPORTANT HIGHLIGHTS: The user explicitly highlighted {len(timestamps)} moments during this recording. Pay special attention to the topics discussed."
        
    prompt = ACTION_ITEM_SYSTEM_PROMPT + additional_context

    try:
        transcript_data = json.loads(transcript_path.read_text())
        transcript_text = _render_transcript(transcript_data)
        raw_response = llm_client.complete(prompt, transcript_text)
        result = _parse_extraction_result(raw_response)
    except Exception as exc:
        state_mod.transition(
            state_dir, session_id, state_mod.State.FAILED, lock_path, lock_timeout,
            error=str(exc),
        )
        raise

    actions_path = meetings_dir / f"{session_id}.actions.json"
    actions_path.write_text(json.dumps(result["action_items"], indent=2))
    
    summary_path = meetings_dir / f"{session_id}.summary.md"
    summary_path.write_text(result["summary"])

    session = state_mod.transition(
        state_dir, session_id, state_mod.State.EXTRACTED, lock_path, lock_timeout,
        actions_path=str(actions_path), action_item_count=len(result["action_items"]),
    )
    return {"session_id": session_id, "state": session.state.value, "action_items": result["action_items"], "summary": result["summary"]}


def _render_transcript(transcript_data: dict) -> str:
    segments = transcript_data.get("segments", [])
    return "\n".join(f"{seg.get('speaker') or 'Speaker'}: {seg['text'].strip()}" for seg in segments)


def _parse_extraction_result(raw_response: str) -> dict:
    # Models routinely wrap JSON in a markdown fence despite being told not to;
    # strip that cosmetic deviation before treating the body as malformed.
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw_response.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            f"LLM did not return valid JSON: {exc}. Raw response: {raw_response!r}"
        ) from exc
    if not isinstance(parsed, dict) or "action_items" not in parsed or "summary" not in parsed:
        raise ExtractionError(f"Expected a JSON object with 'summary' and 'action_items', got {type(parsed).__name__}.")
    
    action_items = parsed["action_items"]
    if not isinstance(action_items, list):
        raise ExtractionError(f"Expected a JSON array for 'action_items', got {type(action_items).__name__}.")
        
    for item in action_items:
        if not isinstance(item, dict) or "description" not in item:
            raise ExtractionError(f"Malformed action item (missing 'description'): {item!r}")
            
    return parsed
