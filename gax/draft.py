"""Gmail draft management for gax.

Implements push/pull for email drafts as markdown files (.draft.gax).
See ADR 006 for design details.
"""

import base64
import difflib
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Optional

import click
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from . import multipart


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
    """Parse a .draft.gax file into config and body.

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
    """Format a draft config and body as .draft.gax content."""
    headers: dict[str, Any] = {}

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


def update_draft(draft_id: str, config: DraftConfig, body: str, *, service=None) -> dict:
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
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
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
            metadataHeaders=["To", "Subject", "Date"],
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
# CLI commands
# =============================================================================


@click.group()
def draft():
    """Draft operations"""
    pass


@draft.command("new")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <subject>.draft.gax)",
)
@click.option("--to", "to_addr", default="", help="Recipient email address")
@click.option("--subject", default="", help="Email subject")
def draft_new(output: Optional[Path], to_addr: str, subject: str):
    """Create a new local draft file.

    Creates a .draft.gax file that can be edited and pushed to Gmail.

    Examples:

        gax mail draft new
        gax mail draft new --to alice@example.com --subject "Hello"
        gax mail draft new -o my_draft.draft.gax
    """
    # Prompt for required fields if not provided
    if not to_addr:
        to_addr = click.prompt("To")
    if not subject:
        subject = click.prompt("Subject")

    config = DraftConfig(
        subject=subject,
        to=to_addr,
        time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    body = "\n"  # Empty body with newline

    content = format_draft(config, body)

    if output:
        file_path = output
    else:
        # Generate filename from subject
        safe_subject = re.sub(r'[<>:"/\\|?*]', "-", subject)
        safe_subject = re.sub(r"\s+", "_", safe_subject)[:50]
        file_path = Path(f"{safe_subject}.draft.gax")

    if file_path.exists():
        click.echo(f"Error: File already exists: {file_path}", err=True)
        sys.exit(1)

    file_path.write_text(content, encoding="utf-8")
    click.echo(f"Created: {file_path}")
    click.echo(f"Edit the file, then run: gax mail draft push {file_path}")


@draft.command("clone")
@click.argument("draft_id_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <subject>.draft.gax)",
)
def draft_clone(draft_id_or_url: str, output: Optional[Path]):
    """Clone an existing draft from Gmail.

    Examples:

        gax mail draft clone r-1234567890123456789
        gax mail draft clone "https://mail.google.com/mail/u/0/#drafts/..."
        gax mail draft clone r-1234567890 -o my_draft.draft.gax
    """
    try:
        draft_id = extract_draft_id(draft_id_or_url)
        click.echo(f"Fetching draft: {draft_id}")

        config, body = get_draft(draft_id)
        content = format_draft(config, body)

        if output:
            file_path = output
        else:
            # Generate filename from subject
            safe_subject = re.sub(r'[<>:"/\\|?*]', "-", config.subject or "untitled")
            safe_subject = re.sub(r"\s+", "_", safe_subject)[:50]
            file_path = Path(f"{safe_subject}.draft.gax")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")
        click.echo(f"Created: {file_path}")
        click.echo(f"Subject: {config.subject}")
        click.echo(f"To: {config.to}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@draft.command("list")
@click.option("--limit", default=100, help="Maximum results (default: 100)")
def draft_list(limit: int):
    """List Gmail drafts (TSV output).

    Output columns: draft_id, thread_id, date, to, subject
    """
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        drafts = list_drafts(limit=limit, service=service)

        if not drafts:
            click.echo("No drafts found.", err=True)
            return

        # Print header
        click.echo("draft_id\tthread_id\tdate\tto\tsubject")

        for draft_info in drafts:
            draft_id = draft_info["id"]
            try:
                summary = get_draft_summary(draft_id, service=service)
                click.echo(
                    f"{summary['draft_id']}\t{summary['thread_id']}\t"
                    f"{summary['date']}\t{summary['to']}\t{summary['subject']}"
                )
            except Exception as e:
                click.echo(f"# Error fetching {draft_id}: {e}", err=True)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@draft.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def draft_push(file: Path, yes: bool):
    """Push local draft to Gmail.

    If the draft doesn't exist in Gmail yet, creates it.
    If it exists, shows diff and updates it (with confirmation).

    Examples:

        gax mail draft push my_draft.draft.gax
        gax mail draft push my_draft.draft.gax -y
    """
    try:
        content = file.read_text(encoding="utf-8")
        config, body = parse_draft(content)

        # Validate required fields
        if not config.to:
            click.echo("Error: 'to' field is required", err=True)
            sys.exit(1)
        if not config.subject:
            click.echo("Error: 'subject' field is required", err=True)
            sys.exit(1)

        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        if not config.draft_id:
            # Create new draft
            click.echo(f"Creating draft: {config.subject}")
            result = create_draft(config, body, service=service)

            # Update local file with draft_id
            config.draft_id = result["id"]
            config.message_id = result.get("message", {}).get("id", "")
            config.source = f"https://mail.google.com/mail/u/0/#drafts/{config.draft_id}"
            config.time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            new_content = format_draft(config, body)
            file.write_text(new_content, encoding="utf-8")

            click.echo(f"Created draft: {config.draft_id}")
            click.echo(f"Updated: {file}")

        else:
            # Update existing draft - show diff first
            try:
                remote_config, remote_body = get_draft(config.draft_id, service=service)
            except Exception as e:
                if "404" in str(e) or "not found" in str(e).lower():
                    click.echo(
                        f"Error: Draft {config.draft_id} no longer exists in Gmail.",
                        err=True,
                    )
                    click.echo(
                        "Clear draft_id in the file and push again to create a new draft.",
                        err=True,
                    )
                    sys.exit(1)
                raise

            # Compare local vs remote
            local_lines = body.splitlines(keepends=True)
            remote_lines = remote_body.splitlines(keepends=True)

            diff = list(
                difflib.unified_diff(
                    remote_lines,
                    local_lines,
                    fromfile="remote",
                    tofile="local",
                    lineterm="",
                )
            )

            # Also check header changes
            header_changes = []
            if config.to != remote_config.to:
                header_changes.append(f"to: {remote_config.to} -> {config.to}")
            if config.subject != remote_config.subject:
                header_changes.append(
                    f"subject: {remote_config.subject} -> {config.subject}"
                )
            if config.cc != remote_config.cc:
                header_changes.append(f"cc: {remote_config.cc} -> {config.cc}")

            if not diff and not header_changes:
                click.echo("No differences to push.")
                return

            # Show changes
            if header_changes:
                click.echo("Header changes:")
                for change in header_changes:
                    click.echo(f"  {change}")

            if diff:
                click.echo("Body changes:")
                click.echo("-" * 40)
                for line in diff:
                    click.echo(line.rstrip("\n"))
                click.echo("-" * 40)

            # Confirm
            if not yes:
                if not click.confirm("Push these changes?"):
                    click.echo("Aborted.")
                    return

            # Update draft
            click.echo(f"Updating draft: {config.draft_id}")
            update_draft(config.draft_id, config, body, service=service)

            # Update local file with new timestamp
            config.time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            new_content = format_draft(config, body)
            file.write_text(new_content, encoding="utf-8")

            click.echo("Pushed successfully.")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@draft.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def draft_pull(file: Path):
    """Pull latest content from Gmail draft.

    Updates the local .draft.gax file with the remote draft content.

    Example:

        gax mail draft pull my_draft.draft.gax
    """
    try:
        content = file.read_text(encoding="utf-8")
        config, local_body = parse_draft(content)

        if not config.draft_id:
            click.echo(
                "Error: No draft_id in file. Use 'push' first to create a draft.",
                err=True,
            )
            sys.exit(1)

        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        try:
            remote_config, remote_body = get_draft(config.draft_id, service=service)
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                click.echo(
                    f"Error: Draft {config.draft_id} no longer exists in Gmail.",
                    err=True,
                )
                click.echo("The draft may have been sent or deleted.", err=True)
                sys.exit(1)
            raise

        # Check for local changes that would be overwritten
        if local_body.strip() != remote_body.strip():
            click.echo("Warning: Local changes will be overwritten.", err=True)

        # Update local file with remote content
        new_content = format_draft(remote_config, remote_body)
        file.write_text(new_content, encoding="utf-8")

        click.echo(f"Updated: {file}")
        click.echo(f"Subject: {remote_config.subject}")
        click.echo(f"To: {remote_config.to}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
