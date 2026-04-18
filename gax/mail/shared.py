"""Shared data types, format helpers, and Gmail API functions for mail.

Used by both thread.py and mailbox.py.
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from googleapiclient.discovery import build

from ..auth import get_authenticated_credentials
from ..store import store_blob
from .. import multipart

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


# =============================================================================
# Multipart format helpers
# =============================================================================


def _mail_section_to_multipart(section: MailSection) -> multipart.Section:
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
    if section.attachments:
        headers["attachments"] = [
            {"name": att.name, "size": att.size, "url": att.url}
            for att in section.attachments
        ]
    return multipart.Section(headers=headers, content=section.content)


def format_section(section: MailSection) -> str:
    """Format a single section as YAML header + markdown body."""
    mp_section = _mail_section_to_multipart(section)
    return multipart.format_section(mp_section.headers, mp_section.content)


def format_multipart(sections: list[MailSection]) -> str:
    """Assemble sections into multipart markdown string."""
    mp_sections = [_mail_section_to_multipart(s) for s in sections]
    return multipart.format_multipart(mp_sections)


# =============================================================================
# Gmail API helpers
# =============================================================================


def extract_thread_id(url_or_id: str) -> str:
    """Extract thread ID from Gmail URL or return as-is."""
    from urllib.parse import unquote

    url_or_id = unquote(url_or_id)

    match = re.search(r"#[^/]+/([A-Za-z0-9]+)$", url_or_id)
    if match:
        return match.group(1)

    match = re.search(r"thread-f[:%]3A(\d+)", url_or_id)
    if match:
        return match.group(1)

    match = re.search(r"thread-f:(\d+)", url_or_id)
    if match:
        return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9]+", url_or_id):
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


def _extract_text_body(payload: dict) -> str:
    """Extract plain text body from message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        return _decode_body(payload)

    if mime_type.startswith("multipart/"):
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                return _decode_body(part)
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


def pull_thread(thread_id: str, *, service=None) -> list[MailSection]:
    """Fetch thread from Gmail API and return list of sections."""
    if service is None:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

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

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source_url = f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"

    sections = []

    for i, msg in enumerate(messages, start=1):
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])

        from_addr = _get_header(headers, "From")
        to_addr = _get_header(headers, "To")
        date_str = _get_header(headers, "Date")
        msg_id = msg.get("id", "")

        try:
            from email.utils import parsedate_to_datetime

            date_dt = parsedate_to_datetime(date_str)
            date_iso = date_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            date_iso = date_str

        body = _extract_text_body(payload)
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
            )
        )

    return sections
