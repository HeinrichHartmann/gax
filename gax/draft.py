"""Gmail draft management for gax.

Reference resource module — intended as a template for new resources.

Implements push/pull for email drafts as markdown files (.draft.gax.md).
See ADR 006 for design details.

Module structure
================

  DraftHeader          — dataclass for .draft.gax.md frontmatter
  File format          — parse/format .draft.gax.md files
  Gmail API helpers    — wrappers around Gmail API quirks
  Draft(Resource)  — resource class (the public interface for cli.py)

Design decisions
================

Separation of concerns:
  This module contains ONLY business logic. No Click, no sys.exit(), no
  UI imports. The CLI layer (cli.py) owns all command definitions, argument
  parsing, confirmation prompts, and user-facing output. cli.py imports only
  the Draft class — never module-level functions.

Resource class as public interface:
  The Draft class is what cli.py calls. All core operations (new, clone,
  list, pull, diff, push) are methods on this class. The class is stateless —
  just a namespace conforming to the Resource interface.

Communication conventions:
  - logging.info()  — status messages (picked up by the spinner)
  - ValueError      — user-fixable errors (cli.py catches and formats)
  - Return values   — results for cli.py to format (e.g. Path from clone)
  - File descriptor  — for streaming output (e.g. list writes TSV to `out`)

Module-level functions:
  Shared helpers extracted from the class to reduce duplication and keep
  methods readable. NOT an additional abstraction layer — just factoring.
  No underscores: everything in this module is internal to the resource
  except DraftHeader, parse_draft, and format_draft (used by mail.py).

What we chose NOT to abstract:
  - No separate API client class. The Gmail API calls live directly in the
    class methods and helpers. An API layer would add indirection without
    real benefit at this scale.
  - No base class for .gax.md file handling. parse_draft/format_draft could
    generalize (every resource has frontmatter + body), but we avoid
    premature abstraction. If a pattern emerges across 3+ resources, then
    extract.
  - No caching or service reuse across methods. diff() then push() will
    auth and fetch twice. Acceptable cost for simplicity — optimize later
    if profiling shows it matters.
"""

import base64
import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from . import multipart
from .resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# Data class — shared between file format functions and the resource class.
# Defined first because Python needs it before type annotations that use it.
# =============================================================================


@dataclass
class DraftHeader:
    """Frontmatter of a .draft.gax.md file."""

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


# =============================================================================
# File format — parse/format .draft.gax.md files.
# Public: parse_draft, format_draft are used by mail.py for reply drafts.
# parse_draft_id is URL/ID parsing, only used by Draft.clone().
# =============================================================================


def parse_draft(content: str) -> tuple[DraftHeader, str]:
    """Parse a .draft.gax.md file into header and body.

    Returns:
        Tuple of (DraftHeader, body_content)
    """
    sections = multipart.parse_multipart(content)
    if not sections:
        raise ValueError("No content found in draft file")

    section = sections[0]
    h = section.headers

    header = DraftHeader(
        draft_id=h.get("draft_id", ""),
        message_id=h.get("message_id", ""),
        subject=h.get("subject", ""),
        to=h.get("to", ""),
        cc=h.get("cc", ""),
        bcc=h.get("bcc", ""),
        thread_id=h.get("thread_id", ""),
        in_reply_to=h.get("in_reply_to", ""),
        source=h.get("source", ""),
        time=h.get("time", ""),
    )

    return header, section.content


def format_draft(header: DraftHeader, body: str) -> str:
    """Format a draft header and body as .draft.gax.md content."""
    h: dict[str, Any] = {"type": "gax/draft"}

    if header.draft_id:
        h["draft_id"] = header.draft_id
    if header.message_id:
        h["message_id"] = header.message_id
    if header.thread_id:
        h["thread_id"] = header.thread_id
    if header.in_reply_to:
        h["in_reply_to"] = header.in_reply_to

    h["subject"] = header.subject
    h["to"] = header.to

    if header.cc:
        h["cc"] = header.cc
    if header.bcc:
        h["bcc"] = header.bcc
    if header.source:
        h["source"] = header.source
    if header.time:
        h["time"] = header.time

    return multipart.format_section(h, body)


def parse_draft_id(url_or_id: str) -> str:
    """Extract draft ID from Gmail URL or return as-is."""
    from urllib.parse import unquote

    url_or_id = unquote(url_or_id)

    match = re.search(r"#drafts/([A-Za-z0-9-]+)$", url_or_id)
    if match:
        return match.group(1)

    if re.fullmatch(r"r?[A-Za-z0-9-]+", url_or_id):
        return url_or_id

    raise ValueError(f"Cannot extract draft ID from: {url_or_id}")


# =============================================================================
# Gmail API helpers — simplify the quirky Gmail API response format.
# These are module-internal: extracted from class methods to reduce
# duplication, not to build an API abstraction layer.
# =============================================================================


def get_header(headers_list: list[dict], name: str) -> str:
    """Get a header value by name from Gmail API headers list."""
    for h in headers_list:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def build_message(header: DraftHeader, body: str) -> dict:
    """Build RFC 2822 message dict for Gmail API."""
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = header.to
    message["subject"] = header.subject

    if header.cc:
        message["cc"] = header.cc
    if header.bcc:
        message["bcc"] = header.bcc
    if header.in_reply_to:
        message["In-Reply-To"] = header.in_reply_to
        message["References"] = header.in_reply_to

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    result: dict[str, Any] = {"raw": raw}
    if header.thread_id:
        result["threadId"] = header.thread_id

    return result


def extract_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload."""
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
        for part in parts:
            result = extract_body(part)
            if result:
                return result

    return ""


def fetch_draft(draft_id: str, *, service=None) -> tuple[DraftHeader, str]:
    """Fetch a draft from Gmail. Returns (DraftHeader, body)."""
    if service is None:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

    result = (
        service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
    )

    message = result.get("message", {})
    payload = message.get("payload", {})
    headers_list = payload.get("headers", [])

    header = DraftHeader(
        draft_id=result.get("id", ""),
        message_id=message.get("id", ""),
        subject=get_header(headers_list, "Subject"),
        to=get_header(headers_list, "To"),
        cc=get_header(headers_list, "Cc"),
        bcc=get_header(headers_list, "Bcc"),
        thread_id=message.get("threadId", ""),
        in_reply_to=get_header(headers_list, "In-Reply-To"),
        source=f"https://mail.google.com/mail/u/0/#drafts/{result.get('id', '')}",
        time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    return header, extract_body(payload)


# =============================================================================
# Resource class — the public interface for cli.py.
# All core operations are methods here. The class is stateless (no __init__).
# cli.py calls Draft().clone(), Draft().push(), etc.
# =============================================================================


class Draft(Resource):
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
        header = DraftHeader(
            subject=subject,
            to=to,
            time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        content = format_draft(header, "\n")

        file_path = self._output_path(subject, output)
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        return file_path

    def list(self, out, *, limit: int = 100) -> None:
        """List Gmail drafts as TSV to file descriptor."""
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Fetch draft ID list
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
        drafts = drafts[:limit]

        if not drafts:
            return

        out.write("draft_id\tthread_id\tdate\tto\tsubject\n")
        for draft_info in drafts:
            did = draft_info["id"]
            logger.info(f"Fetching: {did}")
            try:
                result = (
                    service.users()
                    .drafts()
                    .get(userId="me", id=did, format="metadata")
                    .execute()
                )
                message = result.get("message", {})
                hl = message.get("payload", {}).get("headers", [])

                date_str = get_header(hl, "Date")
                try:
                    date_short = parsedate_to_datetime(date_str).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    date_short = date_str[:10] if date_str else ""

                out.write(
                    f"{result.get('id', '')}\t"
                    f"{message.get('threadId', '')}\t"
                    f"{date_short}\t"
                    f"{get_header(hl, 'To')[:40]}\t"
                    f"{get_header(hl, 'Subject')[:60]}\n"
                )
            except Exception as e:
                logger.warning(f"Error fetching {did}: {e}")

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Clone a draft from Gmail to a local file."""
        draft_id = parse_draft_id(url)
        logger.info(f"Fetching draft: {draft_id}")

        header, body = fetch_draft(draft_id)
        content = format_draft(header, body)

        file_path = self._output_path(header.subject, output)
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        logger.info(f"Subject: {header.subject}, To: {header.to}")
        return file_path

    def pull(self, path: Path, **kw) -> None:
        """Pull latest draft content from Gmail."""
        content = path.read_text(encoding="utf-8")
        header, _ = parse_draft(content)

        if not header.draft_id:
            raise ValueError("No draft_id in file")

        remote_header, remote_body = fetch_draft(header.draft_id)
        new_content = format_draft(remote_header, remote_body)
        path.write_text(new_content, encoding="utf-8")
        logger.info(f"Subject: {remote_header.subject}, To: {remote_header.to}")

    def diff(self, path: Path, **kw) -> str | None:
        """Preview changes between local draft and remote.

        Returns a human-readable diff string, or None if no changes.
        For new drafts (no draft_id), returns a summary of what will be created.
        """
        content = path.read_text(encoding="utf-8")
        header, body = parse_draft(content)

        if not header.to:
            raise ValueError("'to' field is required")
        if not header.subject:
            raise ValueError("'subject' field is required")

        if not header.draft_id:
            return f"New draft: {header.subject}\nTo: {header.to}"

        remote_header, remote_body = fetch_draft(header.draft_id)

        lines = []

        if header.to != remote_header.to:
            lines.append(f"to: {remote_header.to} -> {header.to}")
        if header.subject != remote_header.subject:
            lines.append(f"subject: {remote_header.subject} -> {header.subject}")
        if header.cc != remote_header.cc:
            lines.append(f"cc: {remote_header.cc} -> {header.cc}")

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
        header, body = parse_draft(content)

        if not header.to:
            raise ValueError("'to' field is required")
        if not header.subject:
            raise ValueError("'subject' field is required")

        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)
        message = build_message(header, body)

        if not header.draft_id:
            logger.info(f"Creating draft: {header.subject}")
            result = (
                service.users()
                .drafts()
                .create(userId="me", body={"message": message})
                .execute()
            )
            header.draft_id = result["id"]
            header.message_id = result.get("message", {}).get("id", "")
            header.source = (
                f"https://mail.google.com/mail/u/0/#drafts/{header.draft_id}"
            )
        else:
            logger.info(f"Updating draft: {header.draft_id}")
            service.users().drafts().update(
                userId="me", id=header.draft_id, body={"message": message}
            ).execute()

        header.time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_content = format_draft(header, body)
        path.write_text(new_content, encoding="utf-8")
        logger.info("Pushed successfully")
