"""Gmail thread resource for gax.

Resource module — follows the draft.py reference pattern.

Module structure
================

  _is_thread_id      — URL/ID classification helper
  _pull_single_file  — single-file pull helper
  Thread(Resource)   — resource class (the public interface for cli.py)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).
Additional notes specific to threads:

  Thread is read-only from Gmail's perspective: clone and pull fetch
  data, but there is no push. The reply command creates a draft (a
  different resource type), not a modified thread.

  reply() is a custom method, not a standard Resource operation. It
  returns a Path to the created draft file.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from .. import gaxfile
from . import draft as draft_module
from ..resource import Resource

from .shared import (
    extract_thread_id,
    fetch_message,
    message_to_reply_headers,
    pull_thread,
    format_multipart,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Helpers
# =============================================================================


def _is_thread_id(value: str) -> bool:
    """Check if value looks like a thread ID (vs a search query).

    Accepts:
      - Gmail URLs (mail.google.com)
      - 16-char hex IDs (e.g. 19f9a81e022b5536)
      - Large decimal IDs (15+ digits, from thread-f: URLs)

    Rejects opaque client-side tokens (FMfcg…, KtbxL…, etc.) and
    thread-a:r IDs that cannot be resolved to API thread IDs.
    """
    from .shared import _is_opaque_gmail_token, _is_thread_a_id

    if _is_thread_a_id(value):
        return False
    if "mail.google.com" in value:
        return True
    if _is_opaque_gmail_token(value):
        return False
    if re.fullmatch(r"[0-9a-f]{16}", value):
        return True
    if re.fullmatch(r"\d{15,}", value):
        return True
    return False


def _is_message_hex_id(value: str) -> bool:
    """Check if value looks like a 16-char hex message/thread ID."""
    return bool(re.fullmatch(r"[0-9a-f]{16}", value))


def _resolve_reply_target(input_value: str, *, service=None) -> dict:
    """Resolve a reply target from a message ID, thread ID, or URL.

    Returns a dict from message_to_reply_headers() with keys:
    to, subject, in_reply_to, references, thread_id, from_addr, id, reply_to.

    Resolution rules (ADR 038 §2):
    - 16-hex ID: try messages.get first; on 404, fall back to threads.get
      (last message). Anchor disambiguation: if message.id == message.threadId,
      treat as thread reference (reply to last message, not first).
    - Thread URL or non-hex ID: threads.get, reply to last message.
    """
    # Try as message first if it looks like a 16-hex ID
    if _is_message_hex_id(input_value):
        try:
            msg = fetch_message(input_value, service=service)
            # Anchor disambiguation: if msg.id == msg.threadId, this is the
            # first message (thread anchor). Treat as thread reference.
            if msg.get("id") == msg.get("threadId"):
                logger.info(
                    f"Message {input_value} is thread anchor, "
                    "replying to last message"
                )
                sections = pull_thread(msg["threadId"], service=service)
                last = sections[-1]
                return message_to_reply_headers({
                    "id": last.id,
                    "threadId": last.thread_id,
                    "payload": {
                        "headers": [
                            {"name": "From", "value": last.from_addr},
                            {"name": "Reply-To", "value": last.reply_to},
                            {"name": "Subject", "value": last.title},
                            {"name": "Message-Id", "value": last.message_id},
                            {"name": "References", "value": last.references},
                        ]
                    },
                })
            # Non-anchor message: reply directly to this message
            return message_to_reply_headers(msg)
        except Exception:
            # messages.get failed — fall back to threads.get
            logger.info(
                f"messages.get failed for {input_value}, "
                "trying as thread ID"
            )

    # Thread resolution: extract thread ID and reply to last message
    thread_id = extract_thread_id(input_value)
    sections = pull_thread(thread_id, service=service)
    if not sections:
        raise ValueError(f"No messages found in thread {thread_id}")
    last = sections[-1]
    return message_to_reply_headers({
        "id": last.id,
        "threadId": last.thread_id,
        "payload": {
            "headers": [
                {"name": "From", "value": last.from_addr},
                {"name": "Reply-To", "value": last.reply_to},
                {"name": "Subject", "value": last.title},
                {"name": "Message-Id", "value": last.message_id},
                {"name": "References", "value": last.references},
            ]
        },
    })


def _pull_single_file(file_path: Path) -> tuple[int, int]:
    """Pull updates for a single .mail.gax.md file. Returns (old_count, new_count)."""
    content = file_path.read_text(encoding="utf-8")

    match = re.search(r"^thread_id:\s*(\S+)", content, re.MULTILINE)
    if not match:
        raise ValueError(f"No thread_id found in {file_path}")

    thread_id = match.group(1)
    old_count = len(re.findall(r"^section:\s*\d+", content, re.MULTILINE))

    sections = pull_thread(thread_id)
    new_content = format_multipart(sections)
    new_count = len(sections)

    file_path.write_text(new_content, encoding="utf-8")
    return old_count, new_count


# =============================================================================
# Resource class — the public interface for cli.py.
# =============================================================================


class Thread(Resource):
    """Gmail thread resource.

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "thread"
    FILE_TYPE = "gax/mail"
    FILE_EXTENSIONS = (".mail.gax.md",)
    SCOPES = ("gmail.readonly", "gmail.compose")

    @classmethod
    def from_url(cls, url: str) -> "Thread":
        """Construct from a Gmail thread URL."""
        # Must NOT match draft URLs
        if re.search(r"mail\.google\.com/mail/", url) and "#drafts/" not in url:
            return cls(url=url)
        raise ValueError(f"Not a Gmail thread URL: {url}")

    @classmethod
    def from_id(cls, id_value: str) -> "Thread":
        """Construct from a Gmail thread ID."""
        if _is_thread_id(id_value):
            return cls(url=id_value)
        raise ValueError(f"Not a Gmail thread ID: {id_value}")

    def _output_path(self, subject: str, thread_id: str, output: Path | None) -> Path:
        if output:
            return output
        safe = re.sub(r'[<>:"/\\|?*]', "-", subject or "untitled")
        safe = re.sub(r"\s+", "_", safe)[:50]
        return Path(f"{safe}_{thread_id}.mail.gax.md")

    def get(self, **kw) -> str:
        """Fetch thread content and return as string. Read-only, no side effects.

        Returns the same multipart markdown that clone writes to a file —
        YAML headers with From/Date per message, body content below each.

        Works from URL (thread ID or Gmail URL) or from a .mail.gax.md file
        (reads thread_id from the YAML header).
        """
        if self.path.name.endswith(".mail.gax.md") and self.path.exists():
            # File-based: read thread_id from YAML header
            content = self.path.read_text(encoding="utf-8")
            match = re.search(r"^thread_id:\s*(\S+)", content, re.MULTILINE)
            if not match:
                raise ValueError(f"No thread_id found in {self.path}")
            thread_id = match.group(1)
        else:
            # URL-based: extract thread_id from URL or raw ID
            thread_id = extract_thread_id(self.url)

        sections = pull_thread(thread_id)
        return format_multipart(sections)

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Clone a single email thread to a local file. Returns path created."""
        if not _is_thread_id(self.url):
            raise ValueError(
                f"'{self.url}' is not a valid thread ID or URL.\n"
                "For bulk cloning, use: gax mailbox fetch -q QUERY"
            )

        thread_id = extract_thread_id(self.url)
        logger.info(f"Fetching thread: {thread_id}")

        sections = pull_thread(thread_id)
        content = format_multipart(sections)

        file_path = self._output_path(sections[0].title, thread_id, output)
        if file_path.exists():
            raise ValueError(
                f"File already exists: {file_path}\n"
                'Use "gax pull" to update an existing file.'
            )

        file_path.write_text(content, encoding="utf-8")
        logger.info(f"Subject: {sections[0].title}, Messages: {len(sections)}")

        total_att = sum(len(s.attachments) for s in sections)
        if total_att:
            logger.info(f"Attachments: {total_att}")

        return file_path

    def pull(self, **kw) -> None:
        """Pull latest messages for a .mail.gax.md file or folder."""
        path = self.path
        if path.is_dir():
            files = list(path.glob("*.mail.gax.md"))
            if not files:
                raise ValueError(f"No .mail.gax.md files found in {path}")

            logger.info(f"Updating {len(files)} files in {path}/")
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
                        logger.info(f"{file_path.name}: +{new_messages} messages")
                except Exception as e:
                    errors += 1
                    logger.warning(f"{file_path.name}: Error - {e}")

            logger.info(f"Updated: {updated}, Errors: {errors}")
            if total_new > 0:
                logger.info(f"Total new messages: {total_new}")
        else:
            old_count, new_count = _pull_single_file(path)
            logger.info(f"Messages: {old_count} -> {new_count}")

    def diff(self, **kw) -> str | None:
        """Compare local file with remote thread.

        Returns human-readable summary of differences, or None if unchanged.
        For threads this means checking for new messages in the conversation.
        """
        path = self.path
        content = path.read_text(encoding="utf-8")

        match = re.search(r"^thread_id:\s*(\S+)", content, re.MULTILINE)
        if not match:
            raise ValueError(f"No thread_id found in {path}")
        thread_id = match.group(1)

        local_sections = gaxfile.parse_multipart(content)
        remote_sections = pull_thread(thread_id)

        local_count = len(local_sections)
        remote_count = len(remote_sections)

        lines: list[str] = []

        if remote_count != local_count:
            lines.append(f"Messages: {local_count} -> {remote_count}")

        # Preview new messages
        if remote_count > local_count:
            lines.append("")
            for section in remote_sections[local_count:]:
                lines.append(f"  From: {section.from_addr}")
                lines.append(f"  Date: {section.date}")
                preview = section.content.strip()
                if len(preview) > 200:
                    preview = preview[:200] + "..."
                lines.append(f"  {preview}")
                lines.append("")

        # Check for content changes in existing messages (rare for Gmail)
        for i, (local_sec, remote_sec) in enumerate(
            zip(local_sections, remote_sections)
        ):
            if local_sec.content.strip() != remote_sec.content.strip():
                lines.append(f"Message {i + 1}: content changed")

        return "\n".join(lines).rstrip() if lines else None

    def reply(self, output: Path | None = None) -> Path:
        """Create a reply draft from a message ID, thread file, or URL.

        Resolution (ADR 038 §2):
        - File path: derive headers from last section in the file.
        - 16-hex message ID: messages.get; anchor (id==threadId) → last msg.
        - Thread URL/ID: threads.get, reply to last message.

        Headers derive from the target message only (ADR 038 §3):
        - to = Reply-To or From
        - subject = Re: + target's Subject
        - in_reply_to = target's Message-ID
        - references = target's References + target's Message-ID
        """
        if self.path and self.path.exists() and self.path.name.endswith(".gax.md"):
            # File-based reply: derive from last section
            content = self.path.read_text(encoding="utf-8")
            sections = gaxfile.parse_multipart(content)
            if not sections:
                raise ValueError("No sections found in file")

            last = sections[-1]
            h = last.headers
            reply_to_addr = h.get("reply_to", "")
            from_addr = h.get("from", "")
            to = reply_to_addr or from_addr
            subject = h.get("title", "")
            rfc_message_id = h.get("message_id", "")
            rfc_references = h.get("references", "")
            thread_id = h.get("thread_id", "")

            # References = target's References + target's Message-ID
            ref_parts = rfc_references.split() if rfc_references else []
            if rfc_message_id:
                ref_parts.append(rfc_message_id)

            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"

            target = {
                "to": to,
                "subject": subject,
                "in_reply_to": rfc_message_id,
                "references": " ".join(ref_parts),
                "thread_id": thread_id,
            }
        else:
            # URL/ID-based reply: resolve via API
            target = _resolve_reply_target(self.url)

        config = draft_module.DraftHeader(
            subject=target["subject"],
            to=target["to"],
            thread_id=target["thread_id"],
            in_reply_to=target["in_reply_to"],
            references=target["references"],
            time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        draft_content = draft_module.format_draft(config, "\n")

        if output:
            out_path = output
        else:
            safe_subject = re.sub(r'[<>:"/\\|?*]', "-", target["subject"])
            safe_subject = re.sub(r"\s+", "_", safe_subject)[:50]
            out_path = Path(f"{safe_subject}.draft.gax.md")

        if out_path.exists():
            raise ValueError(f"File already exists: {out_path}")

        out_path.write_text(draft_content, encoding="utf-8")
        logger.info(f"To: {target['to']}, Subject: {target['subject']}")
        return out_path


Resource.register(Thread)
