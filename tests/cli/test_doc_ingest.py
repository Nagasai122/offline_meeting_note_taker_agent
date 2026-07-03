"""Tests for cli/doc_ingest.py.

Regression anchor: ingest_document() crashed with NameError at the final
write step (`atomic_write_text` used without an import) — found by the
2026-07 audit's static-analysis pass. The web upload endpoint only catches
(ValueError, NotImplementedError), so every document upload 500'd after
paying the full LLM summarisation cost. These tests exercise the write path
end-to-end with a fake LLM so that import can never silently vanish again.
"""

from __future__ import annotations

import pytest

from cli.doc_ingest import extract_text, ingest_document, summarise_doc_context


def _fake_llm(system_prompt: str, user_text: str) -> str:
    return "- bullet summary of: " + user_text[:40]


def test_ingest_document_writes_doc_context_file(tmp_path):
    doc = tmp_path / "agenda.txt"
    doc.write_text("Quarterly planning agenda. Discuss roadmap, hiring, budget.", encoding="utf-8")
    meetings_dir = tmp_path / "meetings"

    output_path = ingest_document(doc, "sess-doc-1", meetings_dir, _fake_llm)

    assert output_path == meetings_dir / "sess-doc-1.doc_context.txt"
    content = output_path.read_text(encoding="utf-8")
    assert "bullet summary" in content


def test_ingest_document_empty_document_writes_empty_context(tmp_path):
    doc = tmp_path / "empty.txt"
    doc.write_text("", encoding="utf-8")

    output_path = ingest_document(doc, "sess-doc-2", tmp_path / "meetings", _fake_llm)

    assert output_path.exists()
    assert output_path.read_text(encoding="utf-8") == ""


def test_extract_text_unsupported_extension_raises(tmp_path):
    weird = tmp_path / "notes.xyz"
    weird.write_text("hello", encoding="utf-8")
    with pytest.raises((ValueError, NotImplementedError)):
        extract_text(weird)


def test_summarise_doc_context_truncates_on_line_boundary():
    long_text = "word " * 5000
    calls = []

    def llm(system_prompt: str, user_text: str) -> str:
        calls.append(user_text)
        return "- line one\n- line two\n- line three"

    result = summarise_doc_context(long_text, llm, max_output_tokens=5)
    # 5 tokens * 4 chars = 20-char cap: must cut on a bullet boundary, not mid-line.
    for line in result.splitlines():
        assert line.startswith("- ")
    assert len(calls) >= 1
