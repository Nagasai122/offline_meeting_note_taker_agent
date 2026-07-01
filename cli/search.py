"""
Offline BM25 keyword search over meeting notes (data/meetings/*.md and
data/meetings/*.summary.md).

Design rationale (from PIECE 2 design comparison):
- Option A (llama-server embeddings) would require a second server process /
  a second model profile, complicating the zero-egress lifecycle and VRAM
  accounting.  Option B (BM25) is pure Python, zero new model downloads, and
  adequate recall for this tool's typical query pattern (searching by keyword
  or topic name, not paraphrased natural-language queries).

Index strategy — mtime-cached rebuild:
- The document corpus (file contents + tokenisation) is cached in-process,
  keyed on the newest mtime among all *.md files under meetings_dir.  A
  search against an unchanged directory reuses the cached corpus instead of
  re-reading and re-tokenising every file from disk; as soon as any file is
  added or rewritten, the signature changes and the corpus is rebuilt.  This
  keeps the class of bugs an incremental index would otherwise risk (index
  falls behind disk) off the table, while avoiding O(N) disk I/O on every
  keystroke of a debounced search box.  Still no background tasks, no temp
  files, no git-tracked artefacts -- the cache is purely in-memory.

Zero-egress guarantee: rank_bm25 is a pure-Python package, no network calls.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path


def _tokenise(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric runs, filter empty tokens."""
    return [tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if tok]


@dataclass
class SearchResult:
    session_id: str
    score: float
    snippet: str   # first ~200 chars of the most relevant file
    source: str    # "summary" | "transcript"


def _snippet(text: str, query_tokens: set[str], max_len: int = 200) -> str:
    """Return a short passage that contains at least one query token, or the
    start of the document if none are found."""
    lower = text.lower()
    for tok in query_tokens:
        idx = lower.find(tok)
        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(text), start + max_len)
            raw = text[start:end].strip()
            return ("…" if start > 0 else "") + raw + ("…" if end < len(text) else "")
    return text[:max_len].strip() + ("…" if len(text) > max_len else "")


def _corpus_signature(meetings_dir: Path) -> float:
    """Cache-invalidation key for _load_corpus: the newest mtime among all
    relevant files. A directory mtime alone would miss an in-place rewrite of
    an existing file's content (some filesystems only bump dir mtime on
    add/remove), so the signature is the max of every candidate file's own
    mtime instead."""
    latest = 0.0
    for p in meetings_dir.glob("*.md"):
        latest = max(latest, p.stat().st_mtime)
    return latest


@functools.lru_cache(maxsize=1)
def _load_corpus(meetings_dir: Path, signature: float) -> tuple[list[tuple[str, str, str]], list[list[str]]]:
    """Gather documents (prefer .summary.md when both exist, else .md) and
    their tokenised form. Cached per (meetings_dir, signature) so repeated
    searches -- e.g. the debounced search-as-you-type box -- don't re-read
    and re-tokenise every file under meetings_dir on every keystroke; the
    cache is invalidated automatically as soon as `signature` changes."""
    docs: list[tuple[str, str, str]] = []  # (session_id, text, source)
    seen: set[str] = set()

    # Summaries first (higher quality signal)
    for p in sorted(meetings_dir.glob("*.summary.md")):
        session_id = p.name.replace(".summary.md", "")
        text = p.read_text(encoding="utf-8", errors="replace")
        docs.append((session_id, text, "summary"))
        seen.add(session_id)

    # Full transcripts for sessions with no summary
    for p in sorted(meetings_dir.glob("*.md")):
        # skip .summary.md (already handled), skip .actions.md if any
        if p.name.endswith(".summary.md"):
            continue
        session_id = p.stem  # e.g. "standup-2026-06-30"
        if session_id in seen:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        docs.append((session_id, text, "transcript"))
        seen.add(session_id)

    corpus = [_tokenise(text) for _, text, _ in docs]
    return docs, corpus


def search_meetings(
    meetings_dir: Path | str,
    query: str,
    max_results: int = 10,
) -> list[SearchResult]:
    """Rank the (cached) corpus built from all .summary.md and .md files
    under meetings_dir against `query`, and return up to `max_results`
    results with a score and snippet."""
    from rank_bm25 import BM25Plus

    meetings_dir = Path(meetings_dir)
    query_tokens = _tokenise(query)
    if not query_tokens or not meetings_dir.exists():
        return []

    docs, corpus = _load_corpus(meetings_dir, _corpus_signature(meetings_dir))
    if not docs:
        return []

    # BM25Plus (not BM25Okapi): IDF = log(N/n + 1) is strictly positive, so
    # a single-document corpus still returns a non-zero score for matching terms.
    bm25 = BM25Plus(corpus)
    scores = bm25.get_scores(query_tokens)

    # Pair and sort descending
    ranked = sorted(
        ((float(scores[i]), docs[i]) for i in range(len(docs))),
        key=lambda x: x[0],
        reverse=True,
    )

    results: list[SearchResult] = []
    for score, (session_id, text, source) in ranked[:max_results]:
        if score <= 0.0:
            break
        results.append(SearchResult(
            session_id=session_id,
            score=round(score, 4),
            snippet=_snippet(text, set(query_tokens)),
            source=source,
        ))
    return results
