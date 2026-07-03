"""
Splits a long transcript into overlapping, token-bounded chunks for sequential
LLM extraction, and merges the resulting per-chunk action items back into one
deduplicated list. See architecture_v2.md §6 for the pipeline this feeds into.

Chunking (not parallel extraction) is deliberate: this project runs a single
local llama-server/vLLM instance, so chunks are always processed one at a
time, in order -- see mcp_server/tools/extraction.py's chunked-extraction
branch.
"""

from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass


def estimate_tokens(text: str) -> int:
    """Approximate token count for English technical speech (words * 1.35)."""
    return int(len(text.split()) * 1.35)


@dataclass
class _SegLike:
    """Duck-typed accessor so chunk_transcript works on both plain dicts
    (JSON-derived segments) and dataclass-style segment objects."""

    start: float
    end: float
    text: str


def _seg_text(segment: dict) -> str:
    return segment.get("text", "") or ""


def chunk_transcript(
    segments: list[dict],
    chunk_tokens: int = 5000,
    overlap_tokens: int = 400,
) -> list[list[dict]]:
    """Group transcript segments into chunks bounded by `chunk_tokens`.

    Each chunk after the first is prefixed with however many trailing segments
    of the previous chunk sum to roughly `overlap_tokens`, so context isn't
    lost across a chunk boundary.

    Args:
        segments: Ordered list of {"start", "end", "text", ...} dicts.
        chunk_tokens: Target maximum token count per chunk.
        overlap_tokens: Approximate token overlap carried into the next chunk.

    Returns:
        A list of segment-group lists; each group is itself a list of the
        original segment dicts (overlap segments are shared by reference,
        not copied, between consecutive chunks).
    """
    if not segments:
        return []

    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0

    for segment in segments:
        seg_tokens = estimate_tokens(_seg_text(segment))
        if current and current_tokens + seg_tokens > chunk_tokens:
            chunks.append(current)
            # Build the overlap prefix for the next chunk: trailing segments
            # of `current` whose combined tokens are ~overlap_tokens.
            overlap: list[dict] = []
            overlap_running = 0
            for seg in reversed(current):
                t = estimate_tokens(_seg_text(seg))
                if overlap_running >= overlap_tokens:
                    break
                overlap.insert(0, seg)
                overlap_running += t
            current = list(overlap)
            current_tokens = overlap_running
        current.append(segment)
        current_tokens += seg_tokens

    if current:
        chunks.append(current)

    return chunks


def merge_action_items(chunk_results: list[dict]) -> list[dict]:
    """Deduplicate action items extracted from consecutive, overlapping chunks.

    Two items are considered the same action if their descriptions match
    exactly, or fuzzy-match with a difflib.SequenceMatcher ratio > 0.85. When
    a duplicate pair is found, the surviving copy is whichever has a non-null
    due_date (or the first-seen one if both/neither do). Every surviving item
    is assigned a fresh id.

    Args:
        chunk_results: List of per-chunk `action_items` lists (each a list of
            action-item dicts with at least a "description" key).

    Returns:
        Merged, deduplicated list of action-item dicts, each with a fresh "id".
    """
    merged: list[dict] = []

    for chunk_items in chunk_results:
        for item in chunk_items:
            desc = (item.get("description") or "").strip()
            match_idx = None
            for i, existing in enumerate(merged):
                existing_desc = (existing.get("description") or "").strip()
                if desc == existing_desc:
                    match_idx = i
                    break
                ratio = difflib.SequenceMatcher(None, desc.lower(), existing_desc.lower()).ratio()
                if ratio > 0.85:
                    match_idx = i
                    break
            if match_idx is None:
                merged.append(dict(item))
            else:
                existing = merged[match_idx]
                if not existing.get("due_date") and item.get("due_date"):
                    merged[match_idx] = dict(item)
                # else: keep the first-seen (existing) copy.

    for item in merged:
        item["id"] = uuid.uuid4().hex[:8]

    return merged
