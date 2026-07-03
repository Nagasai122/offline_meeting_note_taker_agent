"""
Fix 3.1: Rule-based quality scoring for extracted meeting data.

Scores happen synchronously after extraction and before the PROPOSED state
transition, so quality metadata travels with the session and is surfaced in
the review UI without any extra API calls.

No LLM calls here — scoring is deterministic and fast so it never adds
meaningful latency to the extraction pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mcp_server.meeting_type import MeetingType

STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "it", "to", "of", "in", "and", "or", "for",
    "on", "at", "by", "be", "was", "are", "with", "we", "this", "that",
    "will", "have", "has", "i", "you", "he", "she", "they", "our", "my",
    "do", "did", "not", "but", "so", "if", "can", "would", "should",
    "could", "may", "might", "need", "please", "just", "also", "been",
    "which", "from", "there", "then", "than", "all", "one", "new", "some",
    "as", "up", "out", "about", "into", "through", "during", "before",
    "after", "above", "below", "between", "each", "more", "other", "such",
    "no", "only", "same", "too", "very", "its", "own", "an",
})

_TYPE_SPECIFIC_REQUIRED_KEYS: dict[MeetingType, list[str]] = {
    MeetingType.IS_CALL: ["progress_reported", "continuation_summary"],
    MeetingType.PROJECT: ["decisions"],
    MeetingType.SEMINAR: ["key_concepts", "topic"],
    MeetingType.GENERAL: ["key_points"],
}


@dataclass
class QualityScore:
    overall: float
    completeness: float
    grounding: float
    action_density: float
    flags: list[str] = field(default_factory=list)
    label: str = "MEDIUM"  # "HIGH" | "MEDIUM" | "LOW"


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"\W+", text.lower()) if len(t) > 2 and t not in STOPWORDS}


def score_extraction(
    extracted: dict,
    transcript_text: str,
    meeting_type: MeetingType,
) -> QualityScore:
    """Compute a quality score for one extraction result.

    Weights:
      grounding    50%  — action item terms must appear in the transcript
      completeness 30%  — required fields present, meeting-type fields present
      action_density 20% — reasonable number of action items for transcript length
    """
    flags: list[str] = []
    transcript_tokens = _tokens(transcript_text)
    action_items = extracted.get("action_items", [])

    # --- grounding ---
    if action_items:
        item_scores: list[float] = []
        for item in action_items:
            item_toks = _tokens(item.get("description", ""))
            if not item_toks:
                item_scores.append(0.0)
                continue
            overlap = item_toks & transcript_tokens
            item_scores.append(len(overlap) / len(item_toks))
        grounding = sum(item_scores) / len(item_scores)
        if grounding < 0.35:
            flags.append("LOW_GROUNDING")
    else:
        grounding = 1.0

    # --- completeness ---
    has_summary = bool(str(extracted.get("summary", "")).strip())
    has_items_key = "action_items" in extracted
    completeness = 1.0 if (has_summary and has_items_key) else 0.5

    required = _TYPE_SPECIFIC_REQUIRED_KEYS.get(meeting_type, [])
    missing = [k for k in required if not extracted.get(k)]
    if missing:
        flags.append(f"MISSING_FIELDS:{','.join(missing)}")
        completeness *= 0.8

    # --- action density ---
    word_count = len(transcript_text.split()) if transcript_text else 0
    n_items = len(action_items)

    if meeting_type == MeetingType.SEMINAR:
        action_density = 1.0  # zero items is expected for seminars
    elif word_count < 60:
        action_density = 1.0  # too short to judge
    else:
        expected = max(1, word_count // 200)
        action_density = min(1.0, n_items / expected)
        if n_items == 0 and meeting_type == MeetingType.IS_CALL:
            flags.append("NO_ACTION_ITEMS_IS_CALL")
            action_density = 0.3

    overall = round(grounding * 0.5 + completeness * 0.3 + action_density * 0.2, 3)

    if overall >= 0.75:
        label = "HIGH"
    elif overall >= 0.50:
        label = "MEDIUM"
    else:
        label = "LOW"
        flags.append("LOW_OVERALL_QUALITY")

    return QualityScore(
        overall=overall,
        completeness=round(completeness, 3),
        grounding=round(grounding, 3),
        action_density=round(action_density, 3),
        flags=flags,
        label=label,
    )
