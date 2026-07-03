"""Tests for cli/mail_sync.py's COM-free logic.

Regression anchor: save_mail_context() crashed with NameError
(`atomic_write_text` used without an import) — found by the 2026-07 audit.
The Outlook COM fetch itself is not testable off-Windows/off-Outlook and is
best-effort by design; the persistence step is plain file I/O and is covered
here.
"""

from __future__ import annotations

from cli.mail_sync import _tokenise_hint, save_mail_context


def test_save_mail_context_writes_file(tmp_path):
    output_path = save_mail_context("sess-mail-1", tmp_path / "meetings", "Mail body text.")
    assert output_path == tmp_path / "meetings" / "sess-mail-1.mail_context.txt"
    assert output_path.read_text(encoding="utf-8") == "Mail body text."


def test_save_mail_context_creates_meetings_dir(tmp_path):
    missing_dir = tmp_path / "not" / "yet" / "created"
    output_path = save_mail_context("sess-mail-2", missing_dir, "body")
    assert output_path.exists()


def test_tokenise_hint_drops_short_tokens_and_lowercases():
    assert _tokenise_hint("IS Call weekly-Budget_Review x") == ["call", "weekly", "budget", "review"]
