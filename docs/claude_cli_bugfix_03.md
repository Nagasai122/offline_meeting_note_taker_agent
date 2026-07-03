# Claude CLI Bug-Fix Prompt — Batch 03
# Agent Race Fix + Hallucination Reduction + Quality Gate
**Use with:** `claude` in `D:\meeting-agent`
**Generated:** 2026-07-02

Paste everything below as a single prompt to Claude Code:

---

```
You are fixing three confirmed issues in the Meeting Agent codebase at D:\meeting-agent:
1. Agent-pipeline race condition: agent tries to re-transcribe already-transcribed sessions
2. LLM hallucination: model fills gaps with plausible content rather than null
3. Quality gate: add a scoring pass between EXTRACTED and PROPOSED

Work in order. Read every file before modifying it.

===========================================================================
FIX 1 — AGENT/PIPELINE RACE: transcribe_meeting TOOL GUARD
===========================================================================

The pipeline (cli/web.py run_pipeline) performs transcription itself, then launches
the agent. The agent has access to the transcribe_meeting MCP tool and calls it
immediately, finding the WAV already deleted → FileNotFoundError → session FAILED.

Two changes needed:

--- FIX 1.1: Guard inside transcribe_meeting MCP tool ---

File: mcp_server/tools/transcription.py  (or wherever transcribe_meeting is defined —
find it by searching: grep -rn "def transcribe_meeting\|transcribe_meeting" mcp_server/)

Read the function. At the very start, before any file access, add a state check:

    from mcp_server import state as state_mod
    from pathlib import Path
    import settings as settings_mod  # or however settings is imported in this file

    # Load current session state
    try:
        session = state_mod.load_session_state(
            state_mod._get_state_dir(),  # adapt to actual settings access
            session_id
        )
        if session.state not in (state_mod.State.STOPPED,):
            # Session is already past STOPPED — transcription already completed
            return {
                "status": "skipped",
                "reason": (
                    f"Session is already in state '{session.state.value}'. "
                    f"Transcription was handled by the pipeline orchestrator. "
                    f"Proceed to the next stage."
                ),
                "current_state": session.state.value,
            }
    except Exception as exc:
        # If we cannot check state, proceed cautiously with transcription
        import logging
        logging.getLogger(__name__).warning(
            "Could not check session state before transcription: %s", exc
        )

The exact import paths will depend on how this file accesses settings and state.
Read the file first to understand the pattern used, then adapt.

--- FIX 1.2: Agent system prompt — enforce state check first ---

File: agent/prompts/system_prompt.md  (or equivalent — find with:
grep -rln "get_session_status\|transcribe_meeting\|Drive session forward" agent/)

Read the system prompt. Add the following rule at the top of the instructions,
BEFORE any tool descriptions:

    ## MANDATORY FIRST STEP — ALWAYS DO THIS BEFORE ANY TOOL CALL

    Before calling ANY tool, you MUST call `get_session_status` to read the
    current pipeline stage of this session.

    NEVER call a tool for a stage that has already been completed:
    - If state is TRANSCRIBED or beyond: do NOT call `transcribe_meeting`
    - If state is EXTRACTED or beyond: do NOT call any extraction tool again
    - If state is PROPOSED or beyond: the session is awaiting human review — stop

    The pipeline orchestrator handles transcription before launching you.
    Your job starts at the TRANSCRIBED stage. Act accordingly.

    If `transcribe_meeting` returns {"status": "skipped"}, this is CORRECT
    behaviour. Proceed immediately to extraction.

--- FIX 1.3: Filter tool list for agent invocation ---

File: cli/web.py (or agent/__main__.py or wherever the agent is launched)

Find where the agent is launched after transcription. Before the agent-run call,
pass a flag or environment variable indicating transcription is complete:

Option A — environment variable:
    env = os.environ.copy()
    env["MA_TRANSCRIPTION_DONE"] = "1"
    # Pass env to the agent subprocess

Option B — direct flag in agent invocation:
    Find how agent-run is invoked (likely subprocess or direct Python call).
    Add --skip-transcription flag if the agent supports it.
    If not, add this flag to the agent CLI.

In the agent itself, if MA_TRANSCRIPTION_DONE is set, remove transcribe_meeting
from the tool registry before the agent loop starts:

    if os.environ.get("MA_TRANSCRIPTION_DONE") == "1":
        # Remove transcription tool from available tools
        tool_registry.pop("transcribe_meeting", None)

Read the agent code to understand how tools are registered before implementing this.

--- VERIFY FIX 1 ---

Search for the transcribe_meeting tool definition:
grep -rn "transcribe_meeting" mcp_server/ agent/

Verify the guard is in place:
python -c "
import ast
# Find the transcription tool file from the grep above and verify it parses
src = open('mcp_server/tools/transcription.py').read()  # adjust path
ast.parse(src)
print('transcription tool file parses OK')
assert 'skipped' in src, 'Guard not found in transcription tool'
print('State guard: PRESENT')
"

To simulate the race:
python -c "
# After a session is TRANSCRIBED, call the tool and verify it returns 'skipped'
# This requires the server to be running — skip if not
print('Manual test: start a session, let it transcribe, then check the tool guard')
print('Expected: transcribe_meeting returns {status: skipped}')
"

===========================================================================
FIX 2 — HALLUCINATION REDUCTION: PROMPT GROUNDING + NULL ENFORCEMENT
===========================================================================

The LLM fills gaps with plausible content instead of outputting null for missing data.
This is a prompt engineering fix — not a model size fix.

--- FIX 2.1: Add transcript-grounding constraints to ALL extraction prompts ---

File: mcp_server/tools/extraction.py

Read all system prompts: IS_CALL_SYSTEM_PROMPT, PROJECT_SYSTEM_PROMPT,
SEMINAR_SYSTEM_PROMPT, GENERAL_SYSTEM_PROMPT.

Add the following GROUNDING RULES section to every prompt, immediately before
the JSON schema definition:

    ## GROUNDING RULES — READ CAREFULLY

    You are a transcript analyst, not a creative writer.
    Your ONLY source of truth is the transcript provided below.

    STRICT RULES:
    1. Every fact in your output must be explicitly stated in the transcript.
       Do NOT infer, assume, extrapolate, or complete partial information.
    2. If a field's value is not clearly stated in the transcript, output null.
       A null is CORRECT. A plausible-sounding but unverified answer is WRONG.
    3. Names: only include names you can literally read in the transcript.
       Do NOT generate names from context or typical meeting patterns.
    4. Dates: only include dates explicitly stated. "next Tuesday" → compute the
       date from {recording_date_iso}. "soon" or "eventually" → null.
    5. Action items: if you are not certain an item was explicitly assigned as
       a task, do not include it. A mentioned topic is NOT an action item.
    6. Summary: describe only what was discussed. Do not add recommendations
       or observations that are not in the transcript.

    FAILURE MODE TO AVOID:
    Producing fluent, professional-sounding output that does not reflect the
    actual transcript. It is better to return a sparse result with many nulls
    than a complete-looking result that contains hallucinated content.

Also add at the end of each prompt, after the JSON schema:

    FINAL INSTRUCTION: Before outputting, scan your response and for each
    non-null field, verify you can point to a specific line in the transcript
    that supports it. If you cannot, set it to null.
    Output only valid JSON. No markdown fences. No explanations.

--- FIX 2.2: Session chaining context isolation ---

File: mcp_server/tools/extraction.py  (the section that builds session chaining context)

Currently prior sessions' notes are injected without a clear delimiter, which can
cause the LLM to confuse prior session content with the current session's transcript.

Wrap the chaining context in a clearly-labelled section:

    CHAINING_CONTEXT_WRAPPER = """
    ## PRIOR SESSION CONTEXT (FOR REFERENCE ONLY — NOT FROM THIS MEETING)

    The following notes are from PREVIOUS sessions with the same person/project.
    This context is provided so you understand the ongoing work.
    DO NOT treat this as content from the current meeting transcript.
    DO NOT extract action items from this section.
    Only use it to understand abbreviations, project names, or running themes
    if they appear in the current transcript.

    ---
    {prior_sessions_content}
    ---

    ## CURRENT MEETING TRANSCRIPT (EXTRACT FROM THIS SECTION ONLY)
    """

Replace the current chaining context injection with this wrapped version.
The separator "## CURRENT MEETING TRANSCRIPT" must appear as a clear heading
that signals to the LLM where the extractable content begins.

--- FIX 2.3: Empty transcript handling ---

File: mcp_server/tools/extraction.py

Before making the LLM call, check if the transcript text is empty or too short
to be meaningful:

    MIN_TRANSCRIPT_TOKENS = 50  # fewer than ~35 words → not a real meeting

    def _is_transcript_meaningful(transcript_text: str) -> bool:
        word_count = len(transcript_text.split())
        return word_count >= MIN_TRANSCRIPT_TOKENS

    if not _is_transcript_meaningful(transcript_text):
        logger.warning(
            "Transcript for session %s is too short (%d words) — "
            "skipping LLM extraction to avoid hallucination.",
            session_id,
            len(transcript_text.split())
        )
        return {
            "summary": "Transcript too short for meaningful extraction.",
            "action_items": [],
            "warning": "SHORT_TRANSCRIPT",
            "word_count": len(transcript_text.split()),
        }

This prevents the LLM from being asked to extract content from a 10-word transcript
and hallucinating a full meeting summary to fill the void.

--- VERIFY FIX 2 ---

python -c "
src = open('mcp_server/tools/extraction.py').read()
assert 'GROUNDING RULES' in src, 'Grounding rules not found in extraction prompts'
assert 'PRIOR SESSION CONTEXT' in src, 'Chaining context wrapper not found'
assert 'MIN_TRANSCRIPT_TOKENS' in src or 'is_transcript_meaningful' in src, \
    'Short transcript guard not found'
print('Fix 2: All hallucination guards PRESENT')
"

===========================================================================
FIX 3 — QUALITY GATE BETWEEN EXTRACTED AND PROPOSED
===========================================================================

Add a lightweight, fast quality-scoring pass after extraction completes.
If the score is below threshold, the session still moves to PROPOSED (human review)
but the low-quality flag is surfaced prominently in the UI so the user knows to
scrutinise it. Nothing is auto-rejected without human involvement.

--- FIX 3.1: Quality scorer module ---

Create file: mcp_server/quality_gate.py

"""
Lightweight rule-based quality gate for LLM extraction outputs.
Runs between EXTRACTED and PROPOSED states. Does not call the LLM — pure Python.
Computes a QualityScore with component scores and flags.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QualityScore:
    """Composite quality assessment for one extraction result."""
    
    overall: float          # 0.0–1.0
    completeness: float     # fraction of required fields that are non-null
    grounding: float        # fraction of extracted entities found in transcript
    action_density: float   # action items per 1000 words (too high = hallucination risk)
    flags: list[str] = field(default_factory=list)
    
    @property
    def label(self) -> str:
        if self.overall >= 0.75:
            return "GOOD"
        if self.overall >= 0.50:
            return "REVIEW_CAREFULLY"
        return "LOW_CONFIDENCE"


def score_extraction(
    extracted: dict[str, Any],
    transcript_text: str,
    meeting_type: str,
) -> QualityScore:
    """Score an extraction result against the source transcript.
    
    Args:
        extracted: The parsed JSON output from the LLM extraction.
        transcript_text: The raw transcript text (all segments joined).
        meeting_type: One of is-call, project-meeting, seminar, general.
    
    Returns:
        QualityScore with overall score and component breakdowns.
    """
    flags: list[str] = []
    
    # --- Completeness score ---
    # Check that key fields are non-null and non-empty
    required_fields = {
        "is-call": ["summary", "action_items"],
        "project-meeting": ["summary", "action_items", "decisions"],
        "seminar": ["summary", "key_concepts"],
        "general": ["summary", "action_items"],
    }.get(meeting_type, ["summary", "action_items"])
    
    present = sum(
        1 for f in required_fields
        if extracted.get(f) not in (None, [], "", {})
    )
    completeness = present / max(len(required_fields), 1)
    
    if completeness < 0.5:
        flags.append("INCOMPLETE_EXTRACTION")
    
    # --- Grounding score ---
    # Check what fraction of extracted names/terms appear in the transcript
    # Use a simple token-overlap approach: extract capitalised words/phrases
    # from action item descriptions and check they appear in the transcript
    transcript_lower = transcript_text.lower()
    
    action_items = extracted.get("action_items", []) or []
    grounded_items = 0
    
    for item in action_items:
        desc = (item.get("description") or "").lower()
        if not desc:
            continue
        # Check if at least 50% of the significant words in the description
        # appear in the transcript
        words = [w for w in re.findall(r'\b[a-z]{4,}\b', desc)
                 if w not in STOPWORDS]
        if not words:
            grounded_items += 1  # short descriptions get benefit of doubt
            continue
        overlap = sum(1 for w in words if w in transcript_lower)
        if overlap / len(words) >= 0.5:
            grounded_items += 1
        else:
            flags.append(f"POSSIBLY_HALLUCINATED: '{item.get('description', '')[:60]}'")
    
    grounding = grounded_items / max(len(action_items), 1) if action_items else 1.0
    if grounding < 0.6 and action_items:
        flags.append("LOW_GROUNDING")
    
    # --- Action density ---
    # Extremely high density suggests hallucination
    word_count = max(len(transcript_text.split()), 1)
    density = len(action_items) / (word_count / 1000)
    action_density_score = 1.0
    
    if density > 15:  # more than 15 action items per 1000 words is suspicious
        flags.append("UNUSUALLY_HIGH_ACTION_DENSITY")
        action_density_score = 0.5
    elif density > 10:
        flags.append("HIGH_ACTION_DENSITY")
        action_density_score = 0.75
    
    # --- Summary sanity ---
    summary = extracted.get("summary") or ""
    if len(summary) < 30:
        flags.append("SUMMARY_TOO_SHORT")
        completeness *= 0.8
    elif len(summary) > 3000:
        flags.append("SUMMARY_UNUSUALLY_LONG")
    
    # --- Overall score ---
    # Weighted: grounding matters most, then completeness, then density
    overall = (
        grounding * 0.50 +
        completeness * 0.35 +
        action_density_score * 0.15
    )
    
    return QualityScore(
        overall=round(overall, 3),
        completeness=round(completeness, 3),
        grounding=round(grounding, 3),
        action_density=round(density, 1),
        flags=flags,
    )


STOPWORDS = frozenset({
    "that", "this", "with", "from", "have", "will", "been", "were",
    "they", "them", "their", "what", "when", "where", "which", "while",
    "about", "after", "also", "into", "more", "some", "such", "than",
    "then", "there", "these", "those", "your", "each", "make", "need",
})

--- FIX 3.2: Wire quality gate into extraction pipeline ---

File: mcp_server/tools/extraction.py

After the extraction result is parsed and before writing .actions.json,
call the quality gate:

    from mcp_server.quality_gate import score_extraction
    
    quality = score_extraction(extracted_data, transcript_text, meeting_type)
    
    logger.info(
        "Quality gate for session %s: %s (overall=%.2f, grounding=%.2f, flags=%s)",
        session_id, quality.label, quality.overall, quality.grounding, quality.flags
    )
    
    # Store quality score as session metadata
    # This is surfaced in the PROPOSED review UI
    metadata_updates["quality_score"] = quality.overall
    metadata_updates["quality_label"] = quality.label
    metadata_updates["quality_flags"] = quality.flags

Then call transition() with these metadata_updates when moving to PROPOSED.

--- FIX 3.3: Show quality score in Needs Review UI ---

File: cli/web.py — GET /api/review/pending endpoint
File: static/app.js — Needs Review tab rendering

In the /api/review/pending response, include quality metadata for each PROPOSED session:

    {
        "session_id": "...",
        "quality_label": "REVIEW_CAREFULLY",
        "quality_score": 0.52,
        "quality_flags": ["LOW_GROUNDING", "POSSIBLY_HALLUCINATED: 'update the report...'"],
        ...
    }

Load this from the session's state metadata (set in Fix 3.2).

In app.js, in the Needs Review card for each session:
- GOOD: green dot + "High confidence"
- REVIEW_CAREFULLY: yellow warning icon + "Review carefully — some items may be inaccurate"
- LOW_CONFIDENCE: red warning icon + "Low confidence — likely contains inaccuracies"
  + list the quality_flags as a collapsible detail

Apply esc() to quality_flags strings before inserting into innerHTML.

--- FIX 3.4: HITL feedback loop — store rejection data ---

Create file: cli/feedback.py

When the user rejects an action item in the Needs Review UI, record it:

    def record_rejection(
        session_id: str,
        item_id: str,
        item_description: str,
        rejection_reason: str | None,
        feedback_dir: Path,
    ) -> None:
        """Append a rejection record to data/feedback/rejections.jsonl.
        
        Each line is a JSON object with:
        - session_id, item_id, item_description (original LLM output)
        - rejection_reason (user-supplied or null)
        - timestamp
        - quality_flags from the quality gate (loaded from session metadata)
        
        This file is used to:
        1. Track rejection patterns over time
        2. Populate negative few-shot examples in future extraction prompts
        3. Feed a future fine-tuning dataset (when enough examples accumulate)
        """

When the user edits an action item before accepting (modifies the text), record:

    def record_edit(
        session_id: str,
        item_id: str,
        original: str,
        corrected: str,
        feedback_dir: Path,
    ) -> None:
        """Append an edit record to data/feedback/edits.jsonl."""

Wire these into the /api/review/decide endpoint in cli/web.py:
- When a decision is "rejected": call record_rejection
- When an item's description differs from original: call record_edit

--- FIX 3.5: Negative few-shot examples in extraction prompt ---

File: mcp_server/tools/extraction.py

Add a function that loads recent rejections and injects them as negative examples:

    def _load_negative_examples(
        feedback_dir: Path,
        max_examples: int = 3,
    ) -> str:
        """Load recent rejection records and format as negative few-shot examples.
        
        Returns empty string if no rejections exist yet.
        Format injected into the prompt:
        
        ## EXAMPLES OF INCORRECT EXTRACTION (DO NOT REPLICATE)
        
        The following were marked incorrect by the user. Avoid similar errors:
        - REJECTED: "Update the website" — this was inferred, not explicitly assigned
        - REJECTED: "Complete the report" — too vague, no specific owner or deadline
        """
        jsonl = feedback_dir / "rejections.jsonl"
        if not jsonl.exists():
            return ""
        
        import json
        lines = jsonl.read_text().strip().splitlines()
        recent = lines[-max_examples:]  # take the most recent N rejections
        
        if not recent:
            return ""
        
        examples = []
        for line in recent:
            try:
                record = json.loads(line)
                desc = record.get("item_description", "")[:100]
                reason = record.get("rejection_reason") or "marked incorrect"
                examples.append(f'- REJECTED: "{desc}" — {reason}')
            except json.JSONDecodeError:
                continue
        
        if not examples:
            return ""
        
        return (
            "\n## EXAMPLES OF INCORRECT EXTRACTION (DO NOT REPLICATE)\n\n"
            "These were rejected by the user. Avoid similar errors:\n"
            + "\n".join(examples)
            + "\n"
        )

Inject the result of _load_negative_examples() into the system prompt,
between the GROUNDING RULES section and the JSON schema.

--- VERIFY FIX 3 ---

python -c "
from mcp_server.quality_gate import score_extraction, QualityScore

# Test with a grounded extraction
transcript = 'John said he will fix the authentication bug by Friday. Sarah will update the documentation.'
extracted = {
    'summary': 'John will fix auth bug. Sarah updates docs.',
    'action_items': [
        {'id': '1', 'description': 'fix the authentication bug', 'assignee': 'John', 'due_date': '2026-07-04', 'priority': 'HIGH'},
        {'id': '2', 'description': 'update the documentation', 'assignee': 'Sarah', 'due_date': None, 'priority': 'MEDIUM'},
    ],
    'decisions': [],
}
score = score_extraction(extracted, transcript, 'general')
print(f'Grounded extraction score: {score.overall} ({score.label})')
assert score.overall >= 0.6, f'Expected >= 0.6, got {score.overall}'
assert score.label in ('GOOD', 'REVIEW_CAREFULLY'), f'Unexpected label: {score.label}'

# Test with a hallucinated extraction
transcript_short = 'We had a quick catch-up.'
extracted_hallucinated = {
    'summary': 'Comprehensive team meeting covering all project areas with detailed discussion.',
    'action_items': [
        {'id': '1', 'description': 'prepare quarterly financial report', 'assignee': 'Finance team', 'due_date': '2026-07-15', 'priority': 'HIGH'},
        {'id': '2', 'description': 'schedule stakeholder interviews', 'assignee': 'Product manager', 'due_date': '2026-07-10', 'priority': 'MEDIUM'},
        {'id': '3', 'description': 'review architecture documentation', 'assignee': 'Tech lead', 'due_date': '2026-07-08', 'priority': 'LOW'},
    ],
    'decisions': ['Approved new budget', 'Selected vendor'],
}
score2 = score_extraction(extracted_hallucinated, transcript_short, 'general')
print(f'Hallucinated extraction score: {score2.overall} ({score2.label})')
print(f'Flags: {score2.flags}')
assert score2.overall < 0.6, f'Expected < 0.6, got {score2.overall} — hallucination not caught'
assert score2.label == 'LOW_CONFIDENCE', f'Expected LOW_CONFIDENCE, got {score2.label}'

print('Quality gate tests PASS')
"

===========================================================================
MODEL SIZING — DUAL PROFILE SETUP
===========================================================================

The 7B model is appropriate. Do NOT switch to 30B — it would be slower with no
quality benefit for structured extraction. Instead, add a smaller fast profile
for per-chunk extraction.

--- STEP M.1: Add Qwen2.5-3B extraction profile ---

File: llm/model_profiles.py

Read the file to understand the profile format. Add a new profile:

    "qwen2_5_3b_gguf": ModelProfile(
        name="qwen2_5_3b_gguf",
        description="Qwen2.5-3B-Instruct-Q4_K_M — fast extraction profile",
        repo_id="Qwen/Qwen2.5-3B-Instruct-GGUF",
        filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        ctx_size=8192,
        extra_launch_args=["--ctx-size", "8192", "--n-gpu-layers", "99"],
        use_case="per_chunk_extraction",  # add this field if it doesn't exist
    )

This profile downloads ~2GB vs 4.68GB for 7B, and runs ~2× faster.
It is suitable for per-chunk extraction but NOT for synthesis passes (keep 7B for those).

--- STEP M.2: Download the 3B profile (optional — run only when ready to test) ---

This is the one permitted network call for model setup:
    meeting-agent setup --profile qwen2_5_3b_gguf

Do NOT run this automatically. Just ensure the profile definition is registered.

--- STEP M.3: Configuration for dual-model routing ---

File: llm/model_profiles.py or a new llm/routing.py

Add a routing function:

    def select_profile_for_task(task: str, default_profile: str) -> str:
        """Select the appropriate model profile for a given task.
        
        Args:
            task: One of 'extraction', 'synthesis', 'context_summary', 'weekly_pattern'
            default_profile: The user's configured default profile name
        
        Returns:
            Profile name to use for this task.
        """
        FAST_TASKS = {'extraction', 'context_summary'}
        
        fast_profile = "qwen2_5_3b_gguf"
        # Only use fast profile if it exists and 3B weights are downloaded
        weights_exist = _weights_path_for_profile(fast_profile).exists()
        
        if task in FAST_TASKS and weights_exist:
            return fast_profile
        return default_profile

Wire this into the extraction pipeline so per-chunk extraction uses the 3B profile
when available, and synthesis uses the configured default (7B).

===========================================================================
FINAL VERIFICATION
===========================================================================

1. python -m py_compile mcp_server/tools/extraction.py mcp_server/quality_gate.py \
   cli/feedback.py cli/web.py llm/model_profiles.py
   All must exit 0.

2. Run quality gate tests:
   python -c "
   from mcp_server.quality_gate import score_extraction
   # grounded case
   s = score_extraction({'summary': 'fix the bug', 'action_items': [{'id':'1','description':'fix bug','assignee':None,'due_date':None,'priority':'HIGH'}]}, 'we need to fix the bug', 'general')
   assert s.overall >= 0.5, f'Grounded case failed: {s}'
   print('Quality gate: PASS')
   "

3. Verify chaining wrapper:
   python -c "
   src = open('mcp_server/tools/extraction.py').read()
   assert 'PRIOR SESSION CONTEXT' in src
   assert 'CURRENT MEETING TRANSCRIPT' in src
   assert 'GROUNDING RULES' in src
   print('Prompt grounding: PASS')
   "

4. Verify agent tool guard:
   grep -n 'skipped\|already.*state\|TRANSCRIBED' mcp_server/tools/transcription.py
   # Must show the guard returning 'skipped' for non-STOPPED sessions

5. Start meeting-agent serve and attempt a full pipeline run.
   The "Processing Failed — No audio found" error must NOT appear.
   The pipeline must complete to PROPOSED state.

Report:
- Fix 1 (race condition): PASS/FAIL — include whether "Processing Failed" still occurs
- Fix 2 (hallucination): PASS/FAIL — include confirmation grounding rules are in prompts
- Fix 3 (quality gate): PASS/FAIL — include the test output from quality gate tests
- Model profiles: confirm qwen2_5_3b_gguf profile is defined (not necessarily downloaded)
```
