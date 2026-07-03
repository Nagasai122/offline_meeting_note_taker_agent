"""
Offline document context ingestion: PDF/PPTX/DOCX/TXT -> extracted text ->
LLM-summarised context (bounded to ~1000 tokens) -> `.doc_context.txt`.

All extraction libraries (pdfplumber, python-pptx, python-docx) are pure
Python and read local files only -- no network calls, consistent with the
zero-egress guarantee the rest of this project is built around. The 1000-
token cap on the final summary is a deliberate trade-off (architecture_v2.md
§7.2): complete coverage of e.g. a 200-slide deck is worth less than keeping
the extraction context window clear for the actual transcript.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from concurrency.atomic import atomic_write_text

DOC_SUMMARY_SYSTEM_PROMPT = (
    "Summarise the following section of a meeting document in 3-5 bullet points. "
    "Focus on key arguments, data, decisions, and terminology."
)

_MAX_OUTPUT_CHARS_PER_TOKEN = 4  # rough token->char approximation for the truncation cap


def extract_text_from_pdf(path: Path) -> str:
    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:  # pdfplumber raises varied exception types for encrypted PDFs
        if "password" in str(exc).lower() or "encrypt" in str(exc).lower():
            raise ValueError("PDF is encrypted") from exc
        raise
    return "\n\n".join(pages)


def extract_text_from_pptx(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    slides_text = []
    for slide in prs.slides:
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text
                if text.strip():
                    parts.append(text)
        slides_text.append("\n".join(parts))
    return "\n\n---\n\n".join(slides_text)


def extract_text_from_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    return "\n".join(p.text for p in document.paragraphs)


def extract_text(path: Path) -> str:
    """Dispatch to the right extractor based on file suffix.

    Args:
        path: Path to the uploaded document.

    Returns:
        Raw extracted text.

    Raises:
        NotImplementedError: for legacy .ppt/.doc binary formats.
        ValueError: for any other unsupported extension.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    if suffix == ".pptx":
        return extract_text_from_pptx(path)
    if suffix == ".ppt":
        raise NotImplementedError("Legacy .ppt is not supported; please save as .pptx.")
    if suffix == ".docx":
        return extract_text_from_docx(path)
    if suffix == ".doc":
        raise NotImplementedError("Legacy .doc is not supported; please save as .docx.")
    if suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Unsupported document extension: {suffix!r}")


def _chunk_text(text: str, chunk_tokens: int = 2000) -> list[str]:
    """Split text into ~chunk_tokens-sized pieces by word count (reuses the
    same words*1.35 approximation as transcribe/chunker.py, applied to plain
    prose rather than transcript segments)."""
    words = text.split()
    if not words:
        return []
    words_per_chunk = max(1, int(chunk_tokens / 1.35))
    return [
        " ".join(words[i : i + words_per_chunk])
        for i in range(0, len(words), words_per_chunk)
    ]


def summarise_doc_context(
    raw_text: str,
    llm_call: Callable[[str, str], str],
    max_output_tokens: int = 1000,
) -> str:
    """Summarise `raw_text` in ~2000-token chunks, then cap total output.

    Args:
        raw_text: Extracted document text.
        llm_call: Callable(system_prompt, user_text) -> response text.
        max_output_tokens: Approximate token budget for the final summary.

    Returns:
        Concatenated bullet-point summary, truncated (on a bullet boundary)
        to roughly `max_output_tokens`.
    """
    chunks = _chunk_text(raw_text, chunk_tokens=2000)
    if not chunks:
        return ""

    summaries = [llm_call(DOC_SUMMARY_SYSTEM_PROMPT, chunk) for chunk in chunks]
    combined = "\n".join(summaries)

    max_chars = max_output_tokens * _MAX_OUTPUT_CHARS_PER_TOKEN
    if len(combined) <= max_chars:
        return combined

    # Truncate on a bullet-point boundary rather than mid-line.
    lines = combined.splitlines()
    kept: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > max_chars:
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(kept)


def ingest_document(
    path: Path,
    session_id: str,
    meetings_dir: Path,
    llm_call: Callable[[str, str], str],
) -> Path:
    """Full pipeline: extract -> summarise -> write `<session_id>.doc_context.txt`.

    Args:
        path: Path to the uploaded document (PDF/PPTX/DOCX/TXT).
        session_id: Session this context belongs to.
        meetings_dir: Directory the `.doc_context.txt` artefact is written into.
        llm_call: Callable(system_prompt, user_text) -> response text.

    Returns:
        Path to the written `.doc_context.txt` file.
    """
    raw_text = extract_text(path)
    summary = summarise_doc_context(raw_text, llm_call)
    meetings_dir = Path(meetings_dir)
    meetings_dir.mkdir(parents=True, exist_ok=True)
    output_path = meetings_dir / f"{session_id}.doc_context.txt"
    atomic_write_text(output_path, summary)
    return output_path
