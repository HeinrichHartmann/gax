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


@dataclass
class Section:
    """A section of a multipart document."""

    title: str  # Document title (repeated in each section)
    source: str  # Source URL (repeated in each section)
    time: str  # ISO timestamp (repeated in each section)
    section: int  # Section number (1-based)
    section_title: str  # Title of this section/tab
    content: str  # Markdown content
    content_length: Optional[int] = None  # Only set if content contains ---
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
    replies: list['CommentReply']


@dataclass
class CommentReply:
    """A reply to a comment."""
    reply_id: str
    author: str
    date: str  # YYYY-MM-DD
    content: str


# =============================================================================
# Multipart format functions (ADR 002)
# =============================================================================

def needs_content_length(content: str) -> bool:
    """Check if content needs content-length header for safe parsing."""
    return '\n---\n' in content or content.startswith('---\n') or content.endswith('\n---')


def format_section(section: Section) -> str:
    """Format a single section as YAML header + markdown body."""
    lines = [
        '---',
        f'title: {section.title}',
        f'source: {section.source}',
        f'time: {section.time}',
        f'section: {section.section}',
    ]

    if section.section_type:
        lines.append(f'section_type: {section.section_type}')

    lines.append(f'section_title: {section.section_title}')

    content = section.content
    if needs_content_length(content):
        content_bytes = content.encode('utf-8')
        lines.append(f'content-length: {len(content_bytes)}')

    lines.append('---')
    return '\n'.join(lines) + '\n' + content


def format_multipart(sections: list[Section]) -> str:
    """Assemble sections into multipart markdown string."""
    return ''.join(format_section(s) for s in sections)


def parse_multipart(text: str) -> list[Section]:
    """Parse multipart markdown into sections."""
    sections = []
    pos = 0
    text_bytes = text.encode('utf-8')

    while pos < len(text):
        # Find header start
        if not text[pos:].startswith('---\n'):
            # Skip any leading content before first ---
            next_header = text.find('\n---\n', pos)
            if next_header == -1:
                break
            pos = next_header + 1
            continue

        pos += 4  # skip '---\n'

        # Parse header until ---
        header_end = text.find('\n---\n', pos)
        if header_end == -1:
            break

        header_text = text[pos:header_end]
        header = _parse_header(header_text)
        pos = header_end + 5  # skip '\n---\n'

        # Read body
        content_length = header.get('content-length')
        if content_length is not None:
            # Read exactly content_length bytes
            byte_pos = len(text[:pos].encode('utf-8'))
            content_bytes = text_bytes[byte_pos:byte_pos + content_length]
            content = content_bytes.decode('utf-8')
            pos += len(content)
        else:
            # Scan for next section or EOF
            next_section = text.find('\n---\n', pos)
            if next_section == -1:
                content = text[pos:]
                pos = len(text)
            else:
                content = text[pos:next_section + 1]  # include trailing \n
                pos = next_section + 1

        sections.append(Section(
            title=header.get('title', ''),
            source=header.get('source', ''),
            time=header.get('time', ''),
            section=int(header.get('section', 1)),
            section_title=header.get('section_title', ''),
            content=content.strip(),
            content_length=content_length,
        ))

    return sections


def _parse_header(text: str) -> dict:
    """Parse simple YAML-like header into dict."""
    result = {}
    for line in text.split('\n'):
        if ':' in line:
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()
            if key == 'content-length':
                result[key] = int(value)
            else:
                result[key] = value
    return result


# =============================================================================
# Google Docs API functions
# =============================================================================

def extract_doc_id(url: str) -> str:
    """Extract document ID from Google Docs URL or return as-is."""
    match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', url)
    if match:
        return match.group(1)
    if re.fullmatch(r'[a-zA-Z0-9-_]+', url):
        return url
    raise ValueError(f"Cannot extract document ID from: {url}")


def _docs_body_to_markdown(body: dict) -> str:
    """Convert Google Docs API body dict to markdown."""
    lines = []

    for element in body.get('content', []):
        if 'paragraph' in element:
            para = element['paragraph']
            style = para.get('paragraphStyle', {}).get('namedStyleType', 'NORMAL_TEXT')

            # Extract text from paragraph elements
            text = ''.join(
                run.get('textRun', {}).get('content', '')
                for run in para.get('elements', [])
            ).rstrip('\n')

            if not text.strip():
                lines.append('')
                continue

            # Map heading styles
            if style == 'HEADING_1':
                lines.append(f'# {text}')
            elif style == 'HEADING_2':
                lines.append(f'## {text}')
            elif style == 'HEADING_3':
                lines.append(f'### {text}')
            elif style == 'HEADING_4':
                lines.append(f'#### {text}')
            else:
                lines.append(text)
            lines.append('')

        elif 'table' in element:
            lines.append('*(table omitted)*')
            lines.append('')

    return '\n'.join(lines)


def pull_doc(document_id: str, source_url: str, *, service=None) -> list[Section]:
    """Fetch document from Google Docs API and return list of sections.

    Args:
        document_id: Google Docs document ID
        source_url: Source URL for metadata
        service: Optional Docs API service object for testing
    """
    if service is None:
        creds = get_authenticated_credentials()
        service = build('docs', 'v1', credentials=creds)

    # Fetch document with tab content
    document = service.documents().get(
        documentId=document_id,
        includeTabsContent=True,
    ).execute()

    doc_title = document.get('title', 'Untitled')
    raw_tabs = document.get('tabs', [])
    time_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    sections = []

    if raw_tabs:
        # Document has tabs
        for i, tab in enumerate(raw_tabs, start=1):
            props = tab.get('tabProperties', {})
            tab_title = props.get('title', f'Tab {i}')
            body = tab.get('documentTab', {}).get('body', {})
            content = _docs_body_to_markdown(body)

            sections.append(Section(
                title=doc_title,
                source=source_url,
                time=time_str,
                section=i,
                section_title=tab_title,
                content=content,
            ))
    else:
        # Single-section document (no tabs or old API)
        body = document.get('body', {})
        content = _docs_body_to_markdown(body)

        sections.append(Section(
            title=doc_title,
            source=source_url,
            time=time_str,
            section=1,
            section_title=doc_title,
            content=content,
        ))

    return sections


# =============================================================================
# Comments functions
# =============================================================================

def fetch_comments(document_id: str) -> list[Comment]:
    """Fetch comments from Google Drive API."""
    creds = get_authenticated_credentials()
    service = build('drive', 'v3', credentials=creds)

    comments = []
    page_token = None

    while True:
        result = service.comments().list(
            fileId=document_id,
            fields='comments(id,author,createdTime,quotedFileContent,content,resolved,replies(id,author,createdTime,content)),nextPageToken',
            pageToken=page_token,
        ).execute()

        for c in result.get('comments', []):
            # Parse date
            created = c.get('createdTime', '')
            date = created[:10] if created else ''

            # Author email
            author = c.get('author', {}).get('emailAddress', '')
            if not author:
                author = c.get('author', {}).get('displayName', 'Unknown')

            # Quoted text
            quoted = c.get('quotedFileContent', {}).get('value', '')

            # Replies
            replies = []
            for r in c.get('replies', []):
                r_created = r.get('createdTime', '')
                r_date = r_created[:10] if r_created else ''
                r_author = r.get('author', {}).get('emailAddress', '')
                if not r_author:
                    r_author = r.get('author', {}).get('displayName', 'Unknown')

                replies.append(CommentReply(
                    reply_id=r.get('id', ''),
                    author=r_author,
                    date=r_date,
                    content=r.get('content', ''),
                ))

            comments.append(Comment(
                comment_id=c.get('id', ''),
                author=author,
                date=date,
                quoted_text=quoted,
                content=c.get('content', ''),
                resolved=c.get('resolved', False),
                replies=replies,
            ))

        page_token = result.get('nextPageToken')
        if not page_token:
            break

    return comments


def format_comment(comment: Comment) -> str:
    """Format a single comment as markdown."""
    lines = []

    # Main comment line
    resolved_tag = ' [RESOLVED]' if comment.resolved else ''
    lines.append(f'* [{comment.comment_id}] {comment.date} - {comment.author}{resolved_tag}')

    # Quoted context
    if comment.quoted_text:
        # Truncate long quotes
        quoted = comment.quoted_text
        if len(quoted) > 80:
            quoted = quoted[:77] + '...'
        lines.append(f'  > "{quoted}"')

    # Comment content
    for line in comment.content.split('\n'):
        lines.append(f'  {line}')

    # Replies
    for reply in comment.replies:
        lines.append(f'  ↳ [{reply.reply_id}] {reply.date} - {reply.author}')
        for line in reply.content.split('\n'):
            lines.append(f'    {line}')

    return '\n'.join(lines)


def format_comments_section(
    comments: list[Comment],
    title: str,
    source: str,
    time_str: str,
    section_num: int,
    section_title: str,
) -> Section:
    """Format comments as a multipart section."""
    content_lines = []
    for comment in comments:
        content_lines.append(format_comment(comment))
        content_lines.append('')

    return Section(
        title=title,
        source=source,
        time=time_str,
        section=section_num,
        section_type='comments',
        section_title=f'{section_title} (Comments)',
        content='\n'.join(content_lines).strip(),
    )


# =============================================================================
# CLI commands
# =============================================================================

@click.group()
def doc():
    """Google Docs operations"""
    pass


def _add_comments_to_sections(
    sections: list[Section],
    document_id: str,
) -> list[Section]:
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
    result.append(format_comments_section(
        comments=comments,
        title=first_section.title,
        source=first_section.source,
        time_str=first_section.time,
        section_num=first_section.section,
        section_title=first_section.section_title,
    ))

    # Add remaining content sections (if multi-tab)
    for section in sections[1:]:
        result.append(section)

    return result


@doc.command()
@click.argument('url')
@click.option('--output', '-o', type=click.Path(path_type=Path), help='Output file (default: <title>.doc.gax)')
@click.option('--with-comments', is_flag=True, help='Include document comments as separate sections')
def clone(url: str, output: Optional[Path], with_comments: bool):
    """Clone a Google Doc to a local .doc.gax file."""
    try:
        document_id = extract_doc_id(url)
        source_url = f'https://docs.google.com/document/d/{document_id}/edit'

        click.echo(f'Fetching: {document_id}')
        sections = pull_doc(document_id, source_url)

        if with_comments:
            click.echo('Fetching comments...')
            sections = _add_comments_to_sections(sections, document_id)

        content = format_multipart(sections)

        if output:
            file_path = output
        else:
            # Generate filename from title
            safe_name = re.sub(r'[<>:"/\\|?*]', '-', sections[0].title)
            safe_name = re.sub(r'\s+', '_', safe_name)
            file_path = Path(f'{safe_name}.doc.gax')

        if file_path.exists():
            click.echo(f'Error: File already exists: {file_path}', err=True)
            sys.exit(1)

        file_path.write_text(content, encoding='utf-8')
        click.echo(f'Created: {file_path}')
        click.echo(f'Title: {sections[0].title}')
        click.echo(f'Sections: {len(sections)}')

    except Exception as e:
        click.echo(f'Error: {e}', err=True)
        sys.exit(1)


@doc.command()
@click.argument('file', type=click.Path(exists=True, path_type=Path))
@click.option('--with-comments', is_flag=True, help='Include document comments as separate sections')
def pull(file: Path, with_comments: bool):
    """Pull latest content from Google Docs to local file."""
    try:
        content = file.read_text(encoding='utf-8')
        sections = parse_multipart(content)

        if not sections:
            click.echo('Error: No sections found in file', err=True)
            sys.exit(1)

        source_url = sections[0].source
        if not source_url:
            click.echo('Error: No source URL found in file', err=True)
            sys.exit(1)

        document_id = extract_doc_id(source_url)
        click.echo(f'Pulling: {document_id}')

        new_sections = pull_doc(document_id, source_url)

        if with_comments:
            click.echo('Fetching comments...')
            new_sections = _add_comments_to_sections(new_sections, document_id)

        new_content = format_multipart(new_sections)

        file.write_text(new_content, encoding='utf-8')
        click.echo(f'Updated: {file}')
        click.echo(f'Sections: {len(new_sections)}')

    except Exception as e:
        click.echo(f'Error: {e}', err=True)
        sys.exit(1)


