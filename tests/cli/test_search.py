"""
Tests for cli/search.py BM25 indexing/ranking logic, isolated from the web
endpoint.
"""

from __future__ import annotations

import pytest

from cli.search import SearchResult, _snippet, _tokenise, search_meetings


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

def test_tokenise_lowercases_and_splits():
    assert _tokenise("Hello, World!") == ["hello", "world"]


def test_tokenise_strips_empty_tokens():
    assert "" not in _tokenise("  foo   bar  ")


def test_tokenise_empty_string():
    assert _tokenise("") == []


# ---------------------------------------------------------------------------
# Snippet helper
# ---------------------------------------------------------------------------

def test_snippet_finds_first_occurrence_of_query_token():
    text = "The quick brown fox jumped over the lazy dog"
    snippet = _snippet(text, {"fox"}, max_len=30)
    assert "fox" in snippet.lower()


def test_snippet_falls_back_to_start_when_no_token_found():
    text = "Something completely unrelated"
    snippet = _snippet(text, {"xyz"}, max_len=20)
    assert snippet.startswith("Something")


def test_snippet_truncates_long_text():
    text = "a" * 500
    snippet = _snippet(text, {"a"}, max_len=100)
    assert len(snippet) <= 105  # +5 for ellipsis chars


# ---------------------------------------------------------------------------
# search_meetings
# ---------------------------------------------------------------------------

@pytest.fixture()
def meetings_dir(tmp_path):
    d = tmp_path / "meetings"
    d.mkdir()
    return d


def test_empty_directory_returns_no_results(meetings_dir):
    assert search_meetings(meetings_dir, "anything") == []


def test_empty_query_returns_no_results(meetings_dir):
    (meetings_dir / "s1.summary.md").write_text("This is a meeting about budgets")
    assert search_meetings(meetings_dir, "") == []
    assert search_meetings(meetings_dir, "   ") == []


def test_single_summary_found(meetings_dir):
    (meetings_dir / "s1.summary.md").write_text("Discussed quarterly budget allocations and forecasts")
    results = search_meetings(meetings_dir, "budget")
    assert len(results) == 1
    assert results[0].session_id == "s1"
    assert results[0].score > 0
    assert "budget" in results[0].snippet.lower()
    assert results[0].source == "summary"


def test_transcript_fallback_when_no_summary(meetings_dir):
    (meetings_dir / "no-summary.md").write_text("Action items: deploy the pipeline by Friday")
    results = search_meetings(meetings_dir, "deploy pipeline")
    assert len(results) == 1
    assert results[0].session_id == "no-summary"
    assert results[0].source == "transcript"


def test_ranking_prefers_higher_relevance(meetings_dir):
    (meetings_dir / "very-relevant.summary.md").write_text(
        "budget budget budget quarterly budget review budget allocation"
    )
    (meetings_dir / "slightly-relevant.summary.md").write_text(
        "we discussed the project timeline and one budget line item"
    )
    (meetings_dir / "irrelevant.summary.md").write_text(
        "team lunch and social event planning for next quarter"
    )
    results = search_meetings(meetings_dir, "budget")
    session_ids = [r.session_id for r in results]
    assert session_ids[0] == "very-relevant"
    assert "irrelevant" not in session_ids or session_ids.index("irrelevant") > session_ids.index("slightly-relevant")


def test_zero_score_results_excluded(meetings_dir):
    (meetings_dir / "relevant.summary.md").write_text("deployment pipeline CI CD")
    (meetings_dir / "unrelated.summary.md").write_text("coffee break social event")
    results = search_meetings(meetings_dir, "deployment")
    ids = [r.session_id for r in results]
    assert "relevant" in ids
    # unrelated may or may not appear depending on BM25 IDF, but if it does, score > 0
    for r in results:
        assert r.score > 0.0


def test_summary_preferred_over_transcript_for_same_session(meetings_dir):
    # When both .summary.md and .md exist for the same session, summary is used
    (meetings_dir / "mtg1.summary.md").write_text("roadmap planning meeting")
    (meetings_dir / "mtg1.md").write_text("roadmap planning meeting DIFFERENT")
    results = search_meetings(meetings_dir, "roadmap")
    mtg1_hits = [r for r in results if r.session_id == "mtg1"]
    assert len(mtg1_hits) == 1  # not double-counted
    assert mtg1_hits[0].source == "summary"


def test_max_results_respected(meetings_dir):
    for i in range(20):
        (meetings_dir / f"meeting-{i}.summary.md").write_text(f"meeting {i} about the project")
    results = search_meetings(meetings_dir, "project", max_results=5)
    assert len(results) <= 5


def test_search_endpoint_integration(tmp_path):
    """Smoke-test the /api/search endpoint via TestClient."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    import cli.web as web_module

    meetings_dir = tmp_path / "data" / "meetings"
    meetings_dir.mkdir(parents=True)
    (meetings_dir / "demo-session.summary.md").write_text("Weekly retrospective on the deployment pipeline")

    # SB-6: the endpoint passes state_dir to search_meetings, which only
    # indexes sessions in PROPOSED/REVIEWED/APPLIED. Give demo-session an
    # indexable state, otherwise it is (correctly) filtered from the corpus.
    from mcp_server.state import State, create_session

    state_dir = tmp_path / "data" / "state"
    state_dir.mkdir(parents=True)
    create_session(
        state_dir, "demo-session", state_dir / ".lock", 1.0, initial_state=State.APPLIED
    )

    class _FakeSettings:
        class paths:
            data_dir = str(tmp_path / "data")
            tmp_dir  = str(tmp_path / "tmp")
        class llm:
            host = "127.0.0.1"; port = 8080; health_check_path = "/health"
            startup_timeout_seconds = 5
        class concurrency:
            lock_path = str(tmp_path / "data" / "state" / ".lock")
            lock_timeout_seconds = 1.0
        class privacy:
            tmp_audio_ttl_seconds = 3600
        class whisper:
            device = "cpu"; compute_type = "int8"

    with patch.object(web_module, "settings", _FakeSettings()):
        with TestClient(web_module.app) as c:
            resp = c.get("/api/search?q=deployment")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["results"]) >= 1
            assert data["results"][0]["session_id"] == "demo-session"

            # Empty query returns empty list, not an error
            resp2 = c.get("/api/search?q=")
            assert resp2.status_code == 200
            assert resp2.json()["results"] == []
