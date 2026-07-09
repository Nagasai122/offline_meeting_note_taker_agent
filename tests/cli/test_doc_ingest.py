"""Tests for cli/doc_ingest.py.

Regression anchor: ingest_document() crashed with NameError at the final
write step (`atomic_write_text` used without an import) — found by the
2026-07 audit's static-analysis pass. The web upload endpoint only catches
(ValueError, NotImplementedError), so every document upload 500'd after
paying the full LLM summarisation cost. These tests exercise the write path
end-to-end with a fake LLM so that import can never silently vanish again.

P3: ingest_document was renamed add_document_context (+ new
add_pasted_text_context) and changed from overwrite to append-with-lock
semantics, since context can now be added throughout a live recording, not
just once pre-meeting.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cli.doc_ingest import (
    add_document_context,
    add_pasted_text_context,
    extract_text,
    extract_text_from_image,
    extract_text_from_xlsx,
    is_ocr_available,
    summarise_doc_context,
)


def _fake_llm(system_prompt: str, user_text: str) -> str:
    return "- bullet summary of: " + user_text[:40]


def _lock(tmp_path):
    return tmp_path / ".lock", 5.0


def test_add_document_context_writes_doc_context_file(tmp_path):
    doc = tmp_path / "agenda.txt"
    # Long text is what actually triggers summarisation via _fake_llm below --
    # a document under the 60-word threshold is appended verbatim instead
    # (see test_short_document_is_appended_verbatim).
    doc.write_text("Quarterly planning agenda. " * 20, encoding="utf-8")
    meetings_dir = tmp_path / "meetings"
    lock_path, lock_timeout = _lock(tmp_path)

    output_path = add_document_context(doc, "sess-doc-1", meetings_dir, _fake_llm, lock_path, lock_timeout)

    assert output_path == meetings_dir / "sess-doc-1.doc_context.txt"
    content = output_path.read_text(encoding="utf-8")
    assert "bullet summary" in content
    assert "Document: agenda.txt" in content


def test_add_document_context_uses_explicit_source_label_over_path_name(tmp_path):
    """Regression: cli/web.py stages an upload under a prefixed tmp filename
    (`<session_id>_doc_<original name>`) to avoid collisions -- without an
    explicit source_label override, the label leaked that tmp-staging naming
    scheme (e.g. "sess-b_doc_agenda.txt") into the extraction prompt instead
    of the filename the user actually uploaded."""
    tmp_staged = tmp_path / "sess-b_doc_agenda.txt"
    tmp_staged.write_text("Short agenda note.", encoding="utf-8")
    meetings_dir = tmp_path / "meetings"
    lock_path, lock_timeout = _lock(tmp_path)

    output_path = add_document_context(
        tmp_staged, "sess-b", meetings_dir, _fake_llm, lock_path, lock_timeout, source_label="agenda.txt",
    )

    content = output_path.read_text(encoding="utf-8")
    assert "Document: agenda.txt" in content
    assert "sess-b_doc_agenda.txt" not in content


def test_add_document_context_empty_document_writes_nothing(tmp_path):
    doc = tmp_path / "empty.txt"
    doc.write_text("", encoding="utf-8")
    lock_path, lock_timeout = _lock(tmp_path)

    output_path = add_document_context(doc, "sess-doc-2", tmp_path / "meetings", _fake_llm, lock_path, lock_timeout)

    # An empty document contributes no labelled section -- nothing worth
    # writing, and no file created (same as if nothing had ever been
    # uploaded; extraction.py already treats a missing file as "no context").
    assert not output_path.exists()


def test_short_document_is_appended_verbatim_without_llm_call(tmp_path):
    doc = tmp_path / "short.txt"
    doc.write_text("Just a short note, no summarisation needed.", encoding="utf-8")
    meetings_dir = tmp_path / "meetings"
    lock_path, lock_timeout = _lock(tmp_path)
    calls = []

    def llm(system_prompt: str, user_text: str) -> str:
        calls.append(user_text)
        return "SHOULD NOT BE CALLED"

    output_path = add_document_context(doc, "sess-doc-3", meetings_dir, llm, lock_path, lock_timeout)

    content = output_path.read_text(encoding="utf-8")
    assert "Just a short note, no summarisation needed." in content
    assert not calls


def test_second_attachment_appends_rather_than_overwrites(tmp_path):
    meetings_dir = tmp_path / "meetings"
    lock_path, lock_timeout = _lock(tmp_path)
    doc1 = tmp_path / "first.txt"
    doc1.write_text("First attachment note.", encoding="utf-8")
    doc2 = tmp_path / "second.txt"
    doc2.write_text("Second attachment note.", encoding="utf-8")

    add_document_context(doc1, "sess-doc-4", meetings_dir, _fake_llm, lock_path, lock_timeout)
    output_path = add_document_context(doc2, "sess-doc-4", meetings_dir, _fake_llm, lock_path, lock_timeout)

    content = output_path.read_text(encoding="utf-8")
    assert "first.txt" in content
    assert "second.txt" in content
    assert "First attachment note." in content
    assert "Second attachment note." in content


def test_add_pasted_text_context_appends_labelled(tmp_path):
    meetings_dir = tmp_path / "meetings"
    lock_path, lock_timeout = _lock(tmp_path)

    output_path = add_pasted_text_context(
        "Quick note pasted from a Teams chat.", "sess-doc-5", meetings_dir, _fake_llm, lock_path, lock_timeout,
    )

    content = output_path.read_text(encoding="utf-8")
    assert "Pasted text" in content
    assert "Quick note pasted from a Teams chat." in content


def test_combined_context_over_cap_triggers_compress(tmp_path):
    """P3.8: once the accumulated file exceeds the combined char cap, the
    whole thing is re-summarised into one consolidated block instead of
    growing unbounded."""
    from cli import doc_ingest

    meetings_dir = tmp_path / "meetings"
    lock_path, lock_timeout = _lock(tmp_path)
    session_id = "sess-doc-6"
    output_path = meetings_dir / f"{session_id}.doc_context.txt"
    meetings_dir.mkdir(parents=True)
    # Seed the file already past the cap so the next append triggers compress.
    output_path.write_text("x" * (doc_ingest._COMBINED_CONTEXT_CHAR_CAP + 100), encoding="utf-8")

    compress_calls = []

    def llm(system_prompt: str, user_text: str) -> str:
        compress_calls.append(user_text)
        return "- consolidated summary"

    add_pasted_text_context("New note that pushes it over the cap.", session_id, meetings_dir, llm, lock_path, lock_timeout)

    content = output_path.read_text(encoding="utf-8")
    assert "consolidated summary" in content
    assert compress_calls


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


# ---------------------------------------------------------------------------
# P3.5: .xlsx support
# ---------------------------------------------------------------------------

def test_extract_text_from_xlsx_reads_sheet_rows(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["Item", "Cost"])
    ws.append(["Venue", 500])
    ws.append([None, None])  # blank row -- must be skipped
    ws.append(["Catering", 250])
    path = tmp_path / "budget.xlsx"
    wb.save(path)

    text = extract_text_from_xlsx(path)

    assert "[Sheet: Budget]" in text
    assert "Item\tCost" in text
    assert "Venue\t500" in text
    assert "Catering\t250" in text
    # Sheet header + 3 non-blank rows -- the blank row must not appear as an
    # empty line in between.
    assert text.splitlines() == ["[Sheet: Budget]", "Item\tCost", "Venue\t500", "Catering\t250"]


def test_extract_text_dispatches_xlsx(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.append(["hello", "world"])
    path = tmp_path / "notes.xlsx"
    wb.save(path)

    text = extract_text(path)

    assert "hello" in text and "world" in text


# ---------------------------------------------------------------------------
# P3.6: local OCR via pytesseract
# ---------------------------------------------------------------------------

def test_extract_text_from_image_happy_path(tmp_path):
    from PIL import Image

    img_path = tmp_path / "screenshot.png"
    Image.new("RGB", (10, 10), color="white").save(img_path)

    with patch("pytesseract.image_to_string", return_value="Extracted OCR text") as mock_ocr:
        text = extract_text_from_image(img_path)

    assert text == "Extracted OCR text"
    assert mock_ocr.called


def test_extract_text_from_image_missing_tesseract_raises_clear_runtime_error(tmp_path):
    import pytesseract as pt
    from PIL import Image

    img_path = tmp_path / "screenshot.png"
    Image.new("RGB", (10, 10), color="white").save(img_path)

    with patch("pytesseract.image_to_string", side_effect=pt.TesseractNotFoundError()):
        with pytest.raises(RuntimeError, match="OCR unavailable"):
            extract_text_from_image(img_path)


def test_extract_text_dispatches_image_suffixes(tmp_path):
    from PIL import Image

    for suffix in (".png", ".jpg", ".jpeg"):
        img_path = tmp_path / f"shot{suffix}"
        Image.new("RGB", (10, 10), color="white").save(img_path)
        with patch("pytesseract.image_to_string", return_value="ocr text"):
            assert extract_text(img_path) == "ocr text"


def test_is_ocr_available_true_when_tesseract_responds():
    from cli import doc_ingest

    doc_ingest._ocr_available_cache = None
    try:
        with patch("pytesseract.get_tesseract_version", return_value="5.0.0"):
            assert is_ocr_available() is True
    finally:
        doc_ingest._ocr_available_cache = None


def test_is_ocr_available_false_when_tesseract_missing():
    from cli import doc_ingest

    doc_ingest._ocr_available_cache = None
    try:
        with patch("pytesseract.get_tesseract_version", side_effect=EnvironmentError("not found")):
            assert is_ocr_available() is False
    finally:
        doc_ingest._ocr_available_cache = None


def test_is_ocr_available_is_cached_after_first_call():
    from cli import doc_ingest

    doc_ingest._ocr_available_cache = None
    try:
        with patch("pytesseract.get_tesseract_version", return_value="5.0.0") as mock_version:
            assert is_ocr_available() is True
            assert is_ocr_available() is True
            assert mock_version.call_count == 1
    finally:
        doc_ingest._ocr_available_cache = None
