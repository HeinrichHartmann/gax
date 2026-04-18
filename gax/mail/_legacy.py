"""Gmail sync for gax.

Implements pull command for archiving email threads as multipart markdown (ADR 004).
"""

import base64
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from googleapiclient.discovery import build

from ..auth import get_authenticated_credentials
from ..ui import operation, success, error
from ..store import store_blob
from .. import multipart
from .. import draft as draft_module
from .. import docs as doc

logger = logging.getLogger(__name__)
# label and filter are now registered in cli.py as top-level commands (ADR 020)


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


# Old mail container group removed - thread is now the mail group (ADR 020)


@doc.section("resource")
@click.group()
def thread():
    """Individual email thread operations (clone, pull, reply)"""
    pass


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


@thread.command()
@click.argument("thread_id_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file",
)
def clone(thread_id_or_url: str, output: Optional[Path]):
    """Clone a single email thread to a local .mail.gax.md file.

    \b
    Examples:
        gax mail thread clone 19d0bed1cddbab6d
        gax mail thread clone "https://mail.google.com/..."
        gax mail thread clone 19d0bed1cddbab6d -o thread.mail.gax.md

    For bulk cloning, use: gax mail list checkout FOLDER -q QUERY
    """
    try:
        if not _is_thread_id(thread_id_or_url):
            click.echo(
                f"Error: '{thread_id_or_url}' is not a valid thread ID or URL",
                err=True,
            )
            click.echo(
                "For bulk cloning, use: gax mail list checkout FOLDER -q QUERY",
                err=True,
            )
            sys.exit(1)

        thread_id = extract_thread_id(thread_id_or_url)

        click.echo(f"Fetching thread: {thread_id}")
        sections = pull_thread(thread_id)
        content = format_multipart(sections)

        if output:
            file_path = output
        else:
            # Generate filename from subject
            safe_subject = re.sub(r'[<>:"/\\|?*]', "-", sections[0].title)
            safe_subject = re.sub(r"\s+", "_", safe_subject)[:50]
            file_path = Path(f"{safe_subject}_{thread_id}.mail.gax.md")

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

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _pull_single_file(file_path: Path) -> tuple[int, int]:
    """Pull updates for a single .mail.gax.md file. Returns (old_count, new_count)."""
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


@thread.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def pull(path: Path):
    """Pull latest messages for .mail.gax.md file(s).

    Single file:

        gax mail thread pull thread.mail.gax.md

    Folder (updates all .mail.gax.md files):

        gax mail thread pull Inbox/
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
            # Folder mode: update all .mail.gax.md files
            files = list(path.glob("*.mail.gax.md"))
            if not files:
                click.echo(f"No .mail.gax.md files found in {path}")
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
    """Create filename: date-from-subject.mail.gax.md"""
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

    return f"{date_part}-{from_part}-{subject_part}.mail.gax.md"


def _get_existing_thread_ids(folder: Path) -> set[str]:
    """Get thread IDs already synced to folder."""
    if not folder.exists():
        return set()

    thread_ids = set()
    for f in folder.glob("*.mail.gax.md"):
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


def _list_threads(query: str, limit: int):
    """List threads matching query (TSV output)."""
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

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

        if total_estimate > limit:
            click.echo(
                f"# Found ~{total_estimate} threads, showing first {limit}", err=True
            )

        click.echo("thread_id\tdate\tfrom\tsubject")

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


# =============================================================================
# Reply command (creates draft from thread)
# =============================================================================


@thread.command()
@click.argument("file_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: Re_<subject>.draft.gax.md)",
)
def reply(file_or_url: str, output: Optional[Path]):
    """Create a reply draft from a thread.

    Takes a .mail.gax.md file or Gmail thread URL and creates a .draft.gax.md file
    with the reply metadata pre-filled.

    Examples:

        gax mail thread reply Project_Update.mail.gax.md
        gax mail thread reply "https://mail.google.com/mail/u/0/#inbox/abc123"
        gax mail thread reply thread.mail.gax.md -o my_reply.draft.gax.md
    """
    try:
        # Determine if input is a file or URL
        file_path = Path(file_or_url)

        if file_path.exists() and file_path.name.endswith(".gax.md"):
            # Parse .mail.gax.md file
            content = file_path.read_text(encoding="utf-8")
            sections = multipart.parse_multipart(content)

            if not sections:
                click.echo("Error: No sections found in file", err=True)
                sys.exit(1)

            # Get last message for reply
            last_section = sections[-1]
            thread_id = last_section.headers.get("thread_id", "")
            subject = last_section.headers.get("title", "")
            from_addr = last_section.headers.get("from", "")

            # For in_reply_to, we'd need message_id which isn't stored in .mail.gax.md
            # Use thread_id for threading
            in_reply_to = ""

        else:
            # Treat as URL or thread ID - fetch from Gmail
            thread_id = extract_thread_id(file_or_url)
            click.echo(f"Fetching thread: {thread_id}")

            sections = pull_thread(thread_id)
            if not sections:
                click.echo("Error: No messages found in thread", err=True)
                sys.exit(1)

            last_section = sections[-1]
            subject = last_section.title
            from_addr = last_section.from_addr
            in_reply_to = ""

        # Create reply subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        # Create draft config
        config = draft_module.DraftHeader(
            subject=subject,
            to=from_addr,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        body = "\n"  # Empty body

        draft_content = draft_module.format_draft(config, body)

        # Determine output filename
        if output:
            out_path = output
        else:
            safe_subject = re.sub(r'[<>:"/\\|?*]', "-", subject)
            safe_subject = re.sub(r"\s+", "_", safe_subject)[:50]
            out_path = Path(f"{safe_subject}.draft.gax.md")

        if out_path.exists():
            click.echo(f"Error: File already exists: {out_path}", err=True)
            sys.exit(1)

        out_path.write_text(draft_content, encoding="utf-8")
        click.echo(f"Created: {out_path}")
        click.echo(f"To: {from_addr}")
        click.echo(f"Subject: {subject}")
        click.echo(f"Edit the file, then run: gax mail draft push {out_path}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# Draft is now a top-level command (registered in cli.py)
# mail.add_command(draft_module.draft)  # Removed - see ADR 020


# =============================================================================
# List commands (search and bulk label operations)
# =============================================================================


@doc.section("resource")
@click.group(invoke_without_command=True)
@click.option(
    "-q", "--query", default="in:inbox", help="Search query (default: in:inbox)"
)
@click.option("--limit", default=20, help="Maximum results (default: 20)")
@click.pass_context
def mailbox(ctx, query: str, limit: int):
    """Search/list Gmail threads and bulk label operations.

    Without subcommand, lists threads matching query (TSV output).

    \b
    Examples:
        gax mailbox                        # List inbox
        gax mailbox -q "from:alice"        # Search
        gax mailbox clone                  # Clone for bulk labeling
    """
    if ctx.invoked_subcommand is None:
        # Default action: search/list threads
        _list_threads(query, limit)


# System label abbreviations (token-efficient)
SYS_LABEL_TO_ABBREV = {
    "INBOX": "I",
    "SPAM": "S",
    "TRASH": "T",
    "UNREAD": "U",
    "STARRED": "*",
    "IMPORTANT": "!",
}
ABBREV_TO_SYS_LABEL = {v: k for k, v in SYS_LABEL_TO_ABBREV.items()}

# Category abbreviations (mutually exclusive)
CAT_LABEL_TO_ABBREV = {
    "CATEGORY_PERSONAL": "P",
    "CATEGORY_UPDATES": "U",
    "CATEGORY_PROMOTIONS": "R",
    "CATEGORY_SOCIAL": "S",
    "CATEGORY_FORUMS": "F",
}
ABBREV_TO_CAT_LABEL = {v: k for k, v in CAT_LABEL_TO_ABBREV.items()}

# System labels to track (others like SENT, DRAFT are ignored)
TRACKED_SYS_LABELS = set(SYS_LABEL_TO_ABBREV.keys())
TRACKED_CAT_LABELS = set(CAT_LABEL_TO_ABBREV.keys())


def _get_thread_for_relabel(thread_id: str, service, label_id_to_name: dict) -> dict:
    """Get thread info for relabel output.

    Args:
        thread_id: Gmail thread ID
        service: Gmail API service
        label_id_to_name: Mapping from label ID to name

    Returns:
        Dict with 'sys', 'cat', and 'labels'
    """
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
        return {
            "id": thread_id,
            "sys": "",
            "cat": "",
            "labels": [],
            "from": "",
            "subject": "",
            "date": "",
            "snippet": "",
        }

    # Collect all labels from all messages in thread
    label_ids = set()
    for msg in messages:
        label_ids.update(msg.get("labelIds", []))

    # Separate system labels, category, and user labels
    sys_abbrevs = []
    cat_abbrev = ""
    user_labels = []
    for lid in label_ids:
        if lid in TRACKED_SYS_LABELS:
            sys_abbrevs.append(SYS_LABEL_TO_ABBREV[lid])
        elif lid in TRACKED_CAT_LABELS:
            cat_abbrev = CAT_LABEL_TO_ABBREV[lid]
        elif lid not in {"SENT", "DRAFT", "CHAT"}:
            # User label - convert ID to name
            name = label_id_to_name.get(lid, lid)
            user_labels.append(name)

    # Sort abbreviations in consistent order: I S T U * !
    abbrev_order = "ISTU*!"
    sys_abbrevs.sort(key=lambda x: abbrev_order.index(x) if x in abbrev_order else 99)

    # Get first message headers
    first_msg = messages[0]
    headers = first_msg.get("payload", {}).get("headers", [])
    snippet = first_msg.get("snippet", "")[:80]

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
        "id": thread_id,
        "sys": "".join(sys_abbrevs),
        "cat": cat_abbrev,
        "labels": sorted(user_labels),
        "from": from_email,
        "subject": subject[:60],
        "date": date_short,
        "snippet": snippet,
    }


def _tsv_quote(value: str) -> str:
    """Quote a TSV field if it contains special characters."""
    if "\t" in value or "\n" in value or '"' in value:
        return '"' + value.replace('"', '""') + '"'
    return value


def _relabel_fetch_threads(
    service, query: str, limit: int, label_id_to_name: dict
) -> list[dict]:
    """Fetch threads for relabeling."""
    threads = []
    page_token = None

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

        batch = result.get("threads", [])
        threads.extend(batch)

        page_token = result.get("nextPageToken")
        if not page_token or not batch:
            break

    threads = threads[:limit]

    # Get details for each thread
    thread_data = []
    for thread_info in threads:
        try:
            data = _get_thread_for_relabel(thread_info["id"], service, label_id_to_name)
            thread_data.append(data)
        except Exception as e:
            click.echo(f"# Error fetching {thread_info['id']}: {e}", err=True)

    return thread_data


def _write_gax_file(path: Path, query: str, limit: int, thread_data: list[dict]):
    """Write threads to .gax.md file with YAML header and TSV content."""
    # Build TSV content first to get content-length
    tsv_lines = ["id\tfrom\tsubject\tdate\tsys\tcat\tlabels"]
    for t in thread_data:
        from_q = _tsv_quote(t["from"])
        subject_q = _tsv_quote(t["subject"])
        labels_str = ",".join(t["labels"]) if t["labels"] else ""
        tsv_lines.append(
            f"{t['id']}\t{from_q}\t{subject_q}\t{t['date']}\t{t['sys']}\t{t['cat']}\t{labels_str}"
        )
    tsv_content = "\n".join(tsv_lines) + "\n"
    content_length = len(tsv_content.encode("utf-8"))

    with open(path, "w") as f:
        # YAML header
        f.write("---\n")
        f.write("type: gax/list\n")
        f.write(
            f"pulled: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        )
        f.write(f"query: {query}\n")
        f.write(f"limit: {limit}\n")
        f.write("columns:\n")
        f.write("  sys: I=Inbox S=Spam T=Trash U=Unread *=Starred !=Important\n")
        f.write("  cat: P=Personal U=Updates R=Promotions S=Social F=Forums\n")
        f.write("  labels: user labels (comma-sep, nesting with /)\n")
        f.write("content-type: text/tab-separated-values\n")
        f.write(f"content-length: {content_length}\n")
        f.write("---\n")
        # TSV content
        f.write(tsv_content)


def _parse_gax_header(path: Path) -> dict:
    """Parse YAML header from .gax.md file to get query and limit."""
    header = {"query": None, "limit": 50}
    with open(path) as f:
        content = f.read()

    # Parse YAML header between --- markers
    if not content.startswith("---\n"):
        return header

    # Find closing ---
    header_end = content.find("\n---\n", 4)
    if header_end == -1:
        return header

    header_text = content[4:header_end]
    for line in header_text.split("\n"):
        if line.startswith("query:"):
            header["query"] = line.split(":", 1)[1].strip()
        elif line.startswith("limit:"):
            try:
                header["limit"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass

    return header


def _parse_gax_content(path: Path) -> str:
    """Extract TSV content from .gax.md file (skip YAML header)."""
    with open(path) as f:
        content = f.read()

    if not content.startswith("---\n"):
        return content

    # Find closing ---
    header_end = content.find("\n---\n", 4)
    if header_end == -1:
        return content

    return content[header_end + 5 :]  # Skip \n---\n


@mailbox.command("fetch")
@click.option(
    "-o",
    "--output",
    default="mailbox.gax.md.d",
    type=click.Path(path_type=Path),
    help="Output folder (default: mailbox.gax.md.d)",
)
@click.option(
    "-q", "--query", default="in:inbox", help="Search query (default: in:inbox)"
)
@click.option("--limit", default=50, help="Maximum threads (default: 50)")
def mailbox_fetch(output: Path, query: str, limit: int):
    """Fetch full threads matching query into a folder.

    Searches Gmail and retrieves each matching thread as a full .mail.gax.md file.
    Incremental: skips existing threads.

    \b
    Examples:
        gax mailbox fetch
        gax mailbox fetch -o Inbox/ -q "in:inbox"
        gax mailbox fetch -o Alice/ -q "from:alice" --limit 100

    \b
    Workflow:
        1. fetch -> retrieve full threads from Gmail to folder
        2. grep/search folder contents as needed
    """
    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Search threads
        click.echo(f"Searching: {query}")
        threads = []
        page_token = None

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

            batch = result.get("threads", [])
            threads.extend(batch)

            page_token = result.get("nextPageToken")
            if not page_token or not batch:
                break

        threads = threads[:limit]

        if not threads:
            click.echo("No threads found.", err=True)
            sys.exit(1)

        thread_ids = [t["id"] for t in threads]
        click.echo(f"Found {len(thread_ids)} threads")

        # Create output folder
        output.mkdir(parents=True, exist_ok=True)

        # Get already cloned thread IDs
        existing_ids = _get_existing_thread_ids(output)

        # Clone each thread
        cloned = 0
        skipped = 0

        for thread_id in thread_ids:
            if thread_id in existing_ids:
                skipped += 1
                continue

            try:
                sections = pull_thread(thread_id)
                content = format_multipart(sections)

                # Generate filename
                first = sections[0]
                filename = _make_filename(
                    first.date, first.from_addr, first.title, thread_id
                )
                file_path = output / filename

                # Avoid overwriting
                if file_path.exists():
                    base = file_path.stem
                    file_path = output / f"{base}_{thread_id}.mail.gax.md"

                file_path.write_text(content, encoding="utf-8")
                cloned += 1
                click.echo(f"  {filename}")

            except Exception as e:
                click.echo(f"  Error cloning {thread_id}: {e}", err=True)

        click.echo(f"Checked out: {cloned}, Skipped: {skipped} (already present)")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@mailbox.command("clone")
@click.option(
    "-o",
    "--output",
    default="mailbox.gax.md",
    help="Output file (default: mailbox.gax.md)",
)
@click.option(
    "-q", "--query", default="in:inbox", help="Search query (default: in:inbox)"
)
@click.option("--limit", default=50, help="Maximum threads (default: 50)")
def mailbox_clone(output: str, query: str, limit: int):
    """Clone threads from Gmail for bulk labeling.

    Creates a .gax.md file with current state. Use 'pull' to update,
    'plan' to compute changes, 'apply' to execute.

    \b
    Columns:
      sys:    I=Inbox S=Spam T=Trash U=Unread *=Starred !=Important
      cat:    P=Personal U=Updates R=Promotions S=Social F=Forums
      labels: User labels (comma-separated, nesting with /)

    \b
    Examples:
        gax mailbox clone
        gax mailbox clone -o inbox.gax.md -q "in:inbox"
        gax mailbox clone -o spam.gax.md -q "in:spam" --limit 100

    \b
    Workflow:
        1. clone  -> create .gax.md file with current state
        2. pull   -> update .gax.md file (re-fetch)
        3. edit   -> change sys/cat/labels to desired state
        4. plan   -> compute diff
        5. apply  -> execute changes
    """
    output_path = Path(output)

    # Check for existing file
    if output_path.exists():
        click.echo(f"Error: {output} already exists. Use 'pull' to update.", err=True)
        sys.exit(1)

    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Get all labels for ID to name mapping
        labels_result = service.users().labels().list(userId="me").execute()
        label_id_to_name = {}
        for label in labels_result.get("labels", []):
            label_id_to_name[label["id"]] = label["name"]

        thread_data = _relabel_fetch_threads(service, query, limit, label_id_to_name)

        _write_gax_file(output_path, query, limit, thread_data)

        click.echo(f"Cloned {len(thread_data)} threads to {output}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@mailbox.command("pull")
@click.argument("file", type=click.Path(exists=True))
def relabel_pull(file: str):
    """Update a .gax.md file by re-fetching from Gmail.

    Reads the query from the file header and fetches fresh data.

    \b
    Example:
        gax mail list pull inbox.gax.md
    """
    path = Path(file)

    # Parse header to get query
    header = _parse_gax_header(path)
    if not header["query"]:
        click.echo(f"Error: No query found in {file} header", err=True)
        sys.exit(1)

    query = header["query"]
    limit = header["limit"]

    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Get all labels for ID to name mapping
        labels_result = service.users().labels().list(userId="me").execute()
        label_id_to_name = {}
        for label in labels_result.get("labels", []):
            label_id_to_name[label["id"]] = label["name"]

        thread_data = _relabel_fetch_threads(service, query, limit, label_id_to_name)

        _write_gax_file(path, query, limit, thread_data)

        click.echo(f"Pulled {len(thread_data)} threads to {file}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _parse_tsv_line(line: str) -> list[str]:
    """Parse a TSV line, handling quoted fields."""
    fields = []
    current = ""
    in_quotes = False

    i = 0
    while i < len(line):
        c = line[i]
        if c == '"':
            if in_quotes and i + 1 < len(line) and line[i + 1] == '"':
                current += '"'
                i += 2
                continue
            in_quotes = not in_quotes
        elif c == "\t" and not in_quotes:
            fields.append(current)
            current = ""
        else:
            current += c
        i += 1

    fields.append(current)
    return fields


@mailbox.command("plan")
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    default="mailbox.plan.yaml",
    help="Output file (default: mailbox.plan.yaml)",
)
def mailbox_plan(file: str, output: str):
    """Generate plan from edited list file.

    Compares desired state (sys/cat/labels) with current state in Gmail.
    Outputs add/remove operations needed to reach desired state.

    \b
    Columns:
      sys:    I=Inbox S=Spam T=Trash U=Unread *=Starred !=Important
      cat:    P=Personal U=Updates R=Promotions S=Social F=Forums
      labels: User labels (comma-separated)

    Example:

        gax mail list plan inbox.gax.md
    """
    import yaml

    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Get label mappings
        labels_result = service.users().labels().list(userId="me").execute()
        label_name_to_id = {}
        label_id_to_name = {}
        for label in labels_result.get("labels", []):
            label_name_to_id[label["name"]] = label["id"]
            label_id_to_name[label["id"]] = label["name"]

        # Read TSV content (skip YAML header if present)
        tsv_content = _parse_gax_content(Path(file))
        lines = tsv_content.split("\n")

        # Parse header and data
        data_lines = []
        header = None
        for line in lines:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if header is None:
                header = _parse_tsv_line(line)
                continue
            data_lines.append(line)

        if not header:
            click.echo("Error: No header found in TSV", err=True)
            sys.exit(1)

        # Find column indices
        try:
            id_idx = header.index("id")
            sys_idx = header.index("sys")
            cat_idx = header.index("cat")
            labels_idx = header.index("labels")
        except ValueError as e:
            click.echo(f"Error: Missing required column: {e}", err=True)
            sys.exit(1)

        # Build changes
        changes = []
        errors = []

        for line in data_lines:
            if not line.strip():
                continue

            fields = _parse_tsv_line(line)
            # Require at least sys column (minimum useful data)
            if len(fields) <= sys_idx:
                continue

            thread_id = fields[id_idx].strip()
            desired_sys = fields[sys_idx].strip()
            desired_cat = fields[cat_idx].strip() if len(fields) > cat_idx else ""
            desired_labels_str = (
                fields[labels_idx].strip() if len(fields) > labels_idx else ""
            )

            if not thread_id:
                continue

            # Parse desired sys labels
            desired_sys_labels = set()
            for c in desired_sys:
                if c in ABBREV_TO_SYS_LABEL:
                    desired_sys_labels.add(ABBREV_TO_SYS_LABEL[c])

            # Parse desired category
            desired_cat_label = None
            if desired_cat and desired_cat in ABBREV_TO_CAT_LABEL:
                desired_cat_label = ABBREV_TO_CAT_LABEL[desired_cat]

            # Parse desired user labels
            desired_labels = set()
            if desired_labels_str:
                desired_labels = {
                    lbl.strip() for lbl in desired_labels_str.split(",") if lbl.strip()
                }

            # Get current labels from Gmail
            try:
                thread = (
                    service.users()
                    .threads()
                    .get(userId="me", id=thread_id, format="minimal")
                    .execute()
                )
            except Exception as e:
                errors.append(f"Cannot fetch thread {thread_id}: {e}")
                continue

            current_label_ids = set()
            for msg in thread.get("messages", []):
                current_label_ids.update(msg.get("labelIds", []))

            # Separate current labels
            current_sys_labels = current_label_ids & TRACKED_SYS_LABELS
            current_cat_labels = current_label_ids & TRACKED_CAT_LABELS
            current_cat_label = next(iter(current_cat_labels), None)
            current_user_labels = set()
            for lid in current_label_ids:
                if lid not in TRACKED_SYS_LABELS and lid not in TRACKED_CAT_LABELS:
                    if lid not in {"SENT", "DRAFT", "CHAT"}:
                        name = label_id_to_name.get(lid, lid)
                        current_user_labels.add(name)

            # Compute diffs
            sys_to_add = desired_sys_labels - current_sys_labels
            sys_to_remove = current_sys_labels - desired_sys_labels

            cat_to_add = None
            cat_to_remove = None
            if desired_cat_label not in current_cat_labels:
                if desired_cat_label:
                    cat_to_add = desired_cat_label
                if current_cat_label:
                    cat_to_remove = current_cat_label

            labels_to_add = desired_labels - current_user_labels
            # Also add parent labels for nested labels (hub/i/x → hub/i, hub)
            parents_to_add = set()
            for lbl in labels_to_add:
                parts = lbl.split("/")
                for i in range(1, len(parts)):
                    parent = "/".join(parts[:i])
                    if parent not in current_user_labels:
                        parents_to_add.add(parent)
            labels_to_add |= parents_to_add
            # Don't remove parents that are implied by desired labels
            desired_labels_expanded = set(desired_labels)
            for lbl in desired_labels:
                parts = lbl.split("/")
                for i in range(1, len(parts)):
                    desired_labels_expanded.add("/".join(parts[:i]))
            labels_to_remove = current_user_labels - desired_labels_expanded

            # Build change record
            change = {"id": thread_id}
            has_change = False

            # System label changes
            if sys_to_add:
                change["add_sys"] = sorted(sys_to_add)
                has_change = True
            if sys_to_remove:
                change["remove_sys"] = sorted(sys_to_remove)
                has_change = True

            # Category change
            if cat_to_add:
                change["add_cat"] = cat_to_add
                has_change = True
            if cat_to_remove:
                change["remove_cat"] = cat_to_remove
                has_change = True

            # User label changes
            if labels_to_add:
                change["add"] = sorted(labels_to_add)
                has_change = True
            if labels_to_remove:
                change["remove"] = sorted(labels_to_remove)
                has_change = True

            if has_change:
                changes.append(change)

        if errors:
            for err in errors:
                click.echo(f"Error: {err}", err=True)
            sys.exit(1)

        if not changes:
            click.echo("No changes to apply.")
            return

        # Write plan
        plan = {
            "source": file,
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "changes": changes,
        }

        path = Path(output)
        with open(path, "w") as f:
            yaml.dump(
                plan, f, default_flow_style=False, allow_unicode=True, sort_keys=False
            )

        # Summary
        sys_add_count = sum(1 for c in changes if c.get("add_sys"))
        sys_remove_count = sum(1 for c in changes if c.get("remove_sys"))
        cat_change_count = sum(
            1 for c in changes if c.get("add_cat") or c.get("remove_cat")
        )
        add_count = sum(1 for c in changes if c.get("add"))
        remove_count = sum(1 for c in changes if c.get("remove"))

        click.echo(f"Wrote {len(changes)} changes to {output}")
        if sys_add_count or sys_remove_count:
            click.echo(f"  System label changes: {sys_add_count + sys_remove_count}")
        if cat_change_count:
            click.echo(f"  Category changes: {cat_change_count}")
        if add_count:
            click.echo(f"  Add user labels: {add_count}")
        if remove_count:
            click.echo(f"  Remove user labels: {remove_count}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@mailbox.command("apply")
@click.argument("plan_file", type=click.Path(exists=True))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def relabel_apply(plan_file: str, yes: bool):
    """Apply label changes from plan.

    Reads the plan file and applies sys/cat/label changes.

    Example:

        gax mail list apply inbox.plan.yaml
    """
    import yaml

    try:
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        # Read plan
        with open(plan_file) as f:
            plan = yaml.safe_load(f)

        changes = plan.get("changes", [])
        if not changes:
            click.echo("No changes in plan.")
            return

        # Get label name -> id mapping
        labels_result = service.users().labels().list(userId="me").execute()
        label_map = {
            label["name"]: label["id"] for label in labels_result.get("labels", [])
        }

        # Find user labels that need to be created
        labels_to_create = set()
        for change in changes:
            for label_name in change.get("add", []):
                if label_name not in label_map:
                    labels_to_create.add(label_name)

        # Show summary
        click.echo(f"Plan: {plan_file}")
        click.echo(f"Changes: {len(changes)}")
        click.echo()

        for change in changes[:10]:
            thread_id = change["id"][:12] + "..."
            actions = []
            if change.get("add_sys"):
                actions.append("+sys:" + ",".join(change["add_sys"]))
            if change.get("remove_sys"):
                actions.append("-sys:" + ",".join(change["remove_sys"]))
            if change.get("add_cat"):
                actions.append("+cat:" + change["add_cat"])
            if change.get("remove_cat"):
                actions.append("-cat:" + change["remove_cat"])
            if change.get("add"):
                actions.append("+" + ",".join(change["add"]))
            if change.get("remove"):
                actions.append("-" + ",".join(change["remove"]))
            click.echo(f"  {thread_id}  {' '.join(actions)}")

        if len(changes) > 10:
            click.echo(f"  ... and {len(changes) - 10} more")

        if labels_to_create:
            click.echo()
            click.echo(f"Labels to create: {', '.join(sorted(labels_to_create))}")

        click.echo()

        if not yes and not click.confirm("Apply these changes?"):
            click.echo("Aborted.")
            return

        # Create missing user labels (with parent labels for nesting)
        if labels_to_create:
            with operation("Creating labels", total=len(labels_to_create)) as op:
                for label_name in sorted(labels_to_create):
                    try:
                        # For nested labels (with /), create parents first
                        if "/" in label_name:
                            parts = label_name.split("/")
                            for i in range(len(parts)):
                                partial = "/".join(parts[: i + 1])
                                if partial not in label_map:
                                    logger.info(f"Creating: {partial}")
                                    result = (
                                        service.users()
                                        .labels()
                                        .create(userId="me", body={"name": partial})
                                        .execute()
                                    )
                                    label_map[partial] = result["id"]
                        else:
                            if label_name not in label_map:
                                logger.info(f"Creating: {label_name}")
                                result = (
                                    service.users()
                                    .labels()
                                    .create(userId="me", body={"name": label_name})
                                    .execute()
                                )
                                label_map[label_name] = result["id"]
                    except Exception as e:
                        if "Label name exists" not in str(e):
                            error(f"Error creating label '{label_name}': {e}")
                    op.advance()

        # Apply changes
        succeeded = 0
        failed = 0

        with operation("Applying label changes", total=len(changes)) as op:
            for change in changes:
                thread_id = change["id"]
                try:
                    add_ids = []
                    remove_ids = []

                    # System labels to add
                    if change.get("add_sys"):
                        add_ids.extend(change["add_sys"])

                    # System labels to remove
                    if change.get("remove_sys"):
                        remove_ids.extend(change["remove_sys"])

                    # Category to add
                    if change.get("add_cat"):
                        add_ids.append(change["add_cat"])

                    # Category to remove
                    if change.get("remove_cat"):
                        remove_ids.append(change["remove_cat"])

                    # User labels to add
                    if change.get("add"):
                        add_ids.extend(label_map[name] for name in change["add"])

                    # User labels to remove
                    if change.get("remove"):
                        remove_ids.extend(label_map[name] for name in change["remove"])

                    modify_body = {}
                    if add_ids:
                        modify_body["addLabelIds"] = add_ids
                    if remove_ids:
                        modify_body["removeLabelIds"] = remove_ids

                    if modify_body:
                        logger.info(f"Thread {thread_id[:8]}...")
                        service.users().threads().modify(
                            userId="me",
                            id=thread_id,
                            body=modify_body,
                        ).execute()

                    succeeded += 1

                except Exception as e:
                    error(f"Error on {thread_id}: {e}")
                    failed += 1

                op.advance()

        success(f"Applied: {succeeded} threads")
        if failed:
            error(f"Failed: {failed} threads")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# All mail subcommands are now top-level (registered in cli.py) - see ADR 020
# - thread → mail (individual threads)
# - list → mailbox (thread collections)
# - draft → draft (top-level)
# - label → mail-label (top-level)
# - filter → mail-filter (top-level)
