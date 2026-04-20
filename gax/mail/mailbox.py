"""Gmail mailbox operations for gax.

Handles thread listing, bulk cloning, and declarative label management.

Module structure
================

  Label constants      — abbreviation mappings for sys/cat labels
  TSV helpers          — quote, parse TSV lines
  File format          — read/write .gax.md list files with YAML header + TSV
  API helpers          — fetch threads, get thread details for relabel
  Mailbox              — class encapsulating all mailbox operations

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).

  Mailbox inherits from Resource and follows the constructor pattern
  (from_file, from_url). It also has domain-specific methods beyond
  the standard Resource interface: list, compute_plan, apply_plan, fetch.

  The plan/apply workflow uses an intermediate YAML file. compute_plan
  returns the plan dict; apply_plan takes the plan dict. cli.py handles
  writing/reading the YAML file and confirmation prompts.
"""

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

from ..gaxfile import GaxFile
from ..auth import get_authenticated_credentials
from ..resource import Resource

from .shared import (
    _get_header,
    pull_thread,
    format_multipart,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Label constants
# =============================================================================

SYS_LABEL_TO_ABBREV = {
    "INBOX": "I",
    "SPAM": "S",
    "TRASH": "T",
    "UNREAD": "U",
    "STARRED": "*",
    "IMPORTANT": "!",
}
ABBREV_TO_SYS_LABEL = {v: k for k, v in SYS_LABEL_TO_ABBREV.items()}

CAT_LABEL_TO_ABBREV = {
    "CATEGORY_PERSONAL": "P",
    "CATEGORY_UPDATES": "U",
    "CATEGORY_PROMOTIONS": "R",
    "CATEGORY_SOCIAL": "S",
    "CATEGORY_FORUMS": "F",
}
ABBREV_TO_CAT_LABEL = {v: k for k, v in CAT_LABEL_TO_ABBREV.items()}

TRACKED_SYS_LABELS = set(SYS_LABEL_TO_ABBREV.keys())
TRACKED_CAT_LABELS = set(CAT_LABEL_TO_ABBREV.keys())


# =============================================================================
# TSV helpers
# =============================================================================


def _tsv_quote(value: str) -> str:
    """Quote a TSV field if it contains special characters."""
    if "\t" in value or "\n" in value or '"' in value:
        return '"' + value.replace('"', '""') + '"'
    return value


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


# =============================================================================
# File format — read/write .gax.md list files
# =============================================================================


def _write_gax_file(path: Path, query: str, limit: int, thread_data: list[dict]):
    """Write threads to .gax.md file with YAML header and TSV content."""
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
        f.write(tsv_content)


def _parse_gax_header(path: Path) -> dict:
    """Parse YAML header from .gax.md file to get query and limit."""
    try:
        gf = GaxFile.from_path(path, multipart=False)
    except ValueError:
        return {"query": None, "limit": 50}
    try:
        limit = int(gf.headers.get("limit", 50))
    except (ValueError, TypeError):
        limit = 50
    return {
        "query": gf.headers.get("query"),
        "limit": limit,
    }


def _parse_gax_content(path: Path) -> str:
    """Extract TSV content from .gax.md file (skip YAML header)."""
    try:
        gf = GaxFile.from_path(path, multipart=False)
    except ValueError:
        return path.read_text(encoding="utf-8")
    return gf.body


# =============================================================================
# API helpers
# =============================================================================


def _make_filename(date_str: str, from_addr: str, subject: str) -> str:
    """Create filename: date-from-subject.mail.gax.md"""
    try:
        dt = parsedate_to_datetime(date_str)
        date_part = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        if "T" in date_str:
            date_part = date_str.split("T")[0]
        else:
            date_part = "unknown-date"

    email_match = re.search(r"<([^>]+)>", from_addr)
    if email_match:
        from_part = email_match.group(1)
    else:
        from_part = from_addr.split()[0] if from_addr else "unknown"

    from_part = re.sub(r'[<>:"/\\|?*\s]', "", from_part)[:30]

    subject_part = re.sub(r'[<>:"/\\|?*]', "-", subject)
    subject_part = re.sub(r"\s+", "_", subject_part)[:40]

    return f"{date_part}-{from_part}-{subject_part}.mail.gax.md"


def _get_existing_thread_ids(folder: Path) -> set[str]:
    """Get thread IDs already synced to folder."""
    if not folder.exists():
        return set()

    thread_ids = set()
    for f in folder.glob("*.mail.gax.md"):
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

    headers = messages[0].get("payload", {}).get("headers", [])

    from_addr = _get_header(headers, "From")
    subject = _get_header(headers, "Subject")
    date_str = _get_header(headers, "Date")

    email_match = re.search(r"<([^>]+)>", from_addr)
    if email_match:
        from_email = email_match.group(1)
    else:
        from_email = from_addr.split()[0] if from_addr else ""

    try:
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


def _get_thread_for_relabel(thread_id: str, service, label_id_to_name: dict) -> dict:
    """Get thread info for relabel output."""
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

    label_ids = set()
    for msg in messages:
        label_ids.update(msg.get("labelIds", []))

    sys_abbrevs = []
    cat_abbrev = ""
    user_labels = []
    for lid in label_ids:
        if lid in TRACKED_SYS_LABELS:
            sys_abbrevs.append(SYS_LABEL_TO_ABBREV[lid])
        elif lid in TRACKED_CAT_LABELS:
            cat_abbrev = CAT_LABEL_TO_ABBREV[lid]
        elif lid not in {"SENT", "DRAFT", "CHAT"}:
            name = label_id_to_name.get(lid, lid)
            user_labels.append(name)

    abbrev_order = "ISTU*!"
    sys_abbrevs.sort(key=lambda x: abbrev_order.index(x) if x in abbrev_order else 99)

    first_msg = messages[0]
    headers = first_msg.get("payload", {}).get("headers", [])
    snippet = first_msg.get("snippet", "")[:80]

    from_addr = _get_header(headers, "From")
    subject = _get_header(headers, "Subject")
    date_str = _get_header(headers, "Date")

    email_match = re.search(r"<([^>]+)>", from_addr)
    if email_match:
        from_email = email_match.group(1)
    else:
        from_email = from_addr.split()[0] if from_addr else ""

    try:
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

    thread_data = []
    for thread_info in threads:
        try:
            data = _get_thread_for_relabel(thread_info["id"], service, label_id_to_name)
            thread_data.append(data)
        except Exception as e:
            logger.warning(f"Error fetching {thread_info['id']}: {e}")

    return thread_data


def _get_label_mappings(service) -> tuple[dict, dict]:
    """Get label ID-to-name and name-to-ID mappings. Returns (id_to_name, name_to_id)."""
    labels_result = service.users().labels().list(userId="me").execute()
    id_to_name = {}
    name_to_id = {}
    for label in labels_result.get("labels", []):
        id_to_name[label["id"]] = label["name"]
        name_to_id[label["name"]] = label["id"]
    return id_to_name, name_to_id


# =============================================================================
# Mailbox class — the public interface for cli.py.
# =============================================================================


class Mailbox(Resource):
    """Gmail mailbox operations — thread list management.

    Constructed via from_file(path) for file-based operations,
    or directly for list/clone/fetch operations.
    """

    name = "mailbox"
    FILE_TYPE = "gax/list"

    @classmethod
    def from_file(cls, path: Path) -> "Mailbox":
        """Construct from a mailbox list file."""
        name = path.name.lower()
        # Check for mailbox extension
        if name.endswith(".mailbox.gax.md"):
            return cls(path=path)
        # Check YAML header for type or query field
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            raise ValueError(f"Cannot read: {path}")
        if content.startswith("---"):
            for line in content.split("\n"):
                if line.startswith("type:"):
                    file_type = line.split(":", 1)[1].strip()
                    if file_type == "gax/list":
                        return cls(path=path)
                    break
                if line.startswith("query:"):
                    return cls(path=path)
                if line == "---" and content.index(line) > 0:
                    break
        raise ValueError(f"Not a mailbox file: {path}")

    def list(self, out, *, query: str = "in:inbox", limit: int = 20) -> None:
        """List threads matching query as TSV to file descriptor."""
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
            raise ValueError("No threads found.")

        if total_estimate > limit:
            logger.info(f"Found ~{total_estimate} threads, showing first {limit}")

        out.write("thread_id\tdate\tfrom\tsubject\n")

        for thread_info in threads:
            thread_id = thread_info["id"]
            try:
                summary = _get_thread_summary(thread_id, service)
                out.write(
                    f"{summary['thread_id']}\t{summary['date']}\t"
                    f"{summary['from']}\t{summary['subject']}\n"
                )
            except Exception as e:
                logger.warning(f"Error fetching {thread_id}: {e}")

    def clone(
        self, *, query: str = "in:inbox", limit: int = 50, output: Path | None = None
    ) -> Path:
        """Clone thread list from Gmail to a local .gax.md file. Returns path."""
        output_path = output or Path("mailbox.gax.md")
        if output_path.exists():
            raise ValueError(f"{output_path} already exists. Use 'pull' to update.")

        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        label_id_to_name, _ = _get_label_mappings(service)
        thread_data = _relabel_fetch_threads(service, query, limit, label_id_to_name)

        _write_gax_file(output_path, query, limit, thread_data)
        logger.info(f"Cloned {len(thread_data)} threads")
        return output_path

    def pull(self, **kw) -> None:
        """Re-fetch thread list from Gmail."""
        path = self.path
        header = _parse_gax_header(path)
        if not header["query"]:
            raise ValueError(f"No query found in {path} header")

        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        label_id_to_name, _ = _get_label_mappings(service)
        thread_data = _relabel_fetch_threads(
            service, header["query"], header["limit"], label_id_to_name
        )

        _write_gax_file(path, header["query"], header["limit"], thread_data)
        logger.info(f"Pulled {len(thread_data)} threads")

    def compute_plan(self) -> dict:
        """Compute label changes between local file and Gmail.

        Returns plan dict with 'source', 'generated', 'changes' keys.
        Raises ValueError on parsing errors or API failures.
        """
        path = self.path
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        label_id_to_name, _ = _get_label_mappings(service)

        tsv_content = _parse_gax_content(path)
        lines = tsv_content.split("\n")

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
            raise ValueError("No header found in TSV")

        try:
            id_idx = header.index("id")
            sys_idx = header.index("sys")
            cat_idx = header.index("cat")
            labels_idx = header.index("labels")
        except ValueError as e:
            raise ValueError(f"Missing required column: {e}") from e

        changes = []
        errors = []

        for line in data_lines:
            if not line.strip():
                continue

            fields = _parse_tsv_line(line)
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

            desired_sys_labels = set()
            for c in desired_sys:
                if c in ABBREV_TO_SYS_LABEL:
                    desired_sys_labels.add(ABBREV_TO_SYS_LABEL[c])

            desired_cat_label = None
            if desired_cat and desired_cat in ABBREV_TO_CAT_LABEL:
                desired_cat_label = ABBREV_TO_CAT_LABEL[desired_cat]

            desired_labels = set()
            if desired_labels_str:
                desired_labels = {
                    lbl.strip() for lbl in desired_labels_str.split(",") if lbl.strip()
                }

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

            current_sys_labels = current_label_ids & TRACKED_SYS_LABELS
            current_cat_labels = current_label_ids & TRACKED_CAT_LABELS
            current_cat_label = next(iter(current_cat_labels), None)
            current_user_labels = set()
            for lid in current_label_ids:
                if lid not in TRACKED_SYS_LABELS and lid not in TRACKED_CAT_LABELS:
                    if lid not in {"SENT", "DRAFT", "CHAT"}:
                        name = label_id_to_name.get(lid, lid)
                        current_user_labels.add(name)

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
            parents_to_add = set()
            for lbl in labels_to_add:
                parts = lbl.split("/")
                for i in range(1, len(parts)):
                    parent = "/".join(parts[:i])
                    if parent not in current_user_labels:
                        parents_to_add.add(parent)
            labels_to_add |= parents_to_add
            desired_labels_expanded = set(desired_labels)
            for lbl in desired_labels:
                parts = lbl.split("/")
                for i in range(1, len(parts)):
                    desired_labels_expanded.add("/".join(parts[:i]))
            labels_to_remove = current_user_labels - desired_labels_expanded

            change: dict[str, Any] = {"id": thread_id}
            has_change = False

            if sys_to_add:
                change["add_sys"] = sorted(sys_to_add)
                has_change = True
            if sys_to_remove:
                change["remove_sys"] = sorted(sys_to_remove)
                has_change = True
            if cat_to_add:
                change["add_cat"] = cat_to_add
                has_change = True
            if cat_to_remove:
                change["remove_cat"] = cat_to_remove
                has_change = True
            if labels_to_add:
                change["add"] = sorted(labels_to_add)
                has_change = True
            if labels_to_remove:
                change["remove"] = sorted(labels_to_remove)
                has_change = True

            if has_change:
                changes.append(change)

        if errors:
            raise ValueError("\n".join(errors))

        return {
            "source": str(path),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "changes": changes,
        }

    def apply_plan(self, plan: dict) -> tuple[int, int]:
        """Apply label changes from plan dict.

        Creates missing labels automatically.
        Returns (succeeded, failed).
        """
        changes = plan.get("changes", [])
        if not changes:
            return 0, 0

        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        labels_result = service.users().labels().list(userId="me").execute()
        label_map = {
            label["name"]: label["id"] for label in labels_result.get("labels", [])
        }

        # Create missing labels
        for change in changes:
            for label_name in change.get("add", []):
                if label_name not in label_map:
                    if "/" in label_name:
                        parts = label_name.split("/")
                        for i in range(len(parts)):
                            partial = "/".join(parts[: i + 1])
                            if partial not in label_map:
                                logger.info(f"Creating label: {partial}")
                                try:
                                    result = (
                                        service.users()
                                        .labels()
                                        .create(userId="me", body={"name": partial})
                                        .execute()
                                    )
                                    label_map[partial] = result["id"]
                                except Exception as e:
                                    if "Label name exists" not in str(e):
                                        raise
                    else:
                        logger.info(f"Creating label: {label_name}")
                        try:
                            result = (
                                service.users()
                                .labels()
                                .create(userId="me", body={"name": label_name})
                                .execute()
                            )
                            label_map[label_name] = result["id"]
                        except Exception as e:
                            if "Label name exists" not in str(e):
                                raise

        # Apply changes
        succeeded = 0
        failed = 0

        for change in changes:
            thread_id = change["id"]
            try:
                add_ids = []
                remove_ids = []

                if change.get("add_sys"):
                    add_ids.extend(change["add_sys"])
                if change.get("remove_sys"):
                    remove_ids.extend(change["remove_sys"])
                if change.get("add_cat"):
                    add_ids.append(change["add_cat"])
                if change.get("remove_cat"):
                    remove_ids.append(change["remove_cat"])
                if change.get("add"):
                    add_ids.extend(label_map[name] for name in change["add"])
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
                logger.warning(f"Error on {thread_id}: {e}")
                failed += 1

        return succeeded, failed

    def fetch(
        self,
        *,
        query: str = "in:inbox",
        limit: int = 50,
        output: Path | None = None,
    ) -> tuple[int, int]:
        """Fetch full threads matching query into a folder.

        Returns (cloned, skipped).
        """
        output_path = output or Path("mailbox.gax.md.d")

        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)

        logger.info(f"Searching: {query}")
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
            raise ValueError("No threads found.")

        thread_ids = [t["id"] for t in threads]
        logger.info(f"Found {len(thread_ids)} threads")

        output_path.mkdir(parents=True, exist_ok=True)
        existing_ids = _get_existing_thread_ids(output_path)

        cloned = 0
        skipped = 0

        for thread_id in thread_ids:
            if thread_id in existing_ids:
                skipped += 1
                continue

            try:
                sections = pull_thread(thread_id)
                content = format_multipart(sections)

                first = sections[0]
                filename = _make_filename(first.date, first.from_addr, first.title)
                file_path = output_path / filename

                if file_path.exists():
                    base = file_path.stem
                    file_path = output_path / f"{base}_{thread_id}.mail.gax.md"

                file_path.write_text(content, encoding="utf-8")
                cloned += 1
                logger.info(f"  {filename}")

            except Exception as e:
                logger.warning(f"Error cloning {thread_id}: {e}")

        logger.info(f"Cloned: {cloned}, Skipped: {skipped}")
        return cloned, skipped


Resource.register(Mailbox)
