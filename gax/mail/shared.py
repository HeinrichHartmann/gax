"""Shared data types, format helpers, and Gmail API functions for mail.

Used by both thread.py and mailbox.py.
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import html2text as _html2text  # type: ignore[import-untyped]  # hard dep since pyproject.toml


from ..auth import get_service
from ..store import store_blob
from .. import gaxfile
from ..syncstate import write_sync_header

logger = logging.getLogger(__name__)


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class Attachment:
    """Email attachment metadata."""

    name: str
    size: int
    mime_type: str
    url: str  # file:// URL to CAS blob


@dataclass
class Message:
    """A single email message."""

    message_id: str
    thread_id: str
    from_addr: str
    to_addr: str
    subject: str
    date: str  # ISO format
    body: str
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class MailSection:
    """A section of a multipart mail document."""

    title: str
    source: str
    time: str
    thread_id: str
    section: int
    section_title: str
    from_addr: str
    to_addr: str
    date: str
    content: str
    attachments: list[Attachment] = field(default_factory=list)
    message_id: str = ""  # RFC 2822 Message-ID header value
    id: str = ""  # Gmail message hex ID (API 'id' field)
    reply_to: str = ""  # Reply-To header (RFC-correct reply target)
    references: str = ""  # References header (ancestor Message-IDs)
    history_id: str = ""  # Gmail historyId (first section only)


# =============================================================================
# Multipart format helpers
# =============================================================================


def _mail_section_to_multipart(section: MailSection) -> gaxfile.Section:
    """Convert MailSection to generic multipart Section."""
    headers = {
        "type": "gax/mail",
        "title": section.title,
        "source": section.source,
        "time": section.time,
        "thread_id": section.thread_id,
        "section": section.section,
        "section_title": section.section_title,
        "from": section.from_addr,
        "to": section.to_addr,
        "date": section.date,
    }
    if section.id:
        headers["id"] = section.id
    if section.message_id:
        headers["message_id"] = section.message_id
    if section.reply_to:
        headers["reply_to"] = section.reply_to
    if section.references:
        headers["references"] = section.references
    if section.attachments:
        headers["attachments"] = [
            {"name": att.name, "size": att.size, "url": att.url}
            for att in section.attachments
        ]
    if section.section == 1:
        headers = write_sync_header(headers, rev=section.history_id)
    return gaxfile.Section(headers=headers, content=section.content)


def format_section(section: MailSection) -> str:
    """Format a single section as YAML header + markdown body."""
    mp_section = _mail_section_to_multipart(section)
    return gaxfile.format_section(mp_section.headers, mp_section.content)


def format_multipart(sections: list[MailSection]) -> str:
    """Assemble sections into multipart markdown string."""
    mp_sections = [_mail_section_to_multipart(s) for s in sections]
    return gaxfile.format_multipart(mp_sections)


# =============================================================================
# Gmail API helpers
# =============================================================================


def _is_opaque_gmail_token(value: str) -> bool:
    """Return True if value is an opaque client-side Gmail token.

    Modern Gmail URLs use encrypted per-account tokens (FMfcg…, KtbxL…, etc.)
    that cannot be mapped to API thread IDs (googleworkspace/cli#858).

    Detection: any alphanumeric token >= 20 chars that contains at least one
    letter. Pure-digit strings are excluded because they may be valid decimal
    thread IDs (thread-f:<digits>).  Shorter strings are excluded because
    16-hex thread IDs are exactly 16 chars.
    """
    if len(value) < 20:
        return False
    if not re.fullmatch(r"[A-Za-z0-9]+", value):
        return False
    # Pure digits could be valid decimal thread IDs
    if re.fullmatch(r"\d+", value):
        return False
    return True


def _is_thread_a_id(value: str) -> bool:
    """Return True if value is a client-side thread-a:r ID (popout URLs)."""
    return bool(re.search(r"thread-a[:%]3[Aa]r|thread-a:r", value))


_OPAQUE_TOKEN_MSG = (
    "This URL contains a client-side Gmail token that cannot be resolved to an "
    "API thread ID. Google encrypts these per-account with no public mapping "
    "(googleworkspace/cli#858).\n"
    "Use: gax mailbox -q '<search terms>' to locate the thread, "
    "then: gax get <hex thread_id>"
)


def extract_thread_id(url_or_id: str) -> str:
    """Extract thread ID from Gmail URL or return as-is."""
    from urllib.parse import unquote

    url_or_id = unquote(url_or_id)

    # Detect unsupported client-side IDs early with a helpful error
    if _is_thread_a_id(url_or_id):
        raise ValueError(
            f"Cannot extract thread ID from: {url_or_id}\n"
            "thread-a:r IDs are client-side popout identifiers with no API mapping.\n"
            + _OPAQUE_TOKEN_MSG
        )

    # Legacy URL pattern: #inbox/HEXID, #sent/HEXID, etc.
    match = re.search(r"#[^/]+/([A-Za-z0-9]+)$", url_or_id)
    if match:
        token = match.group(1)
        if _is_opaque_gmail_token(token):
            raise ValueError(
                f"Cannot extract thread ID from: {url_or_id}\n" + _OPAQUE_TOKEN_MSG
            )
        return token

    match = re.search(r"thread-f[:%]3A(\d+)", url_or_id)
    if match:
        return match.group(1)

    match = re.search(r"thread-f:(\d+)", url_or_id)
    if match:
        return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9]+", url_or_id):
        if _is_opaque_gmail_token(url_or_id):
            raise ValueError(_OPAQUE_TOKEN_MSG)
        return url_or_id

    raise ValueError(f"Cannot extract thread ID from: {url_or_id}")


def _get_header(headers: list, name: str) -> str:
    """Get header value by name."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_body(part: dict) -> str:
    """Decode message body from base64."""
    if "data" in part.get("body", {}):
        data = part["body"]["data"]
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""


def _strip_quoted_text(body: str) -> str:
    """Strip quoted reply history from email body.

    Detects and removes:
    - Lines starting with '>' (RFC 2822 quoting)
    - 'On ... wrote:' attribution blocks (Gmail/Outlook style, may wrap over
      up to 3 lines when the sender name is long)

    To avoid false positives on prose like 'On Monday we wrote: the report',
    the attribution detection requires at least one of:
    - An email address (<...@...>) in the attribution span, OR
    - The line immediately after the attribution ends with '>' quoting

    Returns the new-content portion only.
    """
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].rstrip()

        # '> ' prefixed quoting — start of quote block
        if stripped.startswith(">"):
            return "\n".join(lines[:i]).rstrip()

        # 'On ... wrote:' attribution — may span 1-3 lines
        if stripped.startswith("On "):
            end = min(i + 3, len(lines))
            lookahead = " ".join(lines[i:end])
            if "wrote:" in lookahead:
                # Require an email address OR immediately-following '>' quoting
                # to avoid false-positives on normal prose.
                has_email = bool(re.search(r"<[^>]+@[^>]+>", lookahead))
                next_line = lines[end].rstrip() if end < len(lines) else ""
                next_is_quote = next_line.startswith(">")
                if has_email or next_is_quote:
                    return "\n".join(lines[:i]).rstrip()

        i += 1

    return body


def _html_to_markdown(html: str) -> str:
    """Convert HTML to markdown using html2text."""
    h = _html2text.HTML2Text()
    h.ignore_links = False
    h.body_width = 0  # no wrapping
    return h.handle(html)


def _extract_text_body(payload: dict) -> str:
    """Extract plain text body from message payload.

    Prefers text/plain parts. Falls back to converting text/html to markdown
    when no plain-text part is available (HTML-only emails).
    """
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        return _decode_body(payload)

    if mime_type == "text/html":
        return _html_to_markdown(_decode_body(payload))

    if mime_type.startswith("multipart/"):
        parts = payload.get("parts", [])
        # Prefer text/plain
        for part in parts:
            if part.get("mimeType") == "text/plain":
                return _decode_body(part)
        # Fall back to text/html converted to markdown
        for part in parts:
            if part.get("mimeType") == "text/html":
                return _html_to_markdown(_decode_body(part))
        # Recurse into nested multipart
        for part in parts:
            result = _extract_text_body(part)
            if result:
                return result

    return ""


def _extract_attachments(payload: dict, message_id: str, service) -> list[Attachment]:
    """Extract and store attachments from message payload."""
    attachments = []

    def process_part(part: dict):
        filename = part.get("filename", "")
        if not filename:
            return

        body = part.get("body", {})
        attachment_id = body.get("attachmentId")
        mime_type = part.get("mimeType", "application/octet-stream")

        if attachment_id:
            att_data = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
            )

            data = base64.urlsafe_b64decode(att_data["data"])

            url = store_blob(
                data=data,
                original_name=filename,
                mime_type=mime_type,
                source_message_id=message_id,
            )

            attachments.append(
                Attachment(
                    name=filename,
                    size=len(data),
                    mime_type=mime_type,
                    url=url,
                )
            )

    def walk_parts(part: dict):
        process_part(part)
        for subpart in part.get("parts", []):
            walk_parts(subpart)

    walk_parts(payload)
    return attachments


def fetch_message(message_id: str, *, service=None) -> dict:
    """Fetch a single message from Gmail API by its hex ID.

    Returns the raw API response dict. Raises googleapiclient.errors.HttpError
    on 404 (message not found).
    """
    if service is None:
        service = get_service("gmail", "v1")

    return (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )


def message_to_reply_headers(msg: dict) -> dict:
    """Extract reply-relevant headers from a Gmail API message dict.

    Returns a dict with keys: to, subject, in_reply_to, references, thread_id,
    from_addr, id, reply_to.
    """
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    from_addr = _get_header(headers, "From")
    reply_to = _get_header(headers, "Reply-To")
    subject = _get_header(headers, "Subject") or "No Subject"
    rfc_message_id = _get_header(headers, "Message-Id")
    rfc_references = _get_header(headers, "References")

    # to = Reply-To (if set) or From (ADR 038 §3)
    to = reply_to or from_addr

    # subject = Re: + target subject (if not already prefixed)
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    # references = target's References + target's Message-ID (RFC 5322 §3.6.4)
    ref_parts = rfc_references.split() if rfc_references else []
    if rfc_message_id:
        ref_parts.append(rfc_message_id)
    references = " ".join(ref_parts)

    return {
        "to": to,
        "subject": subject,
        "in_reply_to": rfc_message_id,
        "references": references,
        "thread_id": msg.get("threadId", ""),
        "from_addr": from_addr,
        "id": msg.get("id", ""),
        "reply_to": reply_to,
    }


def pull_thread(thread_id: str, *, service=None) -> list[MailSection]:
    """Fetch thread from Gmail API and return list of sections."""
    if service is None:
        service = get_service("gmail", "v1")

    thread = (
        service.users()
        .threads()
        .get(
            userId="me",
            id=thread_id,
            format="full",
        )
        .execute()
    )

    messages = thread.get("messages", [])
    if not messages:
        raise ValueError(f"No messages found in thread {thread_id}")

    first_headers = messages[0].get("payload", {}).get("headers", [])
    subject = _get_header(first_headers, "Subject") or "No Subject"

    history_id = thread.get("historyId", "")
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"

    sections = []

    for i, msg in enumerate(messages, start=1):
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])

        from_addr = _get_header(headers, "From")
        to_addr = _get_header(headers, "To")
        date_str = _get_header(headers, "Date")
        rfc_message_id = _get_header(headers, "Message-Id")
        reply_to = _get_header(headers, "Reply-To")
        rfc_references = _get_header(headers, "References")
        msg_id = msg.get("id", "")

        try:
            from email.utils import parsedate_to_datetime

            date_dt = parsedate_to_datetime(date_str)
            date_iso = date_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            date_iso = date_str

        body = _strip_quoted_text(_extract_text_body(payload))
        attachments = _extract_attachments(payload, msg_id, service)

        sender_name = from_addr.split("<")[0].strip().strip('"') or from_addr
        section_title = f"From {sender_name}"

        sections.append(
            MailSection(
                title=subject,
                source=source_url,
                time=time_str,
                thread_id=thread_id,
                section=i,
                section_title=section_title,
                from_addr=from_addr,
                to_addr=to_addr,
                date=date_iso,
                content=body.strip(),
                attachments=attachments,
                message_id=rfc_message_id,
                id=msg_id,
                reply_to=reply_to,
                references=rfc_references,
                history_id=history_id if i == 1 else "",
            )
        )

    return sections
