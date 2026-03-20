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
        f'section_title: {section.section_title}',
    ]

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


def pull_doc(document_id: str, source_url: str) -> list[Section]:
    """Fetch document from Google Docs API and return list of sections."""
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
# CLI commands
# =============================================================================

@click.group()
def gdoc():
    """Google Docs operations"""
    pass


@gdoc.command()
@click.argument('url')
@click.option('--output', '-o', type=click.Path(path_type=Path), help='Output file (default: <title>.doc.gax)')
def init(url: str, output: Optional[Path]):
    """Initialize a .doc.gax file from a Google Docs URL."""
    try:
        document_id = extract_doc_id(url)
        source_url = f'https://docs.google.com/document/d/{document_id}/edit'

        click.echo(f'Fetching: {document_id}')
        sections = pull_doc(document_id, source_url)
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


@gdoc.command()
@click.argument('file', type=click.Path(exists=True, path_type=Path))
def pull(file: Path):
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
        new_content = format_multipart(new_sections)

        file.write_text(new_content, encoding='utf-8')
        click.echo(f'Updated: {file}')
        click.echo(f'Sections: {len(new_sections)}')

    except Exception as e:
        click.echo(f'Error: {e}', err=True)
        sys.exit(1)


@gdoc.command()
@click.argument('url')
def cat(url: str):
    """Print a Google Doc as multipart markdown to stdout."""
    try:
        document_id = extract_doc_id(url)
        source_url = f'https://docs.google.com/document/d/{document_id}/edit'

        sections = pull_doc(document_id, source_url)
        content = format_multipart(sections)
        click.echo(content)

    except Exception as e:
        click.echo(f'Error: {e}', err=True)
        sys.exit(1)
