"""Google Docs resource module for gax.

Resource module — follows the draft.py reference pattern.

Two resource classes that share this module:

  Tab(Resource)  — single tab, single file (.doc.gax.md / .tab.gax.md)
  Doc(Resource)  — whole document, folder (.doc.gax.md.d/)

Module structure
================

  Data classes         — DocSection, Comment, CommentReply
  Multipart format     — format/parse .doc.gax.md files
  API helpers          — extract_doc_id, pull_doc, pull_single_tab
  Tab mutations        — get_tabs_list, create_tab_with_content, update_tab_content
  Comments             — fetch_comments, format_comment, format_comments_section
  Tab(Resource)        — single-tab resource (clone/pull/diff/push)
  Doc(Resource)        — whole-document resource (clone/pull/diff/push + tab_list, tab_import)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).
Additional notes specific to Google Docs:

  Tab is the primary editing unit. A single-tab doc clones to one file;
  a multi-tab doc clones to a folder with one file per tab. Both use
  the same .doc.gax.md file format.

  Tab.push supports two modes: full-replace (default) and incremental
  patch (use_patch=True, experimental — see ADR 027).

  Doc and Tab share all the helper functions in this module. They live
  in the same file because they are tightly coupled.
"""

import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from googleapiclient.discovery import build

from ..auth import get_authenticated_credentials
from .. import multipart
from . import native_md
from ..ui import operation
from ..resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class DocSection:
    """A section of a Google Doc."""

    title: str  # Document title (repeated in each section)
    source: str  # Source URL (repeated in each section)
    time: str  # ISO timestamp (repeated in each section)
    section: int  # Section number (1-based)
    section_title: str  # Title of this section/tab
    content: str  # Markdown content
    section_type: Optional[str] = None  # 'comments' for comment sections


@dataclass
class Comment:
    """A comment from Google Docs."""

    comment_id: str
    author: str
    date: str  # YYYY-MM-DD
    quoted_text: str
    content: str
    resolved: bool
    replies: list["CommentReply"]


@dataclass
class CommentReply:
    """A reply to a comment."""

    reply_id: str
    author: str
    date: str  # YYYY-MM-DD
    content: str


# =============================================================================
# Multipart format helpers
# =============================================================================


def _doc_section_to_multipart(section: DocSection) -> multipart.Section:
    """Convert DocSection to generic multipart Section."""
    headers = {
        "type": "gax/doc",
        "title": section.title,
        "source": section.source,
        "time": section.time,
        "tab": section.section_title,
    }
    if section.section_type:
        headers["tab_type"] = section.section_type
    return multipart.Section(headers=headers, content=section.content)


def _multipart_to_doc_section(section: multipart.Section) -> DocSection:
    """Convert generic multipart Section to DocSection."""
    # Support both new (tab) and old (section/section_title) header names
    h = section.headers
    tab_name = h.get("tab", h.get("tab_title", h.get("section_title", "")))
    return DocSection(
        title=h.get("title", ""),
        source=h.get("source", ""),
        time=h.get("time", ""),
        section=int(h.get("section", 1)),  # Keep for internal ordering
        section_title=tab_name,
        content=section.content,
        section_type=h.get("tab_type", h.get("section_type")),
    )


def format_section(section: DocSection) -> str:
    """Format a single section as YAML header + markdown body."""
    mp_section = _doc_section_to_multipart(section)
    return multipart.format_section(mp_section.headers, mp_section.content)


def format_multipart(sections: list[DocSection]) -> str:
    """Assemble sections into multipart markdown string."""
    mp_sections = [_doc_section_to_multipart(s) for s in sections]
    return multipart.format_multipart(mp_sections)


def parse_multipart(text: str) -> list[DocSection]:
    """Parse multipart markdown into sections."""
    mp_sections = multipart.parse_multipart(text)
    return [_multipart_to_doc_section(s) for s in mp_sections]


# =============================================================================
# Google Docs API functions
# =============================================================================


def extract_doc_id(url: str) -> str:
    """Extract document ID from Google Docs URL or return as-is."""
    match = re.search(r"/document/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]+", url):
        return url
    raise ValueError(f"Cannot extract document ID from: {url}")


def _fetch_doc(document_id: str, *, docs_service=None, num_retries: int = 0) -> dict:
    """Fetch full document JSON with tab content."""
    if docs_service is None:
        creds = get_authenticated_credentials()
        docs_service = build("docs", "v1", credentials=creds)
    return (
        docs_service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute(num_retries=num_retries)
    )


def _tab_content_to_markdown(doc: dict, tab: dict) -> str:
    """Convert a tab's body content to markdown via the IR."""
    from . import ir

    body = tab.get("documentTab", {}).get("body", {}).get("content", [])
    blocks = ir.from_doc_json(body, lists=doc.get("lists"))
    md = ir.render_markdown(blocks)
    # Post-process: extract base64 images to blob store
    md = native_md.extract_images_to_store(md)
    return md


def pull_doc(
    document_id: str,
    source_url: str,
    *,
    docs_service=None,
    drive_service=None,
    num_retries: int = 0,
) -> list[DocSection]:
    """Fetch document from Google Docs API and return list of sections.

    Reads directly from the Docs API JSON (no Drive API markdown export).
    Each tab's content is converted to markdown via the Block/Span IR.
    """
    doc = _fetch_doc(
        document_id,
        docs_service=docs_service,
        num_retries=num_retries,
    )
    doc_title = doc.get("title", "Untitled")
    raw_tabs = doc.get("tabs", [])

    if not raw_tabs:
        return []

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = []

    with operation("Processing tabs", total=len(raw_tabs)) as op:
        for i, tab in enumerate(raw_tabs, start=1):
            props = tab.get("tabProperties", {})
            tab_title = props.get("title", f"Tab {i}")
            logger.info(f"Processing tab: {tab_title}")

            content = _tab_content_to_markdown(doc, tab)

            sections.append(
                DocSection(
                    title=doc_title,
                    source=source_url,
                    time=time_str,
                    section=i,
                    section_title=tab_title,
                    content=content,
                )
            )
            op.advance()

    return sections


def pull_single_tab(
    document_id: str,
    tab_name: str,
    source_url: str,
    *,
    docs_service=None,
    drive_service=None,
    num_retries: int = 0,
) -> DocSection:
    """Pull a single tab from a document.

    Reads directly from the Docs API JSON.
    """
    doc = _fetch_doc(
        document_id,
        docs_service=docs_service,
        num_retries=num_retries,
    )
    doc_title = doc.get("title", "Untitled")

    # Find the tab by name
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("title") == tab_name:
            content = _tab_content_to_markdown(doc, tab)
            time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return DocSection(
                title=doc_title,
                source=source_url,
                time=time_str,
                section=1,
                section_title=tab_name,
                content=content,
            )

    raise ValueError(f"Tab not found: {tab_name}")


# =============================================================================
# Tab mutation helpers
# =============================================================================


def get_tabs_list(document_id: str, *, service=None) -> dict:
    """Get document title and list of tabs.

    Returns:
        Dict with 'title' and 'tabs' (list of {id, title, index})
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("docs", "v1", credentials=creds)

    document = (
        service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )

    doc_title = document.get("title", "Untitled")
    raw_tabs = document.get("tabs", [])

    tabs = []
    for i, t in enumerate(raw_tabs):
        props = t.get("tabProperties", {})
        tabs.append(
            {
                "id": props.get("tabId", ""),
                "title": props.get("title", f"Tab {i}"),
                "index": i,
            }
        )

    # If no tabs, document itself is the only "tab"
    if not tabs:
        tabs = [{"id": "", "title": doc_title, "index": 0}]

    return {"title": doc_title, "tabs": tabs}


def create_tab_with_content(
    document_id: str,
    tab_name: str,
    markdown: str,
    *,
    service=None,
    num_retries: int = 0,
) -> tuple[str, list]:
    """Create a new tab and populate it with markdown content.

    Returns:
        Tuple of (tab_id, push_warnings)
    """
    from .ir import from_markdown, to_docs_requests

    if service is None:
        creds = get_authenticated_credentials()
        service = build("docs", "v1", credentials=creds)

    # Step 1: Create the tab
    create_response = (
        service.documents()
        .batchUpdate(
            documentId=document_id,
            body={
                "requests": [{"addDocumentTab": {"tabProperties": {"title": tab_name}}}]
            },
        )
        .execute(num_retries=num_retries)
    )

    # Get the new tab ID from response
    tab_id = create_response["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

    # Step 2: Insert markdown content
    blocks = from_markdown(markdown)
    content_requests, tables_data, warnings = to_docs_requests(blocks, tab_id)
    if content_requests:
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": content_requests},
        ).execute(num_retries=num_retries)

    # Step 3: Populate table cells (read back real indices from API)
    if tables_data:
        _populate_tables(
            service, document_id, tab_id, tables_data, num_retries=num_retries
        )

    return tab_id, warnings


def _populate_tables(
    service,
    document_id: str,
    tab_id: str,
    tables_data: list,
    num_retries: int = 0,
) -> None:
    """Populate empty table cells by reading back actual document indices.

    After insertTable creates empty tables, this reads the document structure
    to get real cell indices, inserts cell content, and applies inline formatting.
    """
    from .ir import _utf16_len

    doc = (
        service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute(num_retries=num_retries)
    )

    # Find the tab's body content
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("tabId") != tab_id:
            continue

        body = tab.get("documentTab", {}).get("body", {})
        content = body.get("content", [])

        # Find table elements in document
        doc_tables = [elem for elem in content if "table" in elem]

        if len(doc_tables) != len(tables_data):
            logger.warning(
                f"Table count mismatch: {len(doc_tables)} in doc vs {len(tables_data)} in markdown"
            )
            return

        # Pass 1: Insert plain text into cells (strip markdown syntax)
        insert_requests = []
        # Track cells that need formatting: (insert_idx, spans)
        cells_to_format = []

        for doc_table, md_rows in zip(doc_tables, tables_data):
            table = doc_table["table"]
            for r, doc_row in enumerate(table.get("tableRows", [])):
                if r >= len(md_rows):
                    break
                md_row = md_rows[r]
                for c, doc_cell in enumerate(doc_row.get("tableCells", [])):
                    spans = md_row[c] if c < len(md_row) else []
                    if not spans:
                        continue
                    cell_content = doc_cell.get("content", [])
                    if not cell_content:
                        continue
                    para = cell_content[0]
                    insert_idx = para.get("startIndex")
                    if insert_idx is None:
                        continue
                    plain = "".join(s.text for s in spans)
                    if not plain:
                        continue

                    loc = {"index": insert_idx, "tabId": tab_id}
                    insert_requests.append(
                        {"insertText": {"text": plain, "location": loc}}
                    )
                    cells_to_format.append((insert_idx, spans))

        if insert_requests:
            # Reverse so last cell is populated first (stable indices)
            insert_requests.reverse()
            service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": insert_requests},
            ).execute(num_retries=num_retries)

        # Pass 2: Apply bold/italic formatting to cell content
        # Re-read the document to get updated indices
        if not cells_to_format:
            break

        has_formatting = any(
            any(s.bold or s.italic or s.strikethrough or s.url for s in spans)
            for _, spans in cells_to_format
        )
        if not has_formatting:
            break

        doc = (
            service.documents()
            .get(documentId=document_id, includeTabsContent=True)
            .execute(num_retries=num_retries)
        )

        # Re-find tables and build formatting requests
        for tab2 in doc.get("tabs", []):
            props2 = tab2.get("tabProperties", {})
            if props2.get("tabId") != tab_id:
                continue

            body2 = tab2.get("documentTab", {}).get("body", {})
            content2 = body2.get("content", [])
            doc_tables2 = [elem for elem in content2 if "table" in elem]

            fmt_requests = []
            for doc_table, md_rows in zip(doc_tables2, tables_data):
                table = doc_table["table"]
                for r, doc_row in enumerate(table.get("tableRows", [])):
                    if r >= len(md_rows):
                        break
                    md_row = md_rows[r]
                    for c, doc_cell in enumerate(doc_row.get("tableCells", [])):
                        spans = md_row[c] if c < len(md_row) else []
                        if not spans:
                            continue
                        cell_content = doc_cell.get("content", [])
                        if not cell_content:
                            continue
                        para = cell_content[0]
                        cell_start = para.get("startIndex")
                        if cell_start is None:
                            continue

                        offset = cell_start
                        for span in spans:
                            span_end = offset + _utf16_len(span.text)
                            if span.bold:
                                fmt_requests.append(
                                    {
                                        "updateTextStyle": {
                                            "range": {
                                                "startIndex": offset,
                                                "endIndex": span_end,
                                                "tabId": tab_id,
                                            },
                                            "textStyle": {"bold": True},
                                            "fields": "bold",
                                        }
                                    }
                                )
                            if span.italic:
                                fmt_requests.append(
                                    {
                                        "updateTextStyle": {
                                            "range": {
                                                "startIndex": offset,
                                                "endIndex": span_end,
                                                "tabId": tab_id,
                                            },
                                            "textStyle": {"italic": True},
                                            "fields": "italic",
                                        }
                                    }
                                )
                            if span.strikethrough:
                                fmt_requests.append(
                                    {
                                        "updateTextStyle": {
                                            "range": {
                                                "startIndex": offset,
                                                "endIndex": span_end,
                                                "tabId": tab_id,
                                            },
                                            "textStyle": {"strikethrough": True},
                                            "fields": "strikethrough",
                                        }
                                    }
                                )
                            if span.url:
                                fmt_requests.append(
                                    {
                                        "updateTextStyle": {
                                            "range": {
                                                "startIndex": offset,
                                                "endIndex": span_end,
                                                "tabId": tab_id,
                                            },
                                            "textStyle": {"link": {"url": span.url}},
                                            "fields": "link",
                                        }
                                    }
                                )
                            offset = span_end

            if fmt_requests:
                service.documents().batchUpdate(
                    documentId=document_id,
                    body={"requests": fmt_requests},
                ).execute(num_retries=num_retries)

            break

        break


def update_tab_content(
    document_id: str, tab_name: str, markdown: str, *, service=None
) -> list:
    """Replace tab content with new markdown.

    Returns list of push warnings.
    """
    from .ir import from_markdown, to_docs_requests

    if service is None:
        creds = get_authenticated_credentials()
        service = build("docs", "v1", credentials=creds)

    # Get tab ID by name
    doc = (
        service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )

    tab_id = None
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("title") == tab_name:
            tab_id = props.get("tabId")
            break

    if not tab_id:
        raise ValueError(f"Tab not found: {tab_name}")

    # Get current content length to delete
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("tabId") == tab_id:
            body = tab.get("documentTab", {}).get("body", {})
            content = body.get("content", [])
            if content:
                # Find end index (last element's endIndex - 1 to preserve final newline)
                end_index = content[-1].get("endIndex", 1) - 1
                if end_index > 1:
                    # Delete existing content
                    service.documents().batchUpdate(
                        documentId=document_id,
                        body={
                            "requests": [
                                {
                                    "deleteContentRange": {
                                        "range": {
                                            "startIndex": 1,
                                            "endIndex": end_index,
                                            "tabId": tab_id,
                                        }
                                    }
                                }
                            ]
                        },
                    ).execute()
            break

    # Insert new content
    blocks = from_markdown(markdown)
    content_requests, tables_data, warnings = to_docs_requests(blocks, tab_id)
    if content_requests:
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": content_requests},
        ).execute()

    # Populate table cells (second pass: read back real indices)
    if tables_data:
        _populate_tables(service, document_id, tab_id, tables_data)

    return warnings


# =============================================================================
# Comments
# =============================================================================


def fetch_comments(document_id: str) -> list[Comment]:
    """Fetch comments from Google Drive API."""
    creds = get_authenticated_credentials()
    service = build("drive", "v3", credentials=creds)

    comments = []
    page_token = None

    with operation("Fetching comments"):
        while True:
            result = (
                service.comments()
                .list(
                    fileId=document_id,
                    fields="comments(id,author,createdTime,quotedFileContent,content,resolved,replies(id,author,createdTime,content)),nextPageToken",
                    pageToken=page_token,
                )
                .execute()
            )

            for c in result.get("comments", []):
                logger.info(f"Processing comment: {c.get('id', 'unknown')}")
                # Parse date
                created = c.get("createdTime", "")
                date = created[:10] if created else ""

                # Author email
                author = c.get("author", {}).get("emailAddress", "")
                if not author:
                    author = c.get("author", {}).get("displayName", "Unknown")

                # Quoted text
                quoted = c.get("quotedFileContent", {}).get("value", "")

                # Replies
                replies = []
                for r in c.get("replies", []):
                    r_created = r.get("createdTime", "")
                    r_date = r_created[:10] if r_created else ""
                    r_author = r.get("author", {}).get("emailAddress", "")
                    if not r_author:
                        r_author = r.get("author", {}).get("displayName", "Unknown")

                    replies.append(
                        CommentReply(
                            reply_id=r.get("id", ""),
                            author=r_author,
                            date=r_date,
                            content=r.get("content", ""),
                        )
                    )

                comments.append(
                    Comment(
                        comment_id=c.get("id", ""),
                        author=author,
                        date=date,
                        quoted_text=quoted,
                        content=c.get("content", ""),
                        resolved=c.get("resolved", False),
                        replies=replies,
                    )
                )

            page_token = result.get("nextPageToken")
            if not page_token:
                break

    return comments


def format_comment(comment: Comment) -> str:
    """Format a single comment as markdown."""
    lines = []

    # Main comment line
    resolved_tag = " [RESOLVED]" if comment.resolved else ""
    lines.append(
        f"* [{comment.comment_id}] {comment.date} - {comment.author}{resolved_tag}"
    )

    # Quoted context
    if comment.quoted_text:
        # Truncate long quotes
        quoted = comment.quoted_text
        if len(quoted) > 80:
            quoted = quoted[:77] + "..."
        lines.append(f'  > "{quoted}"')

    # Comment content
    for line in comment.content.split("\n"):
        lines.append(f"  {line}")

    # Replies
    for reply in comment.replies:
        lines.append(f"  ↳ [{reply.reply_id}] {reply.date} - {reply.author}")
        for line in reply.content.split("\n"):
            lines.append(f"    {line}")

    return "\n".join(lines)


def format_comments_section(
    comments: list[Comment],
    title: str,
    source: str,
    time_str: str,
    section_num: int,
    section_title: str,
) -> DocSection:
    """Format comments as a multipart section."""
    content_lines = []
    with operation("Formatting comments", total=len(comments)) as op:
        for comment in comments:
            logger.info(f"Formatting comment: {comment.comment_id}")
            content_lines.append(format_comment(comment))
            content_lines.append("")
            op.advance()

    return DocSection(
        title=title,
        source=source,
        time=time_str,
        section=section_num,
        section_type="comments",
        section_title=f"{section_title} (Comments)",
        content="\n".join(content_lines).strip(),
    )


def _add_comments_to_sections(
    sections: list[DocSection],
    document_id: str,
) -> list[DocSection]:
    """Fetch comments and interleave comment sections after each content section."""
    comments = fetch_comments(document_id)
    if not comments:
        return sections

    # For now, we don't have per-tab comment mapping from Drive API,
    # so we add all comments after the first section
    # (Google Docs comments are document-wide, not tab-specific)
    result = []
    first_section = sections[0]

    result.append(first_section)
    result.append(
        format_comments_section(
            comments=comments,
            title=first_section.title,
            source=first_section.source,
            time_str=first_section.time,
            section_num=first_section.section,
            section_title=first_section.section_title,
        )
    )

    # Add remaining content sections (if multi-tab)
    with operation("Adding sections", total=len(sections) - 1) as op:
        for section in sections[1:]:
            logger.info(f"Adding section: {section.section_title}")
            result.append(section)
            op.advance()

    return result


# =============================================================================
# Helpers shared by Tab and Doc
# =============================================================================


def _safe_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    safe = re.sub(r'[<>:"/\\|?*]', "-", name)
    return re.sub(r"\s+", "_", safe)


def _parse_tab_file(path: Path) -> DocSection:
    """Read a .doc.gax.md or .tab.gax.md file and return its first section."""
    content = path.read_text(encoding="utf-8")
    sections = parse_multipart(content)
    if not sections:
        raise ValueError(f"No sections found in {path}")
    return sections[0]


# =============================================================================
# Tab(Resource) — single tab, single file
# =============================================================================


class Tab(Resource):
    """A single Google Docs tab (.doc.gax.md or .tab.gax.md file)."""

    name = "doc-tab"

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Clone a single tab to a .doc.gax.md file.

        Keyword args:
            tab_name: specific tab to clone (default: first tab)
            with_comments: include comments section
            quiet: suppress multi-tab hint
        """
        tab_name = kw.get("tab_name")
        with_comments = kw.get("with_comments", False)

        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        if tab_name:
            # Clone specific tab
            section = pull_single_tab(document_id, tab_name, source_url)
        else:
            # Clone first tab via full document fetch
            doc = _fetch_doc(document_id)
            doc_title = doc.get("title", "Untitled")
            raw_tabs = doc.get("tabs", [])

            if not raw_tabs:
                raise ValueError("Document has no tabs")

            first_tab = raw_tabs[0]
            first_title = first_tab.get("tabProperties", {}).get("title", "Document")
            content = _tab_content_to_markdown(doc, first_tab)

            time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            section = DocSection(
                title=doc_title,
                source=source_url,
                time=time_str,
                section=1,
                section_title=first_title,
                content=content,
            )

        if with_comments:
            sections = _add_comments_to_sections([section], document_id)
            content = format_multipart(sections)
        else:
            content = format_section(section)

        if output:
            file_path = output
        else:
            safe_name = _safe_filename(section.title if not tab_name else tab_name)
            suffix = ".tab.gax.md" if tab_name else ".doc.gax.md"
            file_path = Path(f"{safe_name}{suffix}")

        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        return file_path

    def pull(self, path: Path, **kw) -> None:
        """Refresh a tab file from remote."""
        with_comments = kw.get("with_comments", False)

        section = _parse_tab_file(path)
        source_url = section.source
        if not source_url:
            raise ValueError("No source URL found in file")

        document_id = extract_doc_id(source_url)

        # Check if this is a single-tab file or multipart
        content = path.read_text(encoding="utf-8")
        sections = parse_multipart(content)

        if len(sections) == 1:
            # Single tab — pull just that tab
            tab_name = section.section_title
            logger.info(f"Pulling tab: {tab_name}")
            new_section = pull_single_tab(document_id, tab_name, source_url)
            if with_comments:
                new_sections = _add_comments_to_sections([new_section], document_id)
                new_content = format_multipart(new_sections)
            else:
                new_content = format_section(new_section)
        else:
            # Multi-section file (legacy multipart) — pull all tabs
            logger.info(f"Pulling document: {document_id}")
            new_sections = pull_doc(document_id, source_url)
            if with_comments:
                new_sections = _add_comments_to_sections(new_sections, document_id)
            new_content = format_multipart(new_sections)

        path.write_text(new_content, encoding="utf-8")

    def diff(self, path: Path, **kw) -> str | None:
        """Preview changes between local tab and remote.

        Returns unified diff string, or None if no changes.
        """
        section = _parse_tab_file(path)
        source_url = section.source
        tab_name = section.section_title

        if not source_url:
            raise ValueError("No source URL found in file")

        document_id = extract_doc_id(source_url)
        remote_section = pull_single_tab(document_id, tab_name, source_url)

        local_lines = section.content.splitlines(keepends=True)
        remote_lines = remote_section.content.splitlines(keepends=True)

        diff_lines = list(
            difflib.unified_diff(
                remote_lines,
                local_lines,
                fromfile="remote",
                tofile="local",
                lineterm="",
            )
        )

        if not diff_lines:
            return None

        return "\n".join(line.rstrip("\n") for line in diff_lines)

    def push(self, path: Path, **kw) -> None:
        """Push local tab to remote.

        Keyword args:
            use_patch: use incremental AST-level push (experimental)
        """
        use_patch = kw.get("use_patch", False)

        section = _parse_tab_file(path)
        source_url = section.source
        tab_name = section.section_title

        if not source_url:
            raise ValueError("No source URL found in file")

        document_id = extract_doc_id(source_url)
        content_to_push = native_md.inline_images_from_store(section.content)

        if use_patch:
            from .diff_push import diff_push as _diff_push

            logger.info(f"Patching tab '{tab_name}'...")
            _diff_push(document_id, tab_name, content_to_push)
        else:
            logger.info(f"Pushing to tab '{tab_name}'...")
            update_tab_content(document_id, tab_name, content_to_push)


# =============================================================================
# Doc(Resource) — whole document, folder
# =============================================================================


class Doc(Resource):
    """A Google Docs document (.doc.gax.md.d/ folder)."""

    name = "doc"

    def clone(self, url: str, output: Path | None = None, **kw) -> Path:
        """Clone all tabs into a folder."""
        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        logger.info(f"Fetching: {document_id}")
        sections = pull_doc(document_id, source_url)

        if not sections:
            raise ValueError("No sections found in document")

        title = sections[0].title

        if output:
            folder = output
        else:
            folder = Path(f"{_safe_filename(title)}.doc.gax.md.d")

        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata
        metadata = {
            "type": "gax/doc-checkout",
            "document_id": document_id,
            "url": source_url,
            "title": title,
            "checked_out": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        metadata_path = folder / ".gax.yaml"
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        created = 0
        skipped = 0

        for section in sections:
            if section.section_type == "comments":
                continue

            file_path = folder / f"{_safe_filename(section.section_title)}.doc.gax.md"

            if file_path.exists():
                skipped += 1
                continue

            content = format_section(section)
            file_path.write_text(content, encoding="utf-8")
            logger.info(f"Created: {file_path.name}")
            created += 1

        logger.info(f"Checked out: {created}, Skipped: {skipped}")
        return folder

    def pull(self, path: Path, **kw) -> None:
        """Pull all tabs in a checkout folder."""
        metadata_path = path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        document_id = metadata.get("document_id")
        url = metadata.get("url")
        if not document_id or not url:
            raise ValueError("No document_id or url in .gax.yaml")

        logger.info(f"Pulling: {document_id}")
        sections = pull_doc(document_id, url)

        # Update metadata
        metadata["checked_out"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        metadata["title"] = sections[0].title if sections else metadata.get("title", "")
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Write tab files
        for section in sections:
            if section.section_type == "comments":
                continue

            file_path = path / f"{_safe_filename(section.section_title)}.doc.gax.md"
            content = format_section(section)
            file_path.write_text(content, encoding="utf-8")
            logger.info(f"Updated: {file_path.name}")

    def diff(self, path: Path, **kw) -> str | None:
        """Diff all tabs in a checkout folder against remote."""
        metadata_path = path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {path}")

        with open(metadata_path) as f:
            metadata = yaml.safe_load(f)

        document_id = metadata.get("document_id")
        url = metadata.get("url")
        if not document_id or not url:
            raise ValueError("No document_id or url in .gax.yaml")

        all_diffs = []
        tab = Tab()

        # Diff each tab file in the folder
        for tab_file in sorted(path.glob("*.doc.gax.md")):
            tab_diff = tab.diff(tab_file)
            if tab_diff:
                all_diffs.append(f"--- {tab_file.name} ---")
                all_diffs.append(tab_diff)

        return "\n".join(all_diffs) if all_diffs else None

    def push(self, path: Path, **kw) -> None:
        """Push all changed tabs in a checkout folder."""
        metadata_path = path / ".gax.yaml"
        if not metadata_path.exists():
            raise ValueError(f"No .gax.yaml found in {path}")

        tab = Tab()

        for tab_file in sorted(path.glob("*.doc.gax.md")):
            if tab.diff(tab_file) is not None:
                logger.info(f"Pushing: {tab_file.name}")
                tab.push(tab_file, **kw)

    # Non-standard operations

    def tab_list(self, url: str, out) -> None:
        """Write tab listing to file descriptor."""
        document_id = extract_doc_id(url)
        info = get_tabs_list(document_id)

        out.write(f"# {info['title']}\n")
        out.write("index\tid\ttitle\n")
        for t in info["tabs"]:
            out.write(f"{t['index']}\t{t['id']}\t{t['title']}\n")

    def tab_import(self, url: str, file: Path, output: Path | None = None) -> Path:
        """Import a markdown file as a new tab in a document.

        Returns path to the tracking file created.
        """
        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        tab_name = file.stem
        content = file.read_text(encoding="utf-8")

        tracking_path = output or file.with_suffix(".tab.gax.md")
        if tracking_path.exists():
            raise ValueError(
                f"Tracking file already exists: {tracking_path}. "
                "Use 'gax doc tab push' to update an existing tab."
            )

        logger.info(f"Creating tab '{tab_name}' in {document_id}...")
        tab_id, warnings = create_tab_with_content(document_id, tab_name, content)
        for w in warnings:
            logger.info(f"Warning: {w.feature}: {w.detail}")
        logger.info(f"Created tab: {tab_id}")

        # Get document title for tracking file
        creds = get_authenticated_credentials()
        service = build("docs", "v1", credentials=creds)
        doc = service.documents().get(documentId=document_id).execute()
        doc_title = doc.get("title", "Untitled")

        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        section = DocSection(
            title=doc_title,
            source=source_url,
            time=time_str,
            section=1,
            section_title=tab_name,
            content=content,
        )

        tracking_content = format_section(section)
        tracking_path.write_text(tracking_content, encoding="utf-8")
        return tracking_path
