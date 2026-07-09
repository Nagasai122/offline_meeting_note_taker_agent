"""
Deterministic email-context import: parse a dragged-and-dropped .eml / .msg
file into the same plain-text context block `cli/mail_sync.py`'s fuzzy
Outlook-COM matcher produces.

Why this exists: the COM matcher is best-effort and opaque (subject-token
overlap in a ±24h window) — useful when it hits, baffling when it doesn't.
Dropping the actual email is explicit and always right. "New" Outlook and
most desktop clients drag messages out as real .eml files; classic Outlook
drags produce .msg (OLE compound) files, parsed here via the `extract-msg`
dependency (a core install requirement). Files with an unknown or missing
extension are content-sniffed (OLE magic → .msg, RFC-822 headers → .eml)
before being rejected.

Zero-egress: pure local file parsing (stdlib `email` for RFC-822 .eml;
extract-msg reads the OLE container locally). No network, no COM.
"""

from __future__ import annotations

import email
import email.policy
import re
from pathlib import Path

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MAX_BODY_CHARS = 4000


class MailParseError(ValueError):
    """Raised when an .eml/.msg file cannot be parsed into usable context."""


def _clean_body(body: str, was_html: bool) -> str:
    if was_html:
        body = _HTML_TAG_RE.sub(" ", body)
    # Collapse the whitespace storms both HTML stripping and Outlook
    # plain-text exports produce, but keep paragraph breaks readable.
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()[:_MAX_BODY_CHARS]


def parse_eml_bytes(data: bytes) -> dict:
    """Parse an RFC-822 .eml file into {subject, sender, date, body}."""
    try:
        msg = email.message_from_bytes(data, policy=email.policy.default)
    except Exception as exc:  # email lib raises varied types on garbage
        raise MailParseError(f"Not a parsable .eml file: {exc}") from exc

    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        body, was_html = "", False
    else:
        was_html = body_part.get_content_type() == "text/html"
        try:
            body = body_part.get_content()
        except Exception as exc:
            raise MailParseError(f"Could not decode the email body: {exc}") from exc

    parsed = {
        "subject": str(msg.get("Subject", "") or "").strip(),
        "sender": str(msg.get("From", "") or "").strip(),
        "date": str(msg.get("Date", "") or "").strip(),
        "body": _clean_body(body, was_html),
    }
    # The stdlib parser is lenient enough to "parse" arbitrary bytes as a
    # headerless message; require at least one real RFC-822 header before
    # accepting the file as an email.
    if not (parsed["subject"] or parsed["sender"] or parsed["date"]):
        raise MailParseError(
            "This file has no Subject/From/Date headers — it does not look "
            "like an exported email (.eml)."
        )
    if not parsed["subject"] and not parsed["body"]:
        raise MailParseError("The .eml file contains neither a subject nor a body.")
    return parsed


def parse_msg_file(path: Path) -> dict:
    """Parse a classic-Outlook .msg (OLE compound) file. Needs `extract-msg`."""
    try:
        import extract_msg
    except ImportError as exc:
        raise MailParseError(
            "Parsing .msg files requires the optional 'extract-msg' package "
            "(pip install extract-msg), or save the mail as .eml instead."
        ) from exc

    try:
        msg = extract_msg.openMsg(str(path))
        try:
            body = msg.body or ""
            was_html = False
            if not body.strip() and getattr(msg, "htmlBody", None):
                raw_html = msg.htmlBody
                body = raw_html.decode("utf-8", errors="replace") if isinstance(raw_html, bytes) else str(raw_html)
                was_html = True
            parsed = {
                "subject": (msg.subject or "").strip(),
                "sender": (msg.sender or "").strip(),
                "date": str(msg.date or "").strip(),
                "body": _clean_body(body, was_html),
            }
        finally:
            msg.close()
    except MailParseError:
        raise
    except Exception as exc:
        raise MailParseError(f"Not a parsable .msg file: {exc}") from exc

    if not parsed["subject"] and not parsed["body"]:
        raise MailParseError("The .msg file contains neither a subject nor a body.")
    return parsed


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# The bare OLE/CFBF magic number above is shared by legacy .doc/.xls/.ppt/.msi
# files, not just Outlook .msg -- "__substg1.0_" is the MAPI property-stream
# name prefix that only appears inside a real .msg's compound-file directory,
# so requiring it before committing to the "msg" classification is what keeps
# a stray legacy Office file from being misrouted into extract-msg.
_MSG_STREAM_MARKER = "__substg1.0_".encode("utf-16-le")

# RFC-822 header lines near the top of the file are the .eml fingerprint.
_RFC822_HEADER_RE = re.compile(
    r"^(From|To|Subject|Received|MIME-Version|Date|Return-Path|Message-ID)\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def _sniff_mail_kind(data: bytes) -> str | None:
    """Guess whether raw bytes are a .msg (OLE compound) or .eml (RFC-822)
    email, for files dropped without a recognisable extension. Returns
    "msg", "eml", or None."""
    if data.startswith(_OLE_MAGIC):
        return "msg" if _MSG_STREAM_MARKER in data else None
    head = data[:2048]
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = head.decode("latin-1")
        except UnicodeDecodeError:
            return None
        # latin-1 decodes anything; reject if it looks binary.
        if "\x00" in text:
            return None
    if _RFC822_HEADER_RE.search(text):
        return "eml"
    return None


def parse_mail_file(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix == ".eml":
        return parse_eml_bytes(path.read_bytes())
    if suffix == ".msg":
        return parse_msg_file(path)
    # Unknown/missing extension: sniff the content so drag-and-drop sources
    # that strip the extension (some mail clients, browser downloads) still
    # work when the bytes are genuinely one of the supported formats.
    data = path.read_bytes()
    kind = _sniff_mail_kind(data)
    if kind == "msg":
        return parse_msg_file(path)
    if kind == "eml":
        return parse_eml_bytes(data)
    raise MailParseError(
        "Not a recognisable email file. Accepted: .eml, .msg "
        "(or a file whose content is one of those)."
    )


def format_mail_context(parsed: dict) -> str:
    """Render the parsed mail as the context block stored in
    `<session_id>.mail_context.txt` / appended to the agenda notes."""
    header = [f"Subject: {parsed['subject']}" if parsed.get("subject") else None,
              f"From: {parsed['sender']}" if parsed.get("sender") else None,
              f"Date: {parsed['date']}" if parsed.get("date") else None]
    lines = [line for line in header if line]
    if parsed.get("body"):
        lines += ["", parsed["body"]]
    return "\n".join(lines)
