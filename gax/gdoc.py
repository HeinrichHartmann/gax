"""Google Docs sync for gax.

Implements pull/init/cat commands using the multipart YAML-markdown format (ADR 002).
"""

import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from . import multipart
from . import native_md
from . import docs
from .ui import operation, success, error

logger = logging.getLogger(__name__)


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


def pull_doc(
    document_id: str, source_url: str, *, docs_service=None, drive_service=None
) -> list[DocSection]:
    """Fetch document from Google Docs API and return list of sections.

    Uses native Drive API markdown export for high-quality conversion.

    Args:
        document_id: Google Docs document ID
        source_url: Source URL for metadata
        docs_service: Optional Docs API service object for testing
        drive_service: Optional Drive API service object for testing
    """
    # Get tab list from Docs API
    tabs = native_md.get_doc_tabs(document_id, docs_service=docs_service)

    if not tabs:
        # Fallback: single document without tabs
        tabs = [{"id": "", "title": "Document", "index": 0}]

    # Get document title
    if docs_service is None:
        creds = get_authenticated_credentials()
        docs_service = build("docs", "v1", credentials=creds)

    doc = docs_service.documents().get(documentId=document_id).execute()
    doc_title = doc.get("title", "Untitled")

    # Export full document as markdown using native API
    full_md = native_md.export_doc_markdown(document_id, drive_service=drive_service)

    # Split by tabs
    tab_titles = [t["title"] for t in tabs]
    tab_contents = native_md.split_doc_by_tabs(full_md, tab_titles)

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = []

    with operation("Processing tabs", total=len(tabs)) as op:
        for i, tab in enumerate(tabs, start=1):
            tab_title = tab["title"]
            logger.info(f"Processing tab: {tab_title}")
            content = tab_contents.get(tab_title, "")

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


# =============================================================================
# Comments functions
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


# =============================================================================
# CLI commands
# =============================================================================


@docs.section("resource")
@click.group()
def doc():
    """Google Docs operations"""
    pass


# --- Tab subcommand group ---


@doc.group()
def tab():
    """Single tab operations"""
    pass


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


@tab.command("list")
@click.argument("url")
def tab_list(url: str):
    """List tabs in a document (TSV output)."""
    try:
        document_id = extract_doc_id(url)
        info = get_tabs_list(document_id)

        click.echo(f"# {info['title']}")
        click.echo("index\tid\ttitle")
        for t in info["tabs"]:
            click.echo(f"{t['index']}\t{t['id']}\t{t['title']}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def create_tab_with_content(
    document_id: str,
    tab_name: str,
    markdown: str,
    *,
    service=None,
    num_retries: int = 0,
) -> tuple[str, list]:
    """Create a new tab and populate it with markdown content.

    Args:
        document_id: Google Docs document ID
        tab_name: Name for the new tab
        markdown: Markdown content to insert
        service: Optional Docs API service for testing
        num_retries: Retries with exponential backoff on 429/5xx

    Returns:
        Tuple of (tab_id, push_warnings)
    """
    from .md2docs import markdown_to_requests

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
    content_requests, tables_data, warnings = markdown_to_requests(markdown, tab_id)
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
    from .md2docs import _utf16_len

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

    Args:
        document_id: Google Docs document ID
        tab_name: Name of the tab to update
        markdown: New markdown content
        service: Optional Docs API service for testing
    """
    from .md2docs import markdown_to_requests

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
    content_requests, tables_data, warnings = markdown_to_requests(markdown, tab_id)
    if content_requests:
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": content_requests},
        ).execute()

    # Populate table cells (second pass: read back real indices)
    if tables_data:
        _populate_tables(service, document_id, tab_id, tables_data)

    return warnings


@tab.command("import")
@click.argument("url")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output tracking file (default: <filename>.tab.gax.md)",
)
def tab_import(url: str, file: Path, output: Optional[Path]):
    """Import a markdown file as a new tab in a document."""
    try:
        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        # Derive tab name from filename
        tab_name = file.stem

        # Read content
        content = file.read_text(encoding="utf-8")

        # Check tracking file doesn't exist
        if output:
            tracking_path = output
        else:
            tracking_path = file.with_suffix(".tab.gax.md")

        if tracking_path.exists():
            click.echo(
                f"Error: Tracking file already exists: {tracking_path}", err=True
            )
            click.echo("Use 'gax doc tab push' to update an existing tab.")
            sys.exit(1)

        # Create the tab
        click.echo(f"Creating tab '{tab_name}' in {document_id}...")
        tab_id, warnings = create_tab_with_content(document_id, tab_name, content)
        for w in warnings:
            click.echo(f"  Warning: {w.feature}: {w.detail}")
        click.echo(f"Created tab: {tab_id}")

        # Get document title for tracking file
        creds = get_authenticated_credentials()
        service = build("docs", "v1", credentials=creds)
        doc = service.documents().get(documentId=document_id).execute()
        doc_title = doc.get("title", "Untitled")

        # Create tracking file
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
        success(f"Created: {tracking_path}")

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


def pull_single_tab(
    document_id: str,
    tab_name: str,
    source_url: str,
    *,
    docs_service=None,
    drive_service=None,
) -> DocSection:
    """Pull a single tab from a document.

    Uses native Drive API markdown export for high-quality conversion.

    Args:
        document_id: Google Docs document ID
        tab_name: Name of the tab to pull
        source_url: Source URL for metadata
        docs_service: Optional Docs API service object for testing
        drive_service: Optional Drive API service object for testing

    Returns:
        DocSection for the specified tab
    """
    # Get document title
    if docs_service is None:
        creds = get_authenticated_credentials()
        docs_service = build("docs", "v1", credentials=creds)

    doc = docs_service.documents().get(documentId=document_id).execute()
    doc_title = doc.get("title", "Untitled")

    # Export single tab using native API
    content = native_md.export_tab_markdown(
        document_id,
        tab_name,
        drive_service=drive_service,
        docs_service=docs_service,
    )

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return DocSection(
        title=doc_title,
        source=source_url,
        time=time_str,
        section=1,
        section_title=tab_name,
        content=content,
    )


@tab.command("clone")
@click.argument("url")
@click.argument("tab_name")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <tab>.tab.gax.md)",
)
def tab_clone(url: str, tab_name: str, output: Optional[Path]):
    """Clone a single tab to a .tab.gax.md file."""
    try:
        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        click.echo(f"Fetching tab: {tab_name}")
        section = pull_single_tab(document_id, tab_name, source_url)

        content = format_section(section)

        if output:
            file_path = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", tab_name)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.tab.gax.md")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")
        success(f"Created: {file_path}")

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@tab.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def tab_pull(file: Path):
    """Pull latest content for a single tab."""
    try:
        content = file.read_text(encoding="utf-8")
        sections = parse_multipart(content)

        if not sections:
            click.echo("Error: No sections found in file", err=True)
            sys.exit(1)

        section = sections[0]
        source_url = section.source
        tab_name = section.section_title

        if not source_url:
            click.echo("Error: No source URL found in file", err=True)
            sys.exit(1)

        document_id = extract_doc_id(source_url)
        click.echo(f"Pulling tab: {tab_name}")

        new_section = pull_single_tab(document_id, tab_name, source_url)
        new_content = format_section(new_section)

        file.write_text(new_content, encoding="utf-8")
        success(f"Updated: {file}")

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@tab.command("diff")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def tab_diff(file: Path):
    """Show diff between local file and remote tab."""
    try:
        import difflib

        content = file.read_text(encoding="utf-8")
        sections = parse_multipart(content)

        if not sections:
            click.echo("Error: No sections found in file", err=True)
            sys.exit(1)

        local_section = sections[0]
        source_url = local_section.source
        tab_name = local_section.section_title

        document_id = extract_doc_id(source_url)

        remote_section = pull_single_tab(document_id, tab_name, source_url)

        # Compare content only (not headers)
        local_lines = local_section.content.splitlines(keepends=True)
        remote_lines = remote_section.content.splitlines(keepends=True)

        diff = list(
            difflib.unified_diff(
                remote_lines,
                local_lines,
                fromfile="remote",
                tofile="local",
                lineterm="",
            )
        )

        if not diff:
            click.echo("No differences.")
        else:
            for line in diff:
                click.echo(line.rstrip("\n"))

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--patch", "use_patch", is_flag=True, help="Incremental push: apply only changed elements (experimental)")
def tab_push(file: Path, yes: bool, use_patch: bool):
    """Push local changes to a single tab (with confirmation).

    The default push path is full-replace (see ADR 023). The ``--patch`` flag
    selects an **experimental** incremental push path (ADR 027) that diffs the
    local markdown against the live document and applies only the changed
    elements. The ``--patch`` path is under evaluation and may fail on
    structural changes; when in doubt, omit the flag.
    """
    try:
        import difflib

        content = file.read_text(encoding="utf-8")
        sections = parse_multipart(content)

        if not sections:
            click.echo("Error: No sections found in file", err=True)
            sys.exit(1)

        local_section = sections[0]
        source_url = local_section.source
        tab_name = local_section.section_title

        document_id = extract_doc_id(source_url)

        content_to_push = native_md.inline_images_from_store(local_section.content)

        if use_patch:
            # --patch: AST-level diff preview + incremental push
            from .diff_push import diff_push as _diff_push, preview_diff

            preview = preview_diff(document_id, tab_name, content_to_push)

            if not preview.ops:
                click.echo("No differences to push.")
                return

            # Show operation-level preview
            click.echo("Patch operations:")
            click.echo("-" * 40)
            for line in preview.summary_lines:
                click.echo(line)
            click.echo("-" * 40)

            if preview.warnings:
                for w in preview.warnings:
                    error(w)
                click.echo("Use regular push (without --patch) for structural changes.")
                sys.exit(1)

            if not yes:
                if not click.confirm("Apply patch?"):
                    click.echo("Aborted.")
                    return

            click.echo(f"Patching tab '{tab_name}'...")
            push_warnings = _diff_push(
                document_id, tab_name, content_to_push,
                docs_service=preview.docs_service,
                drive_service=preview.drive_service,
            )
            for w in push_warnings:
                click.echo(f"  Note: {w}")
            success("Patched successfully.")
        else:
            # Default: line-level diff preview + full-replace push
            remote_section = pull_single_tab(document_id, tab_name, source_url)

            local_lines = local_section.content.splitlines(keepends=True)
            remote_lines = remote_section.content.splitlines(keepends=True)

            diff = list(
                difflib.unified_diff(
                    remote_lines,
                    local_lines,
                    fromfile="remote",
                    tofile="local",
                    lineterm="",
                )
            )

            if not diff:
                click.echo("No differences to push.")
                return

            click.echo("Changes to push:")
            click.echo("-" * 40)
            for line in diff:
                click.echo(line.rstrip("\n"))
            click.echo("-" * 40)

            from .md2docs import parse_markdown, check_unsupported

            push_warnings = check_unsupported(parse_markdown(local_section.content))
            for w in push_warnings:
                click.echo(f"  Warning: {w.feature}: {w.detail}")

            click.echo(
                "Warning: markdown cannot faithfully represent a Google Doc. "
                "Non-markdown formatting (colors, fonts, alignment, comments, "
                "suggestions, images) may be lost. Use --patch for incremental "
                "updates that preserve formatting (experimental)."
            )

            if not yes:
                if not click.confirm("Push these changes?"):
                    click.echo("Aborted.")
                    return

            click.echo(f"Pushing to tab '{tab_name}'...")
            update_tab_content(document_id, tab_name, content_to_push)
            success("Pushed successfully.")

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


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


@doc.command()
@click.argument("url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <title>.doc.gax.md)",
)
@click.option(
    "--with-comments",
    is_flag=True,
    help="Include document comments as separate sections",
)
@click.option(
    "-q", "--quiet",
    is_flag=True,
    help="Suppress multi-tab status message",
)
def clone(url: str, output: Optional[Path], with_comments: bool, quiet: bool):
    """Clone a Google Doc to a local .doc.gax.md file.

    Clones a single tab. For multi-tab documents, use 'gax doc checkout'.
    """
    try:
        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        click.echo(f"Fetching: {document_id}")

        # Fetch tab metadata
        tabs = native_md.get_doc_tabs(document_id)
        if not tabs:
            tabs = [{"id": "", "title": "Document", "index": 0}]

        # Get document title
        creds = get_authenticated_credentials()
        docs_service = build("docs", "v1", credentials=creds)
        doc = docs_service.documents().get(documentId=document_id).execute()
        doc_title = doc.get("title", "Untitled")

        # Export only the first tab
        first_tab = tabs[0]
        full_md = native_md.export_doc_markdown(document_id)
        tab_titles = [t["title"] for t in tabs]
        tab_contents = native_md.split_doc_by_tabs(full_md, tab_titles)
        tab_content = tab_contents.get(first_tab["title"], "")

        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        section = DocSection(
            title=doc_title,
            source=source_url,
            time=time_str,
            section=1,
            section_title=first_tab["title"],
            content=tab_content,
        )

        if with_comments:
            click.echo("Fetching comments...")
            sections = _add_comments_to_sections([section], document_id)
            content = format_multipart(sections)
        else:
            content = format_section(section)

        if output:
            file_path = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", doc_title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.doc.gax.md")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")
        success(f"Created: {file_path}")

        if not quiet and len(tabs) > 1:
            click.echo(
                f'  Tab "{first_tab["title"]}" cloned (1 of {len(tabs)} tabs).\n'
                f"  For all tabs: gax doc checkout {url}"
            )

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@doc.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-comments",
    is_flag=True,
    help="Include document comments as separate sections",
)
def pull(file: Path, with_comments: bool):
    """Pull latest content from Google Docs to local file."""
    try:
        content = file.read_text(encoding="utf-8")
        sections = parse_multipart(content)

        if not sections:
            click.echo("Error: No sections found in file", err=True)
            sys.exit(1)

        source_url = sections[0].source
        if not source_url:
            click.echo("Error: No source URL found in file", err=True)
            sys.exit(1)

        document_id = extract_doc_id(source_url)
        click.echo(f"Pulling: {document_id}")

        new_sections = pull_doc(document_id, source_url)

        if with_comments:
            click.echo("Fetching comments...")
            new_sections = _add_comments_to_sections(new_sections, document_id)

        new_content = format_multipart(new_sections)

        file.write_text(new_content, encoding="utf-8")
        success(f"Updated: {file}")
        click.echo(f"Sections: {len(new_sections)}")

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@doc.command()
@click.argument("url")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output folder (default: <title>.doc.gax.md.d)",
)
def checkout(url: str, output: Optional[Path]):
    """Checkout all tabs to individual files in a folder.

    Creates a folder with individual .tab.gax.md files for each tab.
    Incremental: skips existing files.
    """
    try:
        import yaml

        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        click.echo(f"Fetching: {document_id}")
        sections = pull_doc(document_id, source_url)

        if not sections:
            click.echo("Error: No sections found in document", err=True)
            sys.exit(1)

        title = sections[0].title

        # Determine output folder
        if output:
            folder = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            folder = Path(f"{safe_name}.doc.gax.md.d")

        # Create folder
        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata file
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

        click.echo(f"Checking out {len(sections)} tabs to {folder}/")

        created = 0
        skipped = 0

        with operation("Writing tab files", total=len(sections)) as op:
            for section in sections:
                tab_name = section.section_title
                logger.info(f"Processing tab: {tab_name}")

                # Skip comment sections
                if section.section_type == "comments":
                    op.advance()
                    continue

                # Generate filename
                safe_tab_name = re.sub(r'[<>:"/\\|?*]', "-", tab_name)
                safe_tab_name = re.sub(r"\s+", "_", safe_tab_name)
                file_path = folder / f"{safe_tab_name}.tab.gax.md"

                # Skip if exists
                if file_path.exists():
                    skipped += 1
                    op.advance()
                    continue

                try:
                    # Write file with full YAML header
                    content = format_section(section)
                    file_path.write_text(content, encoding="utf-8")

                    created += 1
                    click.echo(f"  {file_path.name}")

                except Exception as e:
                    click.echo(f"  Error with tab '{tab_name}': {e}", err=True)

                op.advance()

        success(f"Checked out: {created}, Skipped: {skipped} (already present)")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
