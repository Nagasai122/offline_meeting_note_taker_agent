"""Tests for cli/mail_import.py (drag-and-drop .eml/.msg email context)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cli.mail_import import (
    MailParseError,
    format_mail_context,
    parse_eml_bytes,
    parse_mail_file,
)

PLAIN_EML = b"""\
From: Naga <naga@example.com>
To: Team <team@example.com>
Subject: Weekly budget review agenda
Date: Thu, 02 Jul 2026 10:00:00 +0000
Content-Type: text/plain; charset="utf-8"

Agenda:
1. Variance analysis sign-off
2. Q3 headcount \xc2\xb1 two roles
"""

HTML_EML = b"""\
From: Sender <s@example.com>
Subject: HTML only mail
Date: Thu, 02 Jul 2026 11:00:00 +0000
Content-Type: text/html; charset="utf-8"

<html><body><p>Please review the <b>vendor matrix</b> before Monday.</p></body></html>
"""


def test_parse_plain_eml():
    parsed = parse_eml_bytes(PLAIN_EML)
    assert parsed["subject"] == "Weekly budget review agenda"
    assert "Variance analysis" in parsed["body"]
    assert "± two roles" in parsed["body"]  # UTF-8 survives
    assert "naga@example.com" in parsed["sender"]


def test_parse_html_only_eml_strips_tags():
    parsed = parse_eml_bytes(HTML_EML)
    assert "vendor matrix" in parsed["body"]
    assert "<b>" not in parsed["body"]


def test_parse_garbage_raises_mail_parse_error():
    with pytest.raises(MailParseError):
        parse_eml_bytes(b"\x00\x01\x02 not an email at all")


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class _FakeExtractedMsg:
    """Stand-in for extract_msg's MSGFile: constructing a real OLE-compound
    .msg fixture is impractical, so tests patch extract_msg.openMsg and only
    exercise our routing + field-mapping around it."""

    subject = "Quarterly sync"
    sender = "Naga <naga@example.com>"
    date = "2026-07-02 10:00"
    body = "Agenda: review the risk register."
    htmlBody = None

    def close(self):
        pass


def test_extract_msg_is_installed():
    # extract-msg is a core dependency now (pyproject [project.dependencies]),
    # not an optional extra -- .msg parsing must never hit the ImportError path.
    import extract_msg  # noqa: F401


def test_parse_msg_file_maps_extract_msg_fields(tmp_path):
    p = tmp_path / "mail.msg"
    p.write_bytes(OLE_MAGIC + b"\x00" * 32)
    with patch("extract_msg.openMsg", return_value=_FakeExtractedMsg()) as open_msg:
        parsed = parse_mail_file(p)
    open_msg.assert_called_once_with(str(p))
    assert parsed["subject"] == "Quarterly sync"
    assert "risk register" in parsed["body"]
    assert "naga@example.com" in parsed["sender"]


def test_parse_mail_file_unknown_suffix_sniffs_eml_content(tmp_path):
    # A .txt (or any unknown) suffix no longer hard-fails: content sniffing
    # recognises the RFC-822 headers and parses it as .eml.
    p = tmp_path / "mail.txt"
    p.write_bytes(PLAIN_EML)
    parsed = parse_mail_file(p)
    assert parsed["subject"] == "Weekly budget review agenda"


def test_parse_mail_file_extensionless_eml_sniffs(tmp_path):
    p = tmp_path / "message"  # no suffix at all
    p.write_bytes(PLAIN_EML)
    parsed = parse_mail_file(p)
    assert parsed["subject"] == "Weekly budget review agenda"


def test_parse_mail_file_extensionless_ole_routes_to_msg_parser(tmp_path):
    # The marker below (the real .msg property-stream name prefix, UTF-16LE
    # encoded) is what distinguishes a real .msg from a legacy .doc/.xls/.ppt
    # that merely shares the bare OLE magic number -- see
    # test_parse_mail_file_ole_without_msg_marker_is_rejected below.
    p = tmp_path / "message"
    p.write_bytes(OLE_MAGIC + b"\x00" * 32 + "__substg1.0_".encode("utf-16-le") + b"\x00" * 32)
    with patch("extract_msg.openMsg", return_value=_FakeExtractedMsg()) as open_msg:
        parsed = parse_mail_file(p)
    open_msg.assert_called_once_with(str(p))
    assert parsed["subject"] == "Quarterly sync"


def test_parse_mail_file_ole_without_msg_marker_is_rejected(tmp_path):
    # A legacy .doc/.xls/.ppt (also an OLE compound file) dropped with no
    # extension must NOT be misrouted into the .msg parser just because it
    # shares the bare container magic number.
    p = tmp_path / "message"
    p.write_bytes(OLE_MAGIC + b"\x00" * 64)
    with pytest.raises(MailParseError, match="Not a recognisable email file"):
        parse_mail_file(p)


def test_parse_mail_file_unknown_suffix_garbage_raises(tmp_path):
    p = tmp_path / "blob"
    p.write_bytes(b"\x00\x01\x02\x03 nothing email-like here")
    with pytest.raises(MailParseError, match="Not a recognisable email file"):
        parse_mail_file(p)


def test_format_mail_context_includes_headers_and_body():
    text = format_mail_context(parse_eml_bytes(PLAIN_EML))
    assert text.startswith("Subject: Weekly budget review agenda")
    assert "From: Naga" in text
    assert "Variance analysis" in text


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    import cli.web as web_module

    (tmp_path / "data" / "state").mkdir(parents=True)

    class _FakeSettings:
        class paths:
            data_dir = str(tmp_path / "data")
            tmp_dir = str(tmp_path / "tmp")
        class llm:
            host = "127.0.0.1"; port = 8080; health_check_path = "/health"; startup_timeout_seconds = 5
        class concurrency:
            lock_path = str(tmp_path / "data" / "state" / ".lock"); lock_timeout_seconds = 1.0
        class privacy:
            tmp_audio_ttl_seconds = 3600
        class whisper:
            device = "cpu"; compute_type = "int8"

    with patch.object(web_module, "settings", _FakeSettings()):
        with TestClient(web_module.app, raise_server_exceptions=True) as c:
            yield c, tmp_path


def test_upload_eml_without_session_returns_parsed_body(client):
    c, _ = client
    resp = c.post(
        "/api/context/mail-file",
        files={"file": ("agenda.eml", PLAIN_EML, "message/rfc822")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "parsed"
    assert data["subject"] == "Weekly budget review agenda"
    assert "Variance analysis" in data["body"]


def test_upload_eml_with_session_persists_mail_context(client):
    c, tmp_path = client
    resp = c.post(
        "/api/context/mail-file",
        files={"file": ("agenda.eml", PLAIN_EML, "message/rfc822")},
        data={"session_id": "mail-drop-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "saved"
    saved = tmp_path / "data" / "meetings" / "mail-drop-1.mail_context.txt"
    assert saved.exists()
    assert "Weekly budget review agenda" in saved.read_text(encoding="utf-8")


def test_upload_unsupported_suffix_unrecognisable_content_400(client):
    # Suffix alone no longer rejects the upload; the content sniff runs and
    # rejects bytes that are neither OLE (.msg) nor RFC-822 (.eml).
    c, _ = client
    resp = c.post(
        "/api/context/mail-file",
        files={"file": ("mail.pdf", b"%PDF-", "application/pdf")},
    )
    assert resp.status_code == 400
    assert "Not a recognisable email file" in resp.json()["error"]


def test_upload_extensionless_eml_content_returns_parsed(client):
    c, _ = client
    resp = c.post(
        "/api/context/mail-file",
        files={"file": ("message", PLAIN_EML, "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "parsed"
    assert data["subject"] == "Weekly budget review agenda"


def test_upload_extensionless_garbage_400(client):
    c, _ = client
    resp = c.post(
        "/api/context/mail-file",
        files={"file": ("blob", b"\x00\x01\x02\x03 garbage bytes", "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_upload_garbage_eml_400_not_500(client):
    c, _ = client
    resp = c.post(
        "/api/context/mail-file",
        files={"file": ("bad.eml", b"\x00\x01\x02", "message/rfc822")},
    )
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_upload_path_traversal_session_id_422(client):
    c, _ = client
    resp = c.post(
        "/api/context/mail-file",
        files={"file": ("ok.eml", PLAIN_EML, "message/rfc822")},
        data={"session_id": "../../evil"},
    )
    assert resp.status_code == 422
