"""Convert Markdown to Google Docs API requests.

AST-based approach supporting:
- Headings (# to ######)
- Bold (**text**) and Italic (*text*)
- Ordered lists (1. item)
- Unordered lists (- item, * item)
- Code blocks (``` ... ```)
- Tables (| col | col |) with inline formatting in cells
- Paragraphs with inline formatting
"""

import re
from dataclasses import dataclass, field


def _utf16_len(s: str) -> int:
    """Length of s in UTF-16 code units (used by Google Docs API for indices).

    Characters outside the BMP (code points > U+FFFF, e.g. most emoji)
    occupy 2 UTF-16 code units but count as 1 in Python's len().
    """
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


@dataclass
class Node:
    """Base AST node."""
    pass


@dataclass
class Text(Node):
    text: str
    bold: bool = False
    italic: bool = False


@dataclass
class Heading(Node):
    level: int  # 1-6
    text: str


@dataclass
class Paragraph(Node):
    children: list[Text]


@dataclass
class ListItem(Node):
    children: list[Text]
    ordered: bool = False


@dataclass
class CodeBlock(Node):
    text: str


@dataclass
class Table(Node):
    rows: list[list[str]]  # list of rows, each row is list of cell strings


def _unescape_md(text: str) -> str:
    """Unescape markdown backslash sequences (e.g. r'\\_' -> '_', r'\\-' -> '-')."""
    return re.sub(r'\\(.)', r'\1', text)


def parse_inline(text: str) -> list[Text]:
    """Parse inline formatting (bold, italic)."""
    result = []
    # Pattern: **bold**, *italic*, ***bold+italic***
    pattern = re.compile(r'(\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*)')

    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            result.append(Text(_unescape_md(text[pos:m.start()])))

        if m.group(2):  # ***bold+italic***
            result.append(Text(_unescape_md(m.group(2)), bold=True, italic=True))
        elif m.group(3):  # **bold**
            result.append(Text(_unescape_md(m.group(3)), bold=True))
        elif m.group(4):  # *italic*
            result.append(Text(_unescape_md(m.group(4)), italic=True))

        pos = m.end()

    if pos < len(text):
        result.append(Text(_unescape_md(text[pos:])))

    return result if result else [Text(text)]


def parse_markdown(md: str) -> list[Node]:
    """Parse markdown into AST nodes."""
    nodes = []
    lines = md.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]

        # Empty line
        if not line.strip():
            i += 1
            continue

        # Code block
        if line.strip().startswith('```'):
            i += 1
            code_lines = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # skip closing ```
            nodes.append(CodeBlock(text='\n'.join(code_lines)))
            continue

        # Heading
        m = re.match(r'^(#{1,6})\s+(.+)$', line)
        if m:
            nodes.append(Heading(level=len(m.group(1)), text=m.group(2)))
            i += 1
            continue

        # Table (starts with |)
        if line.strip().startswith('|'):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                row_line = lines[i].strip()
                # Skip separator row (|---|---|)
                if re.match(r'^\|[\s\-:|]+\|$', row_line):
                    i += 1
                    continue
                # Parse cells
                cells = [c.strip() for c in row_line.split('|')[1:-1]]
                rows.append(cells)
                i += 1
            if rows:
                nodes.append(Table(rows=rows))
            continue

        # Unordered list item (- or *)
        m = re.match(r'^[-*]\s+(.+)$', line)
        if m:
            nodes.append(ListItem(children=parse_inline(m.group(1)), ordered=False))
            i += 1
            continue

        # Ordered list item (1. 2. etc)
        m = re.match(r'^\d+\.\s+(.+)$', line)
        if m:
            nodes.append(ListItem(children=parse_inline(m.group(1)), ordered=True))
            i += 1
            continue

        # Regular paragraph
        nodes.append(Paragraph(children=parse_inline(line)))
        i += 1

    return nodes


def _append_inline(text_parts: list[str], format_actions: list, children: list[Text]):
    """Append inline-formatted text spans, tracking positions for bold/italic."""
    for child in children:
        child_start = sum(_utf16_len(p) for p in text_parts) + 1
        text_parts.append(child.text)
        child_end = child_start + _utf16_len(child.text)
        if child.bold:
            format_actions.append((child_start, child_end, 'bold', None))
        if child.italic:
            format_actions.append((child_start, child_end, 'italic', None))


def generate_requests(nodes: list[Node], tab_id: str | None = None) -> tuple[str, list[dict]]:
    """Generate Docs API requests from AST.

    Returns: (plain_text, list_of_formatting_requests)
    """
    text_parts = []
    format_actions = []  # (start, end, action_type, params)

    for node in nodes:
        start = sum(_utf16_len(p) for p in text_parts) + 1  # 1-based index

        if isinstance(node, Heading):
            text_parts.append(node.text + '\n')
            end = start + _utf16_len(node.text)
            format_actions.append((start, end, 'heading', node.level))

        elif isinstance(node, Paragraph):
            _append_inline(text_parts, format_actions, node.children)
            text_parts.append('\n')

        elif isinstance(node, ListItem):
            list_start = sum(_utf16_len(p) for p in text_parts) + 1
            _append_inline(text_parts, format_actions, node.children)
            text_parts.append('\n')
            list_end = sum(_utf16_len(p) for p in text_parts) + 1
            bullet_type = 'ordered_list' if node.ordered else 'unordered_list'
            format_actions.append((list_start, list_end - 1, bullet_type, None))

        elif isinstance(node, CodeBlock):
            # Push as a plain paragraph — Drive API doesn't export Courier New
            # text as code block syntax on pull, so just preserve the content.
            text_parts.append(node.text + '\n')

        elif isinstance(node, Table):
            table_start = sum(_utf16_len(p) for p in text_parts) + 1
            num_rows = len(node.rows)
            num_cols = max(len(row) for row in node.rows) if node.rows else 0

            # Build placeholder text (will be replaced by actual table)
            table_text = '\n'.join('\t'.join(row) for row in node.rows) + '\n'
            text_parts.append(table_text)
            table_end = sum(_utf16_len(p) for p in text_parts) + 1

            format_actions.append((table_start, table_end - 1, 'table', (num_rows, num_cols, node.rows)))

    plain_text = ''.join(text_parts)

    # Second pass: generate API requests
    requests = []

    # Insert text
    insert_loc = {'index': 1}
    if tab_id:
        insert_loc['tabId'] = tab_id
    requests.append({'insertText': {'text': plain_text, 'location': insert_loc}})

    # Apply formatting (in reverse order for stable indices)
    for start, end, action, params in reversed(format_actions):
        range_spec = {'startIndex': start, 'endIndex': end}
        if tab_id:
            range_spec['tabId'] = tab_id

        if action == 'heading':
            style_map = {1: 'HEADING_1', 2: 'HEADING_2', 3: 'HEADING_3',
                        4: 'HEADING_4', 5: 'HEADING_5', 6: 'HEADING_6'}
            requests.append({
                'updateParagraphStyle': {
                    'range': range_spec,
                    'paragraphStyle': {'namedStyleType': style_map.get(params, 'HEADING_1')},
                    'fields': 'namedStyleType'
                }
            })
        elif action == 'bold':
            requests.append({
                'updateTextStyle': {
                    'range': range_spec,
                    'textStyle': {'bold': True},
                    'fields': 'bold'
                }
            })
        elif action == 'italic':
            requests.append({
                'updateTextStyle': {
                    'range': range_spec,
                    'textStyle': {'italic': True},
                    'fields': 'italic'
                }
            })
        elif action == 'unordered_list':
            requests.append({
                'createParagraphBullets': {
                    'range': range_spec,
                    'bulletPreset': 'BULLET_DISC_CIRCLE_SQUARE',
                }
            })
        elif action == 'ordered_list':
            requests.append({
                'createParagraphBullets': {
                    'range': range_spec,
                    'bulletPreset': 'NUMBERED_DECIMAL_NESTED',
                }
            })
        elif action == 'code_block':
            # Monospace font for code blocks
            requests.append({
                'updateTextStyle': {
                    'range': range_spec,
                    'textStyle': {
                        'weightedFontFamily': {
                            'fontFamily': 'Courier New',
                            'weight': 400,
                        },
                    },
                    'fields': 'weightedFontFamily'
                }
            })
        elif action == 'table':
            num_rows, num_cols, rows = params
            # Delete the placeholder text and insert table
            requests.append({'deleteContentRange': {'range': range_spec}})
            table_loc = {'index': start}
            if tab_id:
                table_loc['tabId'] = tab_id
            requests.append({
                'insertTable': {
                    'rows': num_rows,
                    'columns': num_cols,
                    'location': table_loc
                }
            })

    return plain_text, requests


def extract_tables(md: str) -> list[list[list[str]]]:
    """Extract table cell data from markdown.

    Returns list of tables, each a list of rows, each a list of cell strings.
    """
    nodes = parse_markdown(md)
    return [node.rows for node in nodes if isinstance(node, Table)]


def markdown_to_requests(md: str, tab_id: str | None = None) -> list[dict]:
    """Convert markdown to Docs API batchUpdate requests."""
    nodes = parse_markdown(md)
    _, requests = generate_requests(nodes, tab_id)
    return requests
