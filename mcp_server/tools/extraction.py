"""
extract_action_items: the one LLM-backed step in the pipeline. Reads the
structured transcript JSON written by M2, asks the local model for a JSON
object (summary + action items, plus type-specific supplementary fields), and
persists the parsed result alongside the transcript before transitioning
TRANSCRIBED -> EXTRACTED (or -> FAILED).

`llm_client` is injected (an `LLMClient`, see llm/client.py) rather than
constructed internally, specifically so the M4 fake-LLM smoke test (critique
amendment 6) can exercise this whole path -- including the JSON-parsing
contract between this tool and the model -- without a GPU or a running
llama-server/vLLM process.

Meeting-type-aware extraction (architecture_v2.md §4-6): the LLM response
contract stays additive rather than diverging per type -- every type's system
prompt still requires the same two top-level keys ("summary", "action_items")
that `_parse_extraction_result` has always validated, plus type-specific
supplementary keys (e.g. "progress_reported" for IS calls, "decisions" for
project meetings, "key_concepts" for seminars) that `mom_writer.py` reads with
safe defaults when absent. This keeps the existing test suite and downstream
pipeline contract (propose_todo_update, todo.md) intact while still producing
type-aware MoMs. Action items keep the field name "owner" (not "assignee")
across all three prompts, since that is what TodoItem/propose_todo_update
already expect.

Long transcripts (> ~5000 estimated tokens, roughly 35 minutes of speech) are
routed through transcribe/chunker.py's sequential chunked-extraction path
instead of one single-shot call, so a 3-hour meeting doesn't silently exceed
the model's context window (see docs/code_review_2026_07_01.md's truncation
finding, which this directly fixes).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from llm.client import LLMClient
from concurrency.atomic import atomic_write_text
from mcp_server import state as state_mod
from mcp_server.meeting_type import MeetingType, load_meeting_type
from mcp_server.mom_writer import write_mom
from mcp_server.schemas import validate_session_id

logger = logging.getLogger(__name__)

# Token threshold above which extract_action_items switches to chunked mode.
CHUNK_THRESHOLD_TOKENS = 5000

# Fix 2.3: transcripts shorter than this word count are skipped for LLM extraction.
MIN_TRANSCRIPT_WORDS = 50

_BASE_CONTRACT = """Respond with ONLY a JSON object, no commentary and no markdown code fence.
The object must always include these two keys:
"summary": a 3-5 bullet point markdown string summarizing the discussion.
"action_items": a JSON array of objects, each with:
  "description": string (required)
  "owner": string|null
  "due_date": string|null -- if present, must be an ISO-8601 date (YYYY-MM-DD).
      The recording date is {recording_date_iso} (ISO 8601, UTC). If the speaker
      says a relative expression such as "next Tuesday", "end of this week",
      "by Friday", "in two weeks", compute the actual calendar date using
      {recording_date_iso} as today's reference and output that computed date.
      If no due date is mentioned or inferrable, output null.
  "priority": one of "HIGH"|"MEDIUM"|"LOW". Infer priority from language cues:
      HIGH: "urgent", "ASAP", "blocking", "critical", "must", "today", "immediately".
      LOW: "when you get a chance", "eventually", "nice to have", "if time permits".
      MEDIUM: everything else (the default).
  "depends_on": OPTIONAL string -- if this item cannot start until another item
      in this same action_items list is complete, copy that other item's
      description text here exactly (copy it verbatim from the description you
      wrote for that item). Omit this key entirely if there is no dependency.
  "evidence": OPTIONAL string -- a short verbatim quote (max ~25 words) from the
      transcript, the exact moment this task was assigned or agreed. This is the
      provenance a reviewer sees as "why does this task exist". Copy the words
      actually spoken; do not paraphrase. Omit the key if no single quotable
      moment exists.
If there are no action items, "action_items" should be [].

INTELLIGENT CONTEXT INFERENCE:
You will be provided with the user's CURRENT TASK CONTEXT (their existing todo list). Use this context to intelligently infer the ownership of new action items, identify key projects, and understand team member roles based on precedent, rather than relying on rigid rules.

GROUNDING RULES (Fix 2.1 — mandatory, verify before finalizing your response):
- Extract ONLY items explicitly stated or clearly implied in the CURRENT MEETING TRANSCRIPT.
- Do NOT reproduce items from prior session context as new action items unless explicitly re-discussed.
- Do NOT invent names, owners, due dates, or assignments not mentioned in the transcript.
- When uncertain whether something was said, omit it rather than guessing.
- A short or unclear transcript warrants a minimal, conservative extraction — do not pad with inferences.
- Verify every action item description maps to something said in the transcript before outputting.
"""

IS_CALL_SYSTEM_PROMPT = _BASE_CONTRACT + """
This is a daily/periodic IS (Industrial Supervisor) progress call. In addition to the
two required keys above, also include:
"progress_reported": a JSON array of strings -- work completed since the last call.
"new_targets": a JSON array of objects {"task": string, "due_date": string|null} -- new
    targets or instructions given by the IS, with due dates per the same date-inference
    rule above where mentioned.
"blockers": a JSON array of strings -- blockers or concerns raised during the call.
"continuation_summary": a 1-paragraph string summarizing this call for injection into the
    next IS call's context (session chaining).
"""

PROJECT_SYSTEM_PROMPT = _BASE_CONTRACT + """
This is a project or consortium meeting. In addition to the two required keys above,
also include:
"attendees": a JSON array of attendee name strings (from diarisation or context; [] if unknown).
"agenda_items": a JSON array of agenda topic strings ([] if none provided in context).
"decisions": a JSON array of decision strings, each a complete, standalone sentence.
"next_meeting": a string date/time if a next meeting was mentioned, else null.
"project": a string project/work-package name if identifiable from context, else null.
"documents_referenced": a JSON array of strings naming any documents mentioned or provided
    as context ([] if none).
"""

SEMINAR_SYSTEM_PROMPT = _BASE_CONTRACT + """
This is a seminar or knowledge-sharing talk. In addition to the two required keys above,
also include:
"speaker": a string speaker name if identifiable, else null.
"topic": a string topic/title for the talk.
"key_concepts": a JSON array of self-contained concept strings introduced in the talk.
"notable_insights": a JSON array of direct or near-direct quotes worth preserving.
"open_questions": a JSON array of question strings raised during Q&A or left unresolved.
"references": a JSON array of strings naming papers, tools, or resources mentioned.
Seminars often have no assigned action items -- "action_items" should be [] unless the
speaker or attendees explicitly assigned someone a follow-up task.
"""

GENERAL_SYSTEM_PROMPT = _BASE_CONTRACT + """
This is a general or ad-hoc meeting/call -- not specifically an IS progress review, a
project/consortium meeting, or a seminar (e.g. a call with an external party, a quick
sync with a colleague). In addition to the two required keys above, also include:
"participants": a JSON array of participant name or role strings, [] if none identifiable.
"key_points": a JSON array of the main discussion points, each a self-contained string.
"decisions": a JSON array of decision strings, each a complete, standalone sentence
    ([] if none were made).
"""

# Kept for any external caller referencing the pre-v2 prompt name; identical
# in contract to PROJECT_SYSTEM_PROMPT, which is what an explicitly-typed
# project-meeting session resolves to (the default for an untyped/legacy
# session is now MeetingType.GENERAL, not PROJECT -- see meeting_type.py).
ACTION_ITEM_SYSTEM_PROMPT = PROJECT_SYSTEM_PROMPT

_SYSTEM_PROMPTS: dict[MeetingType, str] = {
    MeetingType.IS_CALL: IS_CALL_SYSTEM_PROMPT,
    MeetingType.PROJECT: PROJECT_SYSTEM_PROMPT,
    MeetingType.SEMINAR: SEMINAR_SYSTEM_PROMPT,
    MeetingType.GENERAL: GENERAL_SYSTEM_PROMPT,
}

_LIST_SUPPLEMENTARY_KEYS = (
    "progress_reported", "new_targets", "blockers",
    "attendees", "agenda_items", "decisions", "documents_referenced",
    "key_concepts", "notable_insights", "open_questions", "references",
    "participants", "key_points",
)
_SCALAR_SUPPLEMENTARY_KEYS = ("continuation_summary", "next_meeting", "project", "speaker", "topic")


class ExtractionError(RuntimeError):
    """Raised when the model's response cannot be interpreted as action items."""


def extract_action_items(
    session_id: str,
    meetings_dir: Path | str,
    state_dir: Path | str,
    lock_path: Path | str,
    lock_timeout: float,
    llm_client: LLMClient,
    meeting_type: MeetingType | None = None,
    recording_date: datetime | None = None,
) -> dict:
    validate_session_id(session_id)
    meetings_dir = Path(meetings_dir)
    transcript_path = meetings_dir / f"{session_id}.json"
    if not transcript_path.exists():
        raise FileNotFoundError(f"No transcript found for session '{session_id}' at {transcript_path}.")

    if meeting_type is None:
        meeting_type = load_meeting_type(meetings_dir / f"{session_id}.type")
    if recording_date is None:
        recording_date = datetime.fromtimestamp(transcript_path.stat().st_mtime, tz=timezone.utc)

    # Plain string replacement, not str.format(): the prompt templates contain
    # literal JSON-example braces (e.g. {"task": ...}) that str.format() would
    # misinterpret as additional placeholders.
    system_prompt = _SYSTEM_PROMPTS[meeting_type].replace(
        "{recording_date_iso}", recording_date.date().isoformat()
    )

    context_path = meetings_dir / f"{session_id}.context.txt"
    doc_context_path = meetings_dir / f"{session_id}.doc_context.txt"
    mail_context_path = meetings_dir / f"{session_id}.mail_context.txt"
    highlight_path = meetings_dir / f"{session_id}.highlights.json"

    additional_context = ""
    if context_path.exists():
        additional_context += f"\n\nMEETING CONTEXT:\n{context_path.read_text(encoding='utf-8')}\n"

    if doc_context_path.exists():
        additional_context += f"\n\nPRE-MEETING DOCUMENT CONTEXT (summarised):\n{doc_context_path.read_text(encoding='utf-8')}\n"

    if mail_context_path.exists():
        additional_context += f"\n\nMATCHED EMAIL CONTEXT:\n{mail_context_path.read_text(encoding='utf-8')}\n"

    # SB-3.1: filtered todo context instead of full-file read.
    todo_path = meetings_dir.parent / "todo.md"
    todo_ctx = _build_todo_context(todo_path)
    if todo_ctx:
        additional_context += f"\n\nCURRENT TASK CONTEXT (todo.md — active items only):\n{todo_ctx}\n"

    # SB-4.1 / Fix 2.2: session chaining with meeting-type filter and clear labelling.
    chaining_ctx = _load_prior_sessions(session_id, meetings_dir, meeting_type)
    if chaining_ctx:
        additional_context += chaining_ctx

    if highlight_path.exists():
        highlights = json.loads(highlight_path.read_text(encoding="utf-8"))
        additional_context += (
            f"\n\nIMPORTANT HIGHLIGHTS: The user explicitly highlighted {len(highlights)} moments "
            "during this recording. Pay special attention to the topics discussed."
        )
        notes = [h.get("note") for h in highlights if isinstance(h, dict) and h.get("note")]
        if notes:
            additional_context += "\nHighlight notes: " + "; ".join(notes)

    # Fix 3.5 / SB-5.2: inject negative examples filtered by meeting type.
    feedback_dir = meetings_dir.parent / "feedback"
    negative_examples = _load_negative_examples(feedback_dir, meeting_type)

    full_system_prompt = system_prompt + negative_examples + additional_context

    # SB-3.2: log token budget breakdown for diagnostics.
    logger.debug(
        "[%s] Context budget: system=%d chars, negative_ex=%d chars, "
        "additional=%d chars, total_prompt=%d chars",
        session_id,
        len(system_prompt), len(negative_examples),
        len(additional_context), len(full_system_prompt),
    )

    try:
        transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
        segments = transcript_data.get("segments", [])
        transcript_text = _render_transcript(transcript_data)

        # Fix 2.3: skip expensive LLM call for transcripts too short to be meaningful.
        if not _is_transcript_meaningful(transcript_text):
            logger.warning(
                "[%s] Transcript too short (%d words < %d threshold); "
                "returning minimal extraction without LLM call.",
                session_id, len(transcript_text.split()), MIN_TRANSCRIPT_WORDS,
            )
            result = {
                "summary": "- Transcript too short to extract meaningful content.",
                "action_items": [],
                "_short_transcript_warning": True,
            }
        else:
            from transcribe.chunker import chunk_transcript, estimate_tokens, merge_action_items

            if estimate_tokens(transcript_text) > CHUNK_THRESHOLD_TOKENS:
                result = _extract_chunked(
                    session_id, meetings_dir, segments, full_system_prompt, llm_client,
                    meeting_type, chunk_transcript, merge_action_items,
                )
            else:
                # Fix 2.2: label transcript clearly for the model.
                labeled_transcript = "CURRENT MEETING TRANSCRIPT:\n" + transcript_text
                result = _call_and_parse(llm_client, full_system_prompt, labeled_transcript)
    except Exception as exc:
        state_mod.transition(
            state_dir, session_id, state_mod.State.FAILED, lock_path, lock_timeout,
            error=str(exc),
        )
        raise

    result["action_items"] = link_dependencies(result.get("action_items", []))
    result["recording_date"] = recording_date.date().isoformat()

    actions_path = meetings_dir / f"{session_id}.actions.json"
    atomic_write_text(actions_path, json.dumps(result["action_items"], indent=2))

    summary_path = meetings_dir / f"{session_id}.summary.md"
    atomic_write_text(summary_path, result["summary"])

    mom_path = write_mom(session_id, result, meeting_type, meetings_dir)

    # Fix 3.2: compute quality score and log it; store in session metadata.
    from mcp_server.quality_gate import score_extraction

    quality = score_extraction(result, transcript_text, meeting_type)
    logger.info(
        "[%s] Quality: label=%s overall=%.2f grounding=%.2f completeness=%.2f "
        "action_density=%.2f flags=%s",
        session_id, quality.label, quality.overall, quality.grounding,
        quality.completeness, quality.action_density, quality.flags,
    )

    if meeting_type == MeetingType.IS_CALL:
        try:
            from mcp_server.tools.loop_closure import close_prior_targets

            close_prior_targets(
                state_dir, meetings_dir, session_id, result["summary"],
                llm_client.complete, lock_path, lock_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort enhancement, never fails the session
            logger.warning("Loop-closure reasoning failed for session %s: %s", session_id, exc)

        try:
            from mcp_server.tools.blocker_escalation import detect_recurring_blockers

            detect_recurring_blockers(meetings_dir, llm_client.complete)
        except Exception as exc:  # noqa: BLE001 - best-effort enhancement, never fails the session
            logger.warning("Recurring-blocker detection failed for session %s: %s", session_id, exc)

    session = state_mod.transition(
        state_dir, session_id, state_mod.State.EXTRACTED, lock_path, lock_timeout,
        actions_path=str(actions_path), action_item_count=len(result["action_items"]),
        meeting_type=meeting_type.value, mom_path=str(mom_path),
        quality_score=quality.overall, quality_label=quality.label,
        quality_flags=quality.flags,
    )
    return {
        "session_id": session_id,
        "state": session.state.value,
        "action_items": result["action_items"],
        "summary": result["summary"],
        "meeting_type": meeting_type.value,
        "mom_path": str(mom_path),
        "quality_label": quality.label,
        "quality_score": quality.overall,
        "quality_flags": quality.flags,
    }


def _is_transcript_meaningful(transcript_text: str) -> bool:
    """Fix 2.3: return False when the transcript is too short for useful extraction."""
    return len(transcript_text.split()) >= MIN_TRANSCRIPT_WORDS


def _build_todo_context(todo_path: Path, max_chars: int = 2000) -> str:
    """SB-3.1: return a filtered, capped slice of todo.md.

    Filters out done ([x]) and deleted items to avoid polluting the prompt with
    stale tasks. Caps at max_chars (~500 tokens) so a large todo.md doesn't eat
    the context budget.
    """
    if not todo_path.exists():
        return ""
    lines = todo_path.read_text(encoding="utf-8").splitlines()
    filtered: list[str] = []
    for line in lines:
        if re.match(r"^\s*-\s*\[x\]", line, re.IGNORECASE):
            continue
        lower = line.lower()
        if "status: deleted" in lower or "status: done" in lower:
            continue
        filtered.append(line)
    result = "\n".join(filtered)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n[... truncated — full list in data/todo.md ...]"
    return result


_PRIOR_SESSION_FALLBACK_WORDS = 800  # ~1100 tokens per prior session, worst case


def _load_prior_sessions(
    session_id: str,
    meetings_dir: Path,
    current_meeting_type: MeetingType,
    max_sessions: int = 3,
) -> str:
    """SB-4.1 / Fix 2.2: load prior notes for the same meeting slug, verifying that
    each candidate has the same meeting type as the current session.

    Prior sessions with a different (or unresolvable) meeting type are skipped to
    prevent cross-meeting-type context contamination (SB-4.1). The returned block
    is clearly labelled as reference-only to reduce hallucination risk (Fix 2.2).
    """
    match = re.match(r"^(.*)-(\d{8})-(\d{6})$", session_id)
    if not match:
        return ""
    slug, date_str, time_str = match.group(1), match.group(2), match.group(3)

    candidates: list[Path] = []
    for p in meetings_dir.glob(f"{slug}-*.md"):
        m2 = re.match(r"^(.*)-(\d{8})-(\d{6})\.md$", p.name)
        if not m2:
            continue
        prev_date, prev_time = m2.group(2), m2.group(3)
        if not (prev_date < date_str or (prev_date == date_str and prev_time < time_str)):
            continue
        type_file = meetings_dir / f"{p.stem}.type"
        try:
            prior_type = load_meeting_type(type_file)
        except Exception:
            prior_type = current_meeting_type
        if prior_type != current_meeting_type:
            logger.debug(
                "Skipping prior session %s for chaining: type %s != current %s",
                p.stem, prior_type.value, current_meeting_type.value,
            )
            continue
        candidates.append(p)

    if not candidates:
        return ""

    candidates.sort()
    candidates = candidates[-max_sessions:]

    parts = [
        "\n\n--- PRIOR SESSION CONTEXT (FOR REFERENCE ONLY) ---",
        "These notes are from PREVIOUS occurrences of this recurring meeting.",
        "Use them for continuity context ONLY. Do NOT reproduce items from prior sessions",
        "as new action items unless they were explicitly re-discussed in the current meeting.",
    ]
    for p in candidates:
        parts.append(f"\n--- Session {p.stem} ---")
        # Chain the prior session's SUMMARY, not its full transcript
        # (architecture.md documents "previous meeting summaries"; injecting
        # three full transcripts could exceed the model context on its own
        # and silently starve the *current* transcript — the exact
        # truncation bug class chunking exists to prevent). Fall back to a
        # word-capped transcript slice when no summary was produced.
        summary_path = p.with_name(f"{p.stem}.summary.md")
        if summary_path.exists():
            parts.append(summary_path.read_text(encoding="utf-8"))
        else:
            words = p.read_text(encoding="utf-8").split()
            capped = " ".join(words[:_PRIOR_SESSION_FALLBACK_WORDS])
            if len(words) > _PRIOR_SESSION_FALLBACK_WORDS:
                capped += " [...truncated prior-transcript fallback...]"
            parts.append(capped)
    parts.append("--- END PRIOR SESSION CONTEXT ---")
    return "\n".join(parts)


def _load_negative_examples(
    feedback_dir: Path,
    meeting_type: MeetingType,
    max_examples: int = 3,
) -> str:
    """Fix 3.5 / SB-5.2: inject recent human rejections as negative few-shot examples.

    Filters to the same meeting_type (SB-5.2) so IS-call rejections don't
    pollute project-meeting extraction and vice versa.
    """
    rejections_file = feedback_dir / "rejections.jsonl"
    if not rejections_file.exists():
        return ""
    examples: list[dict] = []
    for line in rejections_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("meeting_type") == meeting_type.value:
            examples.append(rec)
    examples = examples[-max_examples:]
    if not examples:
        return ""
    lines = [
        "\nNEGATIVE EXAMPLES — items previously REJECTED by the user from similar meetings.",
        "Do NOT extract items that match these patterns:",
    ]
    for ex in examples:
        reason = ex.get("rejection_reason") or "no reason given"
        lines.append(f'  REJECTED: "{ex.get("item_description", "")}" (reason: {reason})')
    lines.append("")
    return "\n".join(lines)


def _call_and_parse(llm_client: LLMClient, system_prompt: str, user_text: str) -> dict:
    raw_response = llm_client.complete(system_prompt, user_text)
    return _parse_extraction_result(raw_response)


def _extract_chunked(
    session_id: str,
    meetings_dir: Path,
    segments: list[dict],
    system_prompt: str,
    llm_client: LLMClient,
    meeting_type: MeetingType,
    chunk_transcript,
    merge_action_items,
) -> dict:
    """Sequential (single llama-server instance) chunked extraction for long transcripts."""
    chunks = chunk_transcript(segments)
    chunk_results: list[dict] = []
    for i, chunk_segments in enumerate(chunks):
        chunk_text = _render_transcript({"segments": chunk_segments})
        chunk_result = _call_and_parse(llm_client, system_prompt, chunk_text)
        atomic_write_text(
            meetings_dir / f"{session_id}.chunk_{i}.json", json.dumps(chunk_result, indent=2)
        )
        chunk_results.append(chunk_result)

    merged_action_items = merge_action_items([cr.get("action_items", []) for cr in chunk_results])
    concatenated_summary = "\n\n".join(cr.get("summary", "") for cr in chunk_results if cr.get("summary"))

    merged: dict = {"summary": concatenated_summary, "action_items": merged_action_items}
    for key in _LIST_SUPPLEMENTARY_KEYS:
        seen: list = []
        for cr in chunk_results:
            for v in cr.get(key, []) or []:
                if v not in seen:
                    seen.append(v)
        if seen:
            merged[key] = seen
    for key in _SCALAR_SUPPLEMENTARY_KEYS:
        for cr in chunk_results:
            if cr.get(key):
                merged[key] = cr[key]
                break

    try:
        merged = _synthesis_pass(merged, meeting_type, llm_client, len(chunks))
    except Exception as exc:  # noqa: BLE001 - polish step only; the python-side merge above is authoritative
        logger.warning(
            "Synthesis pass failed for session %s; using the deterministic per-chunk merge instead: %s",
            session_id, exc,
        )
    return merged


def _synthesis_pass(merged: dict, meeting_type: MeetingType, llm_client: LLMClient, n_chunks: int) -> dict:
    """One final LLM call that polishes the concatenated per-chunk summary/supplementary
    fields into one coherent narrative. `action_items` is never touched here -- the
    deterministic, deduplicated merge from merge_action_items() is authoritative."""
    synthesis_system = (
        f"You are given {n_chunks} sequential section extracts from one long meeting, in "
        "chronological order, already merged into draft fields. Rewrite them into one coherent "
        "record. Respond with ONLY a JSON object containing a cleaned-up \"summary\" string, plus "
        "any of the following keys that are present in the draft, cleaned up and deduplicated but "
        "not fabricated: " + ", ".join(_LIST_SUPPLEMENTARY_KEYS + _SCALAR_SUPPLEMENTARY_KEYS) + ". "
        "Do not include \"action_items\" in your response."
    )
    draft_for_llm = {k: v for k, v in merged.items() if k != "action_items"}
    raw_response = llm_client.complete(synthesis_system, json.dumps(draft_for_llm, indent=2))
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw_response.strip(), flags=re.MULTILINE).strip()
    polished = json.loads(cleaned)
    if not isinstance(polished, dict) or "summary" not in polished:
        raise ExtractionError("Synthesis pass did not return a JSON object with a 'summary' key.")
    result = dict(merged)
    result.update(polished)
    result["action_items"] = merged["action_items"]  # never overwritten by the synthesis call
    return result


def link_dependencies(action_items: list[dict]) -> list[dict]:
    """Resolve each item's `depends_on` description string to a `blocked_by_id`.

    Args:
        action_items: Extracted action items, each optionally carrying a
            `depends_on` key whose value is expected to (fuzzy-)match another
            item's `description` in the same list.

    Returns:
        The same list, with `depends_on` replaced by `blocked_by_id` (the
        matched item's `id`, if present) wherever a confident match is found,
        and `depends_on` dropped entirely wherever it doesn't (a hallucinated
        dependency is logged and discarded, not surfaced as an error).
    """
    import difflib

    descriptions = [item.get("description", "") for item in action_items]
    for item in action_items:
        depends_on = item.pop("depends_on", None)
        if not depends_on:
            continue
        candidates = [d for d in descriptions if d != item.get("description")]
        matches = difflib.get_close_matches(depends_on, candidates, n=1, cutoff=0.7)
        if not matches:
            logger.debug("link_dependencies: could not resolve depends_on=%r; dropping.", depends_on)
            continue
        matched_desc = matches[0]
        for other in action_items:
            if other.get("description") == matched_desc and other is not item:
                if other.get("id"):
                    item["blocked_by_id"] = other["id"]
                break
    return action_items


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
