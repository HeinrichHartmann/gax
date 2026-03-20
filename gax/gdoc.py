"""Google Docs sync for gax.

Implements pull/init/cat commands using the multipart YAML-markdown format (ADR 002).
"""

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
        "title": section.title,
        "source": section.source,
        "time": section.time,
        "section": section.section,
    }
    if section.section_type:
        headers["section_type"] = section.section_type
    headers["section_title"] = section.section_title
    return multipart.Section(headers=headers, content=section.content)


def _multipart_to_doc_section(section: multipart.Section) -> DocSection:
    """Convert generic multipart Section to DocSection."""
    return DocSection(
        title=section.headers.get("title", ""),
        source=section.headers.get("source", ""),
        time=section.headers.get("time", ""),
        section=int(section.headers.get("section", 1)),
        section_title=section.headers.get("section_title", ""),
        content=section.content,
        section_type=section.headers.get("section_type"),
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


def _docs_body_to_markdown(body: dict) -> str:
    """Convert Google Docs API body dict to markdown."""
    lines = []

    for element in body.get("content", []):
        if "paragraph" in element:
            para = element["paragraph"]
            style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")

            # Extract text from paragraph elements
            text = "".join(
                run.get("textRun", {}).get("content", "")
                for run in para.get("elements", [])
            ).rstrip("\n")

            if not text.strip():
                lines.append("")
                continue

            # Map heading styles
            if style == "HEADING_1":
                lines.append(f"# {text}")
            elif style == "HEADING_2":
                lines.append(f"## {text}")
            elif style == "HEADING_3":
                lines.append(f"### {text}")
            elif style == "HEADING_4":
                lines.append(f"#### {text}")
            else:
                lines.append(text)
            lines.append("")

        elif "table" in element:
            lines.append("*(table omitted)*")
            lines.append("")

    return "\n".join(lines)


def pull_doc(document_id: str, source_url: str, *, service=None) -> list[DocSection]:
    """Fetch document from Google Docs API and return list of sections.

    Args:
        document_id: Google Docs document ID
        source_url: Source URL for metadata
        service: Optional Docs API service object for testing
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build("docs", "v1", credentials=creds)

    # Fetch document with tab content
    document = (
        service.documents()
        .get(
            documentId=document_id,
            includeTabsContent=True,
        )
        .execute()
    )

    doc_title = document.get("title", "Untitled")
    raw_tabs = document.get("tabs", [])
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    sections = []

    if raw_tabs:
        # Document has tabs
        for i, tab in enumerate(raw_tabs, start=1):
            props = tab.get("tabProperties", {})
            tab_title = props.get("title", f"Tab {i}")
            body = tab.get("documentTab", {}).get("body", {})
            content = _docs_body_to_markdown(body)

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
    else:
        # Single-section document (no tabs or old API)
        body = document.get("body", {})
        content = _docs_body_to_markdown(body)

        sections.append(
            DocSection(
                title=doc_title,
                source=source_url,
                time=time_str,
                section=1,
                section_title=doc_title,
                content=content,
            )
        )

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
    for comment in comments:
        content_lines.append(format_comment(comment))
        content_lines.append("")

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
        .get(documentId=document_id, includeTabsContent=False)
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


@tab.command("import")
@click.argument("url")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output tracking file (default: <filename>.tab.gax)",
)
def tab_import(url: str, file: Path, output: Optional[Path]):
    """Import a markdown file as a new tab in a document.

    This creates a new tab with the file's content and saves a tracking
    file for future push/pull operations.

    Note: Actually creating the tab in Google Docs is not yet implemented.
    """
    try:
        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        # Derive tab name from filename
        tab_name = file.stem

        # Read content
        content = file.read_text(encoding="utf-8")

        # Create a tracking file
        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        section = DocSection(
            title="",  # Will be filled when actually created
            source=source_url,
            time=time_str,
            section=1,
            section_title=tab_name,
            content=content,
        )

        if output:
            tracking_path = output
        else:
            tracking_path = file.with_suffix(".tab.gax")

        if tracking_path.exists():
            click.echo(f"Error: Tracking file already exists: {tracking_path}", err=True)
            click.echo("Use 'gax doc tab push' to update an existing tab.")
            sys.exit(1)

        # TODO: Implement actual tab creation via Docs API
        click.echo("Warning: Creating tabs in Google Docs is not yet implemented.", err=True)
        click.echo(f"Would create tab '{tab_name}' in document {document_id}")
        click.echo(f"Content ({len(content)} chars):")
        click.echo("-" * 40)
        preview = content[:500] + ("..." if len(content) > 500 else "")
        click.echo(preview)
        click.echo("-" * 40)

        # For now, create the tracking file anyway so the workflow is clear
        tracking_content = format_section(section)
        tracking_path.write_text(tracking_content, encoding="utf-8")
        click.echo(f"Created tracking file: {tracking_path}")
        click.echo("When tab creation is implemented, use 'gax doc tab push' to sync changes.")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def pull_single_tab(
    document_id: str, tab_name: str, source_url: str, *, service=None
) -> DocSection:
    """Pull a single tab from a document.

    Args:
        document_id: Google Docs document ID
        tab_name: Name of the tab to pull
        source_url: Source URL for metadata
        service: Optional Docs API service object for testing

    Returns:
        DocSection for the specified tab
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
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Find matching tab
    for i, t in enumerate(raw_tabs, start=1):
        props = t.get("tabProperties", {})
        title = props.get("title", f"Tab {i}")
        tab_id = props.get("tabId", "")

        if title == tab_name or tab_id == tab_name:
            body = t.get("documentTab", {}).get("body", {})
            content = _docs_body_to_markdown(body)

            return DocSection(
                title=doc_title,
                source=source_url,
                time=time_str,
                section=1,
                section_title=title,
                content=content,
            )

    # If no tabs, check if tab_name matches document title
    if not raw_tabs:
        body = document.get("body", {})
        content = _docs_body_to_markdown(body)

        return DocSection(
            title=doc_title,
            source=source_url,
            time=time_str,
            section=1,
            section_title=doc_title,
            content=content,
        )

    raise ValueError(f"Tab not found: {tab_name}")


@tab.command("clone")
@click.argument("url")
@click.argument("tab_name")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <tab>.tab.gax)",
)
def tab_clone(url: str, tab_name: str, output: Optional[Path]):
    """Clone a single tab to a .tab.gax file."""
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
            file_path = Path(f"{safe_name}.tab.gax")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")
        click.echo(f"Created: {file_path}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
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
        click.echo(f"Updated: {file}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
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
def tab_push(file: Path, yes: bool):
    """Push local changes to a single tab (with confirmation)."""
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

        # Get remote content for diff
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

        # Show diff
        click.echo("Changes to push:")
        click.echo("-" * 40)
        for line in diff:
            click.echo(line.rstrip("\n"))
        click.echo("-" * 40)

        # Confirm
        if not yes:
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                return

        # TODO: Implement actual push via Docs API
        # For now, just warn that push is not yet implemented
        click.echo(
            "Warning: Push to Google Docs is not yet implemented.", err=True
        )
        click.echo("This will be added when the Docs API write support is ready.")
        sys.exit(1)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
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
    for section in sections[1:]:
        result.append(section)

    return result


@doc.command()
@click.argument("url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <title>.doc.gax)",
)
@click.option(
    "--with-comments",
    is_flag=True,
    help="Include document comments as separate sections",
)
def clone(url: str, output: Optional[Path], with_comments: bool):
    """Clone a Google Doc to a local .doc.gax file."""
    try:
        document_id = extract_doc_id(url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        click.echo(f"Fetching: {document_id}")
        sections = pull_doc(document_id, source_url)

        if with_comments:
            click.echo("Fetching comments...")
            sections = _add_comments_to_sections(sections, document_id)

        content = format_multipart(sections)

        if output:
            file_path = output
        else:
            # Generate filename from title
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", sections[0].title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.doc.gax")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")
        click.echo(f"Created: {file_path}")
        click.echo(f"Title: {sections[0].title}")
        click.echo(f"Sections: {len(sections)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
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
        click.echo(f"Updated: {file}")
        click.echo(f"Sections: {len(new_sections)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
