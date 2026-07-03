"""
Cross-session semantic search over meeting history (audit 2026-07, Strand E
candidate 1 stage 1 — approved).

Local-only hybrid retrieval:
- dense: fastembed (ONNX, CPU) BAAI/bge-small-en-v1.5 vectors in a
  sqlite-vec single-file index at data/semantic_index.db;
- sparse: the existing BM25 ranker in cli/search.py;
- fused with reciprocal-rank fusion (RRF), so a paraphrase ("Redis
  migration") finds "caching layer" meetings and exact tokens still win
  when they should.

Zero-egress: the embedding model is fetched once by `meeting-agent setup`
into the HF cache; at runtime fastembed loads from cache under
HF_HUB_OFFLINE=1 and never touches the network. Indexing respects the same
SB-6 privacy rule as BM25: only PROPOSED/REVIEWED/APPLIED sessions are
indexable. The index is derived data — safe to delete, rebuilt on demand.

Degrades gracefully: if fastembed/sqlite-vec or the cached model are
missing, `hybrid_search` returns plain BM25 results with
`semantic_available=False` rather than failing the search box.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
_EMBED_DIM = 384
_CHUNK_WORDS = 180          # ~1 paragraph of transcript per vector
_RRF_K = 60                 # standard RRF constant

_INDEXABLE_STATES = {"PROPOSED", "REVIEWED", "APPLIED"}


def _embedder():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=EMBED_MODEL)


def _open_index(db_path: Path):
    import sqlite3

    import sqlite_vec

    db = sqlite3.connect(db_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute(
        f"""CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING vec0(
            embedding float[{_EMBED_DIM}],
            +session_id TEXT,
            +artefact TEXT,
            +snippet TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS indexed_files (
            path TEXT PRIMARY KEY, mtime REAL NOT NULL
        )"""
    )
    return db


def _session_state(state_dir: Path, session_id: str) -> str | None:
    state_file = state_dir / f"{session_id}.json"
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text(encoding="utf-8")).get("state")
    except (ValueError, OSError):
        return None


def _chunk_words(text: str) -> list[str]:
    words = text.split()
    if not words:
        return []
    return [
        " ".join(words[i : i + _CHUNK_WORDS])
        for i in range(0, len(words), _CHUNK_WORDS)
    ]


def refresh_index(
    meetings_dir: Path | str,
    state_dir: Path | str,
    db_path: Path | str,
    embedder=None,
) -> dict:
    """Incrementally (re-)index changed artefacts. Returns counts.

    `embedder` is injectable for tests; anything with
    `.embed(list[str]) -> iterable of vectors` works.
    """
    meetings_dir = Path(meetings_dir)
    state_dir = Path(state_dir)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if embedder is None:
        embedder = _embedder()
    db = _open_index(db_path)

    added = removed = 0
    try:
        seen_paths: set[str] = set()
        for pattern in ("*.summary.md", "*.mom.md", "*.md"):
            for path in sorted(meetings_dir.glob(pattern)):
                if path.name.endswith((".summary.md", ".mom.md")):
                    session_id = path.name.rsplit(".", 2)[0]
                elif path.suffix == ".md" and not path.name.endswith((".summary.md", ".mom.md")):
                    session_id = path.stem
                else:  # pragma: no cover - pattern exhausts these
                    continue
                key = str(path)
                if key in seen_paths:
                    continue
                seen_paths.add(key)

                if _session_state(state_dir, session_id) not in _INDEXABLE_STATES:
                    continue

                mtime = path.stat().st_mtime
                row = db.execute(
                    "SELECT mtime FROM indexed_files WHERE path = ?", (key,)
                ).fetchone()
                if row is not None and row[0] == mtime:
                    continue

                # Re-embed this artefact from scratch.
                db.execute(
                    "DELETE FROM chunks WHERE artefact = ?", (key,)
                )
                text = path.read_text(encoding="utf-8", errors="replace")
                snippets = _chunk_words(text)
                if snippets:
                    vectors = list(embedder.embed(snippets))
                    for snippet, vector in zip(snippets, vectors):
                        db.execute(
                            "INSERT INTO chunks (embedding, session_id, artefact, snippet) "
                            "VALUES (?, ?, ?, ?)",
                            (_to_blob(vector), session_id, key, snippet[:500]),
                        )
                        added += 1
                db.execute(
                    "INSERT OR REPLACE INTO indexed_files (path, mtime) VALUES (?, ?)",
                    (key, mtime),
                )

        # Drop entries for files that no longer exist.
        for (key,) in db.execute("SELECT path FROM indexed_files").fetchall():
            if not Path(key).exists():
                db.execute("DELETE FROM chunks WHERE artefact = ?", (key,))
                db.execute("DELETE FROM indexed_files WHERE path = ?", (key,))
                removed += 1
        db.commit()
    finally:
        db.close()
    return {"chunks_added": added, "files_removed": removed}


def _to_blob(vector) -> bytes:
    import struct

    values = list(float(v) for v in vector)
    return struct.pack(f"{len(values)}f", *values)


def semantic_search(
    query: str,
    db_path: Path | str,
    max_results: int = 10,
    embedder=None,
) -> list[dict]:
    """Pure dense search. Returns [{session_id, snippet, distance}]."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    if embedder is None:
        embedder = _embedder()
    query_vec = next(iter(embedder.embed([query])))
    db = _open_index(db_path)
    try:
        rows = db.execute(
            "SELECT session_id, snippet, distance FROM chunks "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (_to_blob(query_vec), max_results * 3),
        ).fetchall()
    finally:
        db.close()
    # Best chunk per session only.
    best: dict[str, dict] = {}
    for session_id, snippet, distance in rows:
        if session_id not in best or distance < best[session_id]["distance"]:
            best[session_id] = {
                "session_id": session_id, "snippet": snippet, "distance": distance,
            }
    return sorted(best.values(), key=lambda r: r["distance"])[:max_results]


def hybrid_search(
    query: str,
    meetings_dir: Path | str,
    state_dir: Path | str,
    db_path: Path | str,
    max_results: int = 10,
    embedder=None,
) -> dict:
    """BM25 ∪ dense with reciprocal-rank fusion. Falls back to BM25-only when
    the semantic stack is unavailable (missing deps / model not cached)."""
    from cli.search import search_meetings

    bm25_results = search_meetings(meetings_dir, query, max_results=max_results, state_dir=Path(state_dir))
    bm25 = [{"session_id": r.session_id, "snippet": r.snippet} for r in bm25_results]

    if not Path(db_path).exists():
        dense, semantic_available = [], False
    else:
        try:
            dense = semantic_search(query, db_path, max_results=max_results, embedder=embedder)
            semantic_available = True
        except Exception as exc:  # noqa: BLE001 - degrade to lexical search, never fail the search box
            logger.warning("Semantic search unavailable (%s); returning BM25 only.", exc)
            dense, semantic_available = [], False

    scores: dict[str, float] = {}
    snippets: dict[str, str] = {}
    for rank, hit in enumerate(bm25):
        sid = hit["session_id"]
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        snippets.setdefault(sid, hit.get("snippet", ""))
    for rank, hit in enumerate(dense):
        sid = hit["session_id"]
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (_RRF_K + rank + 1)
        snippets.setdefault(sid, hit["snippet"])

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:max_results]
    return {
        "semantic_available": semantic_available,
        "results": [
            {"session_id": sid, "score": round(score, 5), "snippet": snippets.get(sid, "")}
            for sid, score in ranked
        ],
    }
