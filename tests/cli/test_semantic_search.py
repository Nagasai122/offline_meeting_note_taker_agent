"""Tests for cli/semantic_search.py using a deterministic fake embedder —
no model download, no network, runs anywhere sqlite-vec loads."""

from __future__ import annotations

import math

import pytest

pytest.importorskip("sqlite_vec")

from cli.semantic_search import hybrid_search, refresh_index, semantic_search
from mcp_server.state import State, create_session

_DIM = 384

_TOPICS = {
    "caching": 0, "redis": 0, "migration": 0,      # same topic axis
    "budget": 1, "finance": 1, "variance": 1,       # another axis
    "hiring": 2, "interview": 2,
}


class FakeEmbedder:
    """Maps text onto a few topic axes so semantically-related words land on
    the same unit vector — a tiny, deterministic stand-in for bge-small."""

    def embed(self, texts):
        for text in texts:
            vec = [0.0] * _DIM
            for word, axis in _TOPICS.items():
                if word in text.lower():
                    vec[axis] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            yield [v / norm for v in vec]


def _seed(tmp_path, session_id: str, state: State, summary: str):
    meetings = tmp_path / "meetings"
    meetings.mkdir(exist_ok=True)
    (meetings / f"{session_id}.summary.md").write_text(summary, encoding="utf-8")
    create_session(tmp_path / "state", session_id, tmp_path / ".lock", 1.0, initial_state=state)


def test_index_and_semantic_search_match_paraphrase(tmp_path):
    _seed(tmp_path, "arch-20260701-100000", State.APPLIED,
          "- Team agreed the caching layer needs work; redis discussed at length.")
    _seed(tmp_path, "fin-20260701-110000", State.APPLIED,
          "- Budget variance reviewed with finance.")
    db = tmp_path / "index.db"

    stats = refresh_index(tmp_path / "meetings", tmp_path / "state", db, embedder=FakeEmbedder())
    assert stats["chunks_added"] == 2

    hits = semantic_search("redis migration plan", db, embedder=FakeEmbedder())
    assert hits, "expected a semantic hit"
    assert hits[0]["session_id"] == "arch-20260701-100000"


def test_unreviewed_sessions_are_not_indexed(tmp_path):
    _seed(tmp_path, "raw-20260701-100000", State.TRANSCRIBED, "- caching secrets")
    db = tmp_path / "index.db"

    stats = refresh_index(tmp_path / "meetings", tmp_path / "state", db, embedder=FakeEmbedder())

    assert stats["chunks_added"] == 0
    assert semantic_search("caching", db, embedder=FakeEmbedder()) == []


def test_refresh_is_incremental_and_removes_deleted_files(tmp_path):
    _seed(tmp_path, "arch-20260701-100000", State.APPLIED, "- caching layer notes")
    db = tmp_path / "index.db"
    first = refresh_index(tmp_path / "meetings", tmp_path / "state", db, embedder=FakeEmbedder())
    second = refresh_index(tmp_path / "meetings", tmp_path / "state", db, embedder=FakeEmbedder())
    assert first["chunks_added"] == 1
    assert second["chunks_added"] == 0  # unchanged mtime -> untouched

    (tmp_path / "meetings" / "arch-20260701-100000.summary.md").unlink()
    third = refresh_index(tmp_path / "meetings", tmp_path / "state", db, embedder=FakeEmbedder())
    assert third["files_removed"] == 1
    assert semantic_search("caching", db, embedder=FakeEmbedder()) == []


def test_hybrid_search_fuses_lexical_and_dense(tmp_path):
    _seed(tmp_path, "arch-20260701-100000", State.APPLIED,
          "- caching layer redis eviction policy discussion")
    _seed(tmp_path, "hr-20260701-110000", State.APPLIED,
          "- hiring pipeline and interview feedback")
    db = tmp_path / "index.db"
    refresh_index(tmp_path / "meetings", tmp_path / "state", db, embedder=FakeEmbedder())

    out = hybrid_search("redis migration", tmp_path / "meetings", tmp_path / "state", db,
                        embedder=FakeEmbedder())

    assert out["semantic_available"] is True
    assert out["results"][0]["session_id"] == "arch-20260701-100000"


def test_hybrid_falls_back_to_bm25_when_semantic_unavailable(tmp_path):
    _seed(tmp_path, "arch-20260701-100000", State.APPLIED, "- redis eviction policy")

    class BrokenEmbedder:
        def embed(self, texts):
            raise RuntimeError("model not cached")

    out = hybrid_search("redis", tmp_path / "meetings", tmp_path / "state",
                        tmp_path / "missing.db", embedder=BrokenEmbedder())

    assert out["semantic_available"] is False
    assert out["results"] and out["results"][0]["session_id"] == "arch-20260701-100000"
