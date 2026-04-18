"""Gmail draft management for gax.

Implements push/pull for email drafts as markdown files (.draft.gax.md).
See ADR 006 for design details.
"""

import base64
import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from . import multipart
from .resource import ResourceItem

logger = logging.getLogger(__name__)


@dataclass
class DraftConfig:
    """Configuration/metadata for a draft file."""

    draft_id: str = ""
    message_id: str = ""
    subject: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    thread_id: str = ""
    in_reply_to: str = ""
    source: str = ""
    time: str = ""


def parse_draft(content: str) -> tuple[DraftConfig, str]:
    """Parse a .draft.gax.md file into config and body.

    Returns:
        Tuple of (DraftConfig, body_content)
    """
    sections = multipart.parse_multipart(content)
    if not sections:
        raise ValueError("No content found in draft file")

    section = sections[0]
    headers = section.headers

    config = DraftConfig(
        draft_id=headers.get("draft_id", ""),
        message_id=headers.get("message_id", ""),
        subject=headers.get("subject", ""),
        to=headers.get("to", ""),
        cc=headers.get("cc", ""),
        bcc=headers.get("bcc", ""),
        thread_id=headers.get("thread_id", ""),
        in_reply_to=headers.get("in_reply_to", ""),
        source=headers.get("source", ""),
        time=headers.get("time", ""),
    )

    return config, section.content


def format_draft(config: DraftConfig, body: str) -> str:
    """Format a draft config and body as .draft.gax.md content."""
    headers: dict[str, Any] = {"type": "gax/draft"}

    # Add headers in consistent order
    if config.draft_id:
        headers["draft_id"] = config.draft_id
    if config.message_id:
        headers["message_id"] = config.message_id
    if config.thread_id:
        headers["thread_id"] = config.thread_id
    if config.in_reply_to:
        headers["in_reply_to"] = config.in_reply_to

    headers["subject"] = config.subject
    headers["to"] = config.to

    if config.cc:
        headers["cc"] = config.cc
    if config.bcc:
        headers["bcc"] = config.bcc
    if config.source:
        headers["source"] = config.source
    if config.time:
        headers["time"] = config.time

    return multipart.format_section(headers, body)


# =============================================================================
# Gmail Drafts API functions
# =============================================================================


def _create_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    thread_id: str = "",
    in_reply_to: str = "",
) -> dict:
    """Create RFC 2822 message for Gmail API."""
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to
    message["subject"] = subject

    if cc:
        message["cc"] = cc
    if bcc:
        message["bcc"] = bcc
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    result: dict[str, Any] = {"raw": raw}
    if thread_id:
        result["threadId"] = thread_id

    return result


def create_draft(config: DraftConfig, body: str, *, service=None) -> dict:
    """Create a new draft in Gmail.

    Returns:
        Gmail API response with draft info
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

    message = _create_message(
        to=config.to,
        subject=config.subject,
        body=body,
        cc=config.cc,
        bcc=config.bcc,
        thread_id=config.thread_id,
        in_reply_to=config.in_reply_to,
    )

    return (
        service.users()
        .drafts()
        .create(userId="me", body={"message": message})
        .execute()
    )


def update_draft(
    draft_id: str, config: DraftConfig, body: str, *, service=None
) -> dict:
    """Update an existing draft in Gmail.

    Returns:
        Gmail API response with updated draft info
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

    message = _create_message(
        to=config.to,
        subject=config.subject,
        body=body,
        cc=config.cc,
        bcc=config.bcc,
        thread_id=config.thread_id,
        in_reply_to=config.in_reply_to,
    )

    return (
        service.users()
        .drafts()
        .update(userId="me", id=draft_id, body={"message": message})
        .execute()
    )


def get_draft(draft_id: str, *, service=None) -> tuple[DraftConfig, str]:
    """Fetch a draft from Gmail.

    Returns:
        Tuple of (DraftConfig, body_content)
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

    result = (
        service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
    )

    message = result.get("message", {})
    payload = message.get("payload", {})
    headers_list = payload.get("headers", [])

    def get_header(name: str) -> str:
        for h in headers_list:
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""

    # Extract body
    body = _extract_body(payload)

    config = DraftConfig(
        draft_id=result.get("id", ""),
        message_id=message.get("id", ""),
        subject=get_header("Subject"),
        to=get_header("To"),
        cc=get_header("Cc"),
        bcc=get_header("Bcc"),
        thread_id=message.get("threadId", ""),
        in_reply_to=get_header("In-Reply-To"),
        source=f"https://mail.google.com/mail/u/0/#drafts/{result.get('id', '')}",
        time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    return config, body


def _extract_body(payload: dict) -> str:
    """Extract plain text body from message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        if "data" in payload.get("body", {}):
            data = payload["body"]["data"]
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    if mime_type.startswith("multipart/"):
        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain":
                if "data" in part.get("body", {}):
                    data = part["body"]["data"]
                    return base64.urlsafe_b64decode(data).decode(
                        "utf-8", errors="replace"
                    )
        # Fallback: recurse into parts
        for part in parts:
            result = _extract_body(part)
            if result:
                return result

    return ""


def list_drafts(*, limit: int = 100, service=None) -> list[dict]:
    """List drafts from Gmail.

    Returns:
        List of draft info dicts with id field
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

    drafts = []
    page_token = None

    while len(drafts) < limit:
        batch_size = min(100, limit - len(drafts))
        result = (
            service.users()
            .drafts()
            .list(userId="me", maxResults=batch_size, pageToken=page_token)
            .execute()
        )

        batch = result.get("drafts", [])
        drafts.extend(batch)

        page_token = result.get("nextPageToken")
        if not page_token or not batch:
            break

    return drafts[:limit]


def get_draft_summary(draft_id: str, *, service=None) -> dict:
    """Get summary info for a draft (metadata only)."""
    if service is None:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

    result = (
        service.users()
        .drafts()
        .get(
            userId="me",
            id=draft_id,
            format="metadata",
        )
        .execute()
    )

    message = result.get("message", {})
    headers_list = message.get("payload", {}).get("headers", [])

    def get_header(name: str) -> str:
        for h in headers_list:
            if h["name"].lower() == name.lower():
                return h["value"]
        return ""

    # Parse date
    date_str = get_header("Date")
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(date_str)
        date_short = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        date_short = date_str[:10] if date_str else ""

    return {
        "draft_id": result.get("id", ""),
        "thread_id": message.get("threadId", ""),
        "date": date_short,
        "to": get_header("To")[:40],
        "subject": get_header("Subject")[:60],
    }


def extract_draft_id(url_or_id: str) -> str:
    """Extract draft ID from Gmail URL or return as-is."""
    from urllib.parse import unquote

    url_or_id = unquote(url_or_id)

    # Gmail drafts URL: https://mail.google.com/mail/u/0/#drafts/r1234567890
    match = re.search(r"#drafts/([A-Za-z0-9-]+)$", url_or_id)
    if match:
        return match.group(1)

    # Already an ID
    if re.fullmatch(r"r?[A-Za-z0-9-]+", url_or_id):
        return url_or_id

    raise ValueError(f"Cannot extract draft ID from: {url_or_id}")


# =============================================================================
# Resource class
# =============================================================================


class Draft(ResourceItem):
    """Gmail draft resource."""

    name = "draft"

    def _output_path(self, subject: str, output: Path | None) -> Path:
        if output:
            return output
        safe = re.sub(r'[<>:"/\\|?*]', "-", subject or "untitled")
        safe = re.sub(r"\s+", "_", safe)[:50]
        return Path(f"{safe}.draft.gax.md")

    def new(self, to: str, subject: str, output: Path | None = None) -> Path:
        """Create a new local draft file. Returns path created."""
        config = DraftConfig(
            subject=subject,
            to=to,
            time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        content = format_draft(config, "\n")

        file_path = self._output_path(subject, output)
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        return file_path

    def list(self, out, *, limit: int = 100) -> None:
        """List Gmail drafts as TSV to file descriptor."""
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        drafts = list_drafts(limit=limit, service=service)
        if not drafts:
            return

        out.write("draft_id\tthread_id\tdate\tto\tsubject\n")
        for draft_info in drafts:
            draft_id = draft_info["id"]
            logger.info(f"Fetching: {draft_id}")
            try:
                summary = get_draft_summary(draft_id, service=service)
                out.write(
                    f"{summary['draft_id']}\t{summary['thread_id']}\t"
                    f"{summary['date']}\t{summary['to']}\t{summary['subject']}\n"
                )
            except Exception as e:
                logger.warning(f"Error fetching {draft_id}: {e}")

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Clone a draft from Gmail to a local file."""
        draft_id = extract_draft_id(url)
        logger.info(f"Fetching draft: {draft_id}")

        config, body = get_draft(draft_id)
        content = format_draft(config, body)

        file_path = self._output_path(config.subject, output)
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        logger.info(f"Subject: {config.subject}, To: {config.to}")
        return file_path

    def pull(self, path: Path, **kw) -> None:
        """Pull latest draft content from Gmail."""
        content = path.read_text(encoding="utf-8")
        config, _ = parse_draft(content)

        if not config.draft_id:
            raise ValueError("No draft_id in file")

        remote_config, remote_body = get_draft(config.draft_id)
        new_content = format_draft(remote_config, remote_body)
        path.write_text(new_content, encoding="utf-8")
        logger.info(f"Subject: {remote_config.subject}, To: {remote_config.to}")

    def diff(self, path: Path, **kw) -> str | None:
        """Preview changes between local draft and remote.

        Returns a human-readable diff string, or None if no changes.
        For new drafts (no draft_id), returns a summary of what will be created.
        """
        content = path.read_text(encoding="utf-8")
        config, body = parse_draft(content)

        if not config.to:
            raise ValueError("'to' field is required")
        if not config.subject:
            raise ValueError("'subject' field is required")

        if not config.draft_id:
            return f"New draft: {config.subject}\nTo: {config.to}"

        remote_config, remote_body = get_draft(config.draft_id)

        lines = []

        # Header changes
        if config.to != remote_config.to:
            lines.append(f"to: {remote_config.to} -> {config.to}")
        if config.subject != remote_config.subject:
            lines.append(f"subject: {remote_config.subject} -> {config.subject}")
        if config.cc != remote_config.cc:
            lines.append(f"cc: {remote_config.cc} -> {config.cc}")

        # Body diff
        body_diff = list(
            difflib.unified_diff(
                remote_body.splitlines(keepends=True),
                body.splitlines(keepends=True),
                fromfile="remote",
                tofile="local",
                lineterm="",
            )
        )
        if body_diff:
            lines.append("")
            lines.extend(line.rstrip("\n") for line in body_diff)

        return "\n".join(lines) if lines else None

    def push(self, path: Path, **kw) -> None:
        """Push local draft to Gmail. Unconditional — caller handles confirmation."""
        content = path.read_text(encoding="utf-8")
        config, body = parse_draft(content)

        if not config.to:
            raise ValueError("'to' field is required")
        if not config.subject:
            raise ValueError("'subject' field is required")

        if not config.draft_id:
            # Create new draft
            logger.info(f"Creating draft: {config.subject}")
            result = create_draft(config, body)

            config.draft_id = result["id"]
            config.message_id = result.get("message", {}).get("id", "")
            config.source = (
                f"https://mail.google.com/mail/u/0/#drafts/{config.draft_id}"
            )
        else:
            # Update existing draft
            logger.info(f"Updating draft: {config.draft_id}")
            update_draft(config.draft_id, config, body)

        config.time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_content = format_draft(config, body)
        path.write_text(new_content, encoding="utf-8")
        logger.info("Pushed successfully")
