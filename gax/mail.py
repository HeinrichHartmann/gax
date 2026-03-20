"""Gmail sync for gax.

Implements pull command for archiving email threads as multipart markdown (ADR 004).
"""

import base64
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from .store import store_blob
from . import multipart


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
# Gmail API functions
# =============================================================================


def extract_thread_id(url_or_id: str) -> str:
    """Extract thread ID from Gmail URL or return as-is."""
    from urllib.parse import unquote

    # URL decode first
    url_or_id = unquote(url_or_id)

    # Gmail URL format: https://mail.google.com/mail/u/0/#inbox/FMfcgzQXJWDsKmvPLCdfvxhHXqhSwBZV
    match = re.search(r"#[^/]+/([A-Za-z0-9]+)$", url_or_id)
    if match:
        return match.group(1)

    # Popout URL with th parameter: th=#thread-f:1859907402038417535
    match = re.search(r"thread-f[:%]3A(\d+)", url_or_id)
    if match:
        return match.group(1)

    # th parameter already decoded: #thread-f:1859907402038417535
    match = re.search(r"thread-f:(\d+)", url_or_id)
    if match:
        return match.group(1)

    # Already an ID (alphanumeric or numeric)
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
        # Gmail uses URL-safe base64
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
            # Prefer text/plain
            if part.get("mimeType") == "text/plain":
                return _decode_body(part)
        # Fallback to first text part
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
            # Fetch attachment data
            att_data = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
            )

            data = base64.urlsafe_b64decode(att_data["data"])

            # Store in CAS
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
    """Fetch thread from Gmail API and return list of sections.

    Args:
        thread_id: Gmail thread ID
        service: Optional Gmail API service object for testing
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

    # Fetch thread with full message content
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

    # Get subject from first message
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

        # Parse date to ISO format
        try:
            from email.utils import parsedate_to_datetime

            date_dt = parsedate_to_datetime(date_str)
            date_iso = date_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            date_iso = date_str

        # Extract body
        body = _extract_text_body(payload)

        # Extract attachments
        attachments = _extract_attachments(payload, msg_id, service)

        # Create section title from sender
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


# =============================================================================
# CLI commands
# =============================================================================


@click.group()
def mail():
    """Gmail operations"""
    pass


@mail.command()
def labels():
    """List Gmail labels (TSV output)."""
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        result = service.users().labels().list(userId="me").execute()
        labels_list = result.get("labels", [])

        # Print header
        click.echo("id\tname\ttype")

        # Sort: system labels first, then user labels alphabetically
        system_labels = [label for label in labels_list if label.get("type") == "system"]
        user_labels = [label for label in labels_list if label.get("type") == "user"]

        system_labels.sort(key=lambda label: label.get("name", ""))
        user_labels.sort(key=lambda label: label.get("name", ""))

        for label in system_labels + user_labels:
            label_id = label.get("id", "")
            name = label.get("name", "")
            label_type = label.get("type", "")
            click.echo(f"{label_id}\t{name}\t{label_type}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _is_thread_id(value: str) -> bool:
    """Check if value looks like a thread ID (vs a search query)."""
    # Gmail URL
    if "mail.google.com" in value:
        return True
    # Pure hex string (typical thread ID)
    if re.fullmatch(r"[0-9a-f]{16}", value):
        return True
    # Alphanumeric ID (Gmail web IDs)
    if re.fullmatch(r"[A-Za-z0-9]{20,}", value):
        return True
    # Numeric thread ID from popout URLs
    if re.fullmatch(r"\d{15,}", value):
        return True
    return False


@mail.command()
@click.argument("query_or_id")
@click.option(
    "--to",
    "folder",
    type=click.Path(path_type=Path),
    help="Clone to folder (bulk mode)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (single thread mode)",
)
@click.option(
    "--limit", default=100, help="Maximum threads to clone in bulk mode (default: 100)"
)
def clone(query_or_id: str, folder: Optional[Path], output: Optional[Path], limit: int):
    """Clone email thread(s) to local .mail.gax file(s).

    Single thread mode (ID or URL):

        gax mail clone 19d0bed1cddbab6d
        gax mail clone "https://mail.google.com/..."

    Bulk mode (query with --to):

        gax mail clone "label:Inbox" --to Inbox/
        gax mail clone "from:alice" --to Alice/ --limit 50
    """
    try:
        # Detect single thread vs search query
        if _is_thread_id(query_or_id) and not folder:
            # Single thread mode
            thread_id = extract_thread_id(query_or_id)

            click.echo(f"Fetching thread: {thread_id}")
            sections = pull_thread(thread_id)
            content = format_multipart(sections)

            if output:
                file_path = output
            else:
                # Generate filename from subject
                safe_subject = re.sub(r'[<>:"/\\|?*]', "-", sections[0].title)
                safe_subject = re.sub(r"\s+", "_", safe_subject)[:50]
                file_path = Path(f"{safe_subject}_{thread_id}.mail.gax")

            if file_path.exists():
                click.echo(f"Error: File already exists: {file_path}", err=True)
                click.echo('Use "gax mail pull" to update an existing file.', err=True)
                sys.exit(1)

            file_path.write_text(content, encoding="utf-8")
            click.echo(f"Created: {file_path}")
            click.echo(f"Subject: {sections[0].title}")
            click.echo(f"Messages: {len(sections)}")

            # Report attachments
            total_attachments = sum(len(s.attachments) for s in sections)
            if total_attachments:
                click.echo(f"Attachments: {total_attachments}")

        else:
            # Bulk mode: search query -> folder
            if not folder:
                click.echo(
                    "Error: Use --to <folder> for bulk cloning with a search query",
                    err=True,
                )
                click.echo(
                    "Or provide a thread ID/URL for single thread mode.", err=True
                )
                sys.exit(1)

            creds = get_authenticated_credentials()
            service = build("gmail", "v1", credentials=creds)

            # Search threads
            click.echo(f"Searching: {query_or_id}")
            threads = []
            page_token = None

            while len(threads) < limit:
                batch_size = min(100, limit - len(threads))
                result = (
                    service.users()
                    .threads()
                    .list(
                        userId="me",
                        q=query_or_id,
                        maxResults=batch_size,
                        pageToken=page_token,
                    )
                    .execute()
                )

                batch = result.get("threads", [])
                threads.extend(batch)

                page_token = result.get("nextPageToken")
                if not page_token or not batch:
                    break

            threads = threads[:limit]

            if not threads:
                click.echo("No threads found.")
                return

            # Check if more available
            total_estimate = result.get("resultSizeEstimate", 0)
            if total_estimate > limit:
                click.echo(
                    f"Warning: Found ~{total_estimate} threads, cloning first {limit}. Use --limit to clone more.",
                    err=True,
                )

            click.echo(f"Found {len(threads)} threads")

            # Create folder
            folder.mkdir(parents=True, exist_ok=True)

            # Get already cloned thread IDs
            existing_ids = _get_existing_thread_ids(folder)

            # Clone each thread
            cloned = 0
            skipped = 0

            for thread_info in threads:
                thread_id = thread_info["id"]

                if thread_id in existing_ids:
                    skipped += 1
                    continue

                try:
                    sections = pull_thread(thread_id)
                    content = format_multipart(sections)

                    # Get first message info for filename
                    first = sections[0]
                    filename = _make_filename(
                        first.date, first.from_addr, first.title, thread_id
                    )
                    file_path = folder / filename

                    # Avoid overwriting (add thread_id suffix if collision)
                    if file_path.exists():
                        base = file_path.stem
                        file_path = folder / f"{base}_{thread_id}.mail.gax"

                    file_path.write_text(content, encoding="utf-8")
                    cloned += 1
                    click.echo(f"  {filename}")

                except Exception as e:
                    click.echo(f"  Error cloning {thread_id}: {e}", err=True)

            click.echo(f"Cloned: {cloned}, Skipped: {skipped} (already present)")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _pull_single_file(file_path: Path) -> tuple[int, int]:
    """Pull updates for a single .mail.gax file. Returns (old_count, new_count)."""
    content = file_path.read_text(encoding="utf-8")

    # Extract thread_id from file
    match = re.search(r"^thread_id:\s*(\S+)", content, re.MULTILINE)
    if not match:
        raise ValueError(f"No thread_id found in {file_path}")

    thread_id = match.group(1)

    # Count existing messages
    old_count = len(re.findall(r"^section:\s*\d+", content, re.MULTILINE))

    sections = pull_thread(thread_id)
    new_content = format_multipart(sections)

    new_count = len(sections)

    file_path.write_text(new_content, encoding="utf-8")
    return old_count, new_count


@mail.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def pull(path: Path):
    """Pull latest messages for .mail.gax file(s).

    Single file:

        gax mail pull thread.mail.gax

    Folder (updates all .mail.gax files):

        gax mail pull Inbox/
    """
    try:
        if path.is_file():
            # Single file mode
            click.echo(f"Updating: {path}")
            old_count, new_count = _pull_single_file(path)
            click.echo(f"Messages: {old_count} -> {new_count}")
            if new_count > old_count:
                click.echo(f"New messages: {new_count - old_count}")

        elif path.is_dir():
            # Folder mode: update all .mail.gax files
            files = list(path.glob("*.mail.gax"))
            if not files:
                click.echo(f"No .mail.gax files found in {path}")
                return

            click.echo(f"Updating {len(files)} files in {path}/")
            updated = 0
            errors = 0
            total_new = 0

            for file_path in sorted(files):
                try:
                    old_count, new_count = _pull_single_file(file_path)
                    updated += 1
                    new_messages = new_count - old_count
                    if new_messages > 0:
                        total_new += new_messages
                        click.echo(f"  {file_path.name}: +{new_messages} messages")
                except Exception as e:
                    errors += 1
                    click.echo(f"  {file_path.name}: Error - {e}", err=True)

            click.echo(f"Updated: {updated}, Errors: {errors}")
            if total_new > 0:
                click.echo(f"Total new messages: {total_new}")

        else:
            click.echo(f"Error: {path} is not a file or directory", err=True)
            sys.exit(1)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _make_filename(date_str: str, from_addr: str, subject: str, _thread_id: str) -> str:
    """Create filename: date-from-subject.mail.gax"""
    # Extract date (YYYY-MM-DD)
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(date_str)
        date_part = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        # Try ISO format
        if "T" in date_str:
            date_part = date_str.split("T")[0]
        else:
            date_part = "unknown-date"

    # Extract email from "Name <email>" format
    email_match = re.search(r"<([^>]+)>", from_addr)
    if email_match:
        from_part = email_match.group(1)
    else:
        from_part = from_addr.split()[0] if from_addr else "unknown"

    # Sanitize from
    from_part = re.sub(r'[<>:"/\\|?*\s]', "", from_part)[:30]

    # Sanitize subject
    subject_part = re.sub(r'[<>:"/\\|?*]', "-", subject)
    subject_part = re.sub(r"\s+", "_", subject_part)[:40]

    return f"{date_part}-{from_part}-{subject_part}.mail.gax"


def _get_existing_thread_ids(folder: Path) -> set[str]:
    """Get thread IDs already synced to folder."""
    if not folder.exists():
        return set()

    thread_ids = set()
    for f in folder.glob("*.mail.gax"):
        # Try to extract thread_id from file content
        try:
            content = f.read_text(encoding="utf-8")
            match = re.search(r"^thread_id:\s*(\S+)", content, re.MULTILINE)
            if match:
                thread_ids.add(match.group(1))
        except Exception:
            pass

    return thread_ids


def _get_thread_summary(thread_id: str, service) -> dict:
    """Get summary info for a thread (first message metadata)."""
    thread = (
        service.users()
        .threads()
        .get(
            userId="me",
            id=thread_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )

    messages = thread.get("messages", [])
    if not messages:
        return {"thread_id": thread_id, "date": "", "from": "", "subject": ""}

    # Get first message headers
    headers = messages[0].get("payload", {}).get("headers", [])

    from_addr = _get_header(headers, "From")
    subject = _get_header(headers, "Subject")
    date_str = _get_header(headers, "Date")

    # Extract email from "Name <email>" format
    email_match = re.search(r"<([^>]+)>", from_addr)
    if email_match:
        from_email = email_match.group(1)
    else:
        from_email = from_addr.split()[0] if from_addr else ""

    # Parse date
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(date_str)
        date_short = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        date_short = date_str[:10] if date_str else ""

    return {
        "thread_id": thread_id,
        "date": date_short,
        "from": from_email,
        "subject": subject[:60],
    }


@mail.command()
@click.argument("query")
@click.option("--limit", default=100, help="Maximum results (default: 100)")
def search(query: str, limit: int):
    """Search Gmail and list matching threads (TSV output).

    Uses Gmail query syntax: from:, to:, subject:, after:, before:, has:attachment, etc.

    Output is TSV (tab-separated) for easy parsing:

    \b
        thread_id    date    from    subject

    Pipe to pull: gax mail search "from:alice" | tail -n +2 | cut -f1 | xargs -I{} gax mail pull {}
    """
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Search threads
        threads = []
        page_token = None
        total_estimate = 0

        while len(threads) < limit:
            batch_size = min(100, limit - len(threads))
            result = (
                service.users()
                .threads()
                .list(
                    userId="me",
                    q=query,
                    maxResults=batch_size,
                    pageToken=page_token,
                )
                .execute()
            )

            total_estimate = result.get("resultSizeEstimate", 0)
            batch = result.get("threads", [])
            threads.extend(batch)

            page_token = result.get("nextPageToken")
            if not page_token or not batch:
                break

        threads = threads[:limit]

        if not threads:
            click.echo("No threads found.", err=True)
            sys.exit(1)

        # Check if more available
        if total_estimate > limit:
            click.echo(
                f"# Found ~{total_estimate} threads, showing first {limit}", err=True
            )

        # Print header
        click.echo("thread_id\tdate\tfrom\tsubject")

        # Get summary for each thread
        for thread_info in threads:
            thread_id = thread_info["id"]
            try:
                summary = _get_thread_summary(thread_id, service)
                click.echo(
                    f"{summary['thread_id']}\t{summary['date']}\t{summary['from']}\t{summary['subject']}"
                )
            except Exception as e:
                click.echo(f"# Error fetching {thread_id}: {e}", err=True)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
