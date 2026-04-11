"""Convert Markdown to Google Docs API requests.

Uses mistune for parsing. Supports:
- Headings (# to ######)
- Bold (**text**) and Italic (*text*)
- Hyperlinks ([text](url))
- Ordered lists (1. item) and Unordered lists (- item)
- Nested lists
- Code blocks (``` ... ```)
- Tables (| col | col |) with inline formatting in cells
- Paragraphs with inline formatting
"""

from dataclasses import dataclass, field

import mistune


def _utf16_len(s: str) -> int:
    """Length of s in UTF-16 code units (used by Google Docs API for indices).

    Characters outside the BMP (code points > U+FFFF, e.g. most emoji)
    occupy 2 UTF-16 code units but count as 1 in Python's len().
    """
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


# =============================================================================
# AST nodes (intermediate representation between mistune and Docs API)
# =============================================================================


@dataclass
class Node:
    """Base AST node."""
    pass


@dataclass
class Text(Node):
    text: str
    bold: bool = False
    italic: bool = False
    url: str | None = None


@dataclass
class Heading(Node):
    level: int  # 1-6
    children: list[Text] = field(default_factory=list)

    @property
    def text(self) -> str:
        return ''.join(c.text for c in self.children)


@dataclass
class Paragraph(Node):
    children: list[Text] = field(default_factory=list)


@dataclass
class ListItem(Node):
    children: list[Text] = field(default_factory=list)
    ordered: bool = False
    depth: int = 0


@dataclass
class CodeBlock(Node):
    text: str


@dataclass
class Table(Node):
    rows: list[list[list[Text]]]  # list of rows, each row is list of cells, each cell is list of Text spans


# =============================================================================
# Mistune AST -> our AST
# =============================================================================

_parser = mistune.create_markdown(renderer=None, plugins=['table'])


def _flatten_inline(tokens: list[dict], bold=False, italic=False, url=None) -> list[Text]:
    """Recursively flatten mistune inline tokens into Text spans."""
    result = []
    for tok in tokens:
        t = tok['type']
        if t == 'text':
            result.append(Text(tok['raw'], bold=bold, italic=italic, url=url))
        elif t == 'strong':
            result.extend(_flatten_inline(tok['children'], bold=True, italic=italic, url=url))
        elif t == 'emphasis':
            result.extend(_flatten_inline(tok['children'], bold=bold, italic=True, url=url))
        elif t == 'link':
            link_url = tok.get('attrs', {}).get('url', '')
            result.extend(_flatten_inline(tok['children'], bold=bold, italic=italic, url=link_url))
        elif t == 'codespan':
            result.append(Text(tok.get('raw', tok.get('text', '')), bold=bold, italic=italic, url=url))
        elif t == 'softbreak':
            result.append(Text('\n', bold=bold, italic=italic, url=url))
        elif t == 'block_text':
            result.extend(_flatten_inline(tok.get('children', []), bold=bold, italic=italic, url=url))
        else:
            # Fallback: extract raw text
            if 'raw' in tok:
                result.append(Text(tok['raw'], bold=bold, italic=italic, url=url))
            elif 'children' in tok:
                result.extend(_flatten_inline(tok['children'], bold=bold, italic=italic, url=url))
    return result


def _extract_list_items(list_tok: dict, depth: int = 0) -> list[ListItem]:
    """Extract ListItem nodes from a mistune list token, handling nesting."""
    ordered = list_tok.get('attrs', {}).get('ordered', False)
    items = []
    for item_tok in list_tok.get('children', []):
        if item_tok['type'] != 'list_item':
            continue
        inline_children = []
        for child in item_tok.get('children', []):
            if child['type'] == 'list':
                # Nested list
                items.extend(_extract_list_items(child, depth=depth + 1))
            else:
                inline_children.extend(_flatten_inline([child]))
        if inline_children:
            items.append(ListItem(children=inline_children, ordered=ordered, depth=depth))
    return items


def _table_to_rows(tok: dict) -> list[list[list[Text]]]:
    """Convert mistune table token to list of rows of parsed cells."""
    rows = []
    for section in tok.get('children', []):
        if section['type'] in ('table_head', 'table_body'):
            section_children = section.get('children', [])
            if section['type'] == 'table_head':
                # table_head contains cells directly (it IS the row)
                cells = []
                for cell_tok in section_children:
                    if cell_tok['type'] == 'table_cell':
                        cells.append(_flatten_inline(cell_tok.get('children', [])))
                if cells:
                    rows.append(cells)
            else:
                # table_body contains table_row children
                for row_tok in section_children:
                    if row_tok['type'] != 'table_row':
                        continue
                    cells = []
                    for cell_tok in row_tok.get('children', []):
                        if cell_tok['type'] != 'table_cell':
                            continue
                        cells.append(_flatten_inline(cell_tok.get('children', [])))
                    rows.append(cells)
    return rows


def _spans_to_md(spans: list[Text]) -> str:
    """Convert Text spans back to markdown string (for table cells)."""
    parts = []
    for s in spans:
        t = s.text
        if s.bold and s.italic:
            t = f'***{t}***'
        elif s.bold:
            t = f'**{t}**'
        elif s.italic:
            t = f'*{t}*'
        parts.append(t)
    return ''.join(parts)


def parse_inline(text: str) -> list[Text]:
    """Parse inline formatting from a markdown string.

    Public API used by table cell population in gdoc.py.
    """
    if not text or not text.strip():
        return [Text(text)]
    tokens = _parser(text)
    result = []
    for tok in tokens:
        if tok['type'] in ('paragraph', 'block_text'):
            result.extend(_flatten_inline(tok.get('children', [])))
        else:
            result.extend(_flatten_inline([tok]))
    return result if result else [Text(text)]


def parse_markdown(md: str) -> list[Node]:
    """Parse markdown into AST nodes using mistune."""
    tokens = _parser(md)
    nodes = []

    for tok in tokens:
        t = tok['type']

        if t == 'blank_line':
            continue

        elif t == 'heading':
            children = _flatten_inline(tok.get('children', []))
            nodes.append(Heading(level=tok['attrs']['level'], children=children))

        elif t == 'paragraph':
            children = _flatten_inline(tok.get('children', []))
            nodes.append(Paragraph(children=children))

        elif t == 'list':
            items = _extract_list_items(tok)
            nodes.extend(items)

        elif t == 'block_code':
            nodes.append(CodeBlock(text=tok.get('raw', '').rstrip('\n')))

        elif t == 'table':
            rows = _table_to_rows(tok)
            if rows:
                nodes.append(Table(rows=rows))

        elif t == 'block_quote':
            # Flatten block quote children as paragraphs
            for child in tok.get('children', []):
                if child['type'] == 'paragraph':
                    children = _flatten_inline(child.get('children', []))
                    nodes.append(Paragraph(children=children))

    return nodes


# =============================================================================
# Docs API request generation
# =============================================================================


def _append_inline(text_parts: list[str], format_actions: list, children: list[Text]):
    """Append inline-formatted text spans, tracking positions for bold/italic/link."""
    for child in children:
        child_start = sum(_utf16_len(p) for p in text_parts) + 1
        text_parts.append(child.text)
        child_end = child_start + _utf16_len(child.text)
        if child.bold:
            format_actions.append((child_start, child_end, 'bold', None))
        if child.italic:
            format_actions.append((child_start, child_end, 'italic', None))
        if child.url:
            format_actions.append((child_start, child_end, 'link', child.url))


def generate_requests(nodes: list[Node], tab_id: str | None = None) -> tuple[str, list[dict]]:
    """Generate Docs API requests from AST.

    Returns: (plain_text, list_of_formatting_requests)
    """
    text_parts = []
    format_actions = []  # (start, end, action_type, params)

    prev_node = None
    for node in nodes:
        # Insert blank line (empty paragraph) between nodes that need spacing.
        # Google Docs exports blank lines between paragraphs only if there is
        # an actual empty paragraph in the document.
        if prev_node is not None:
            needs_spacing = False
            # Blank line between consecutive paragraphs (but not between
            # consecutive "> " lines — those are code block workaround output
            # and must stay together without spacing to remain stable)
            if isinstance(prev_node, Paragraph) and isinstance(node, Paragraph):
                prev_text = ''.join(c.text for c in prev_node.children)
                curr_text = ''.join(c.text for c in node.children)
                if not (prev_text.startswith('> ') and curr_text.startswith('> ')):
                    needs_spacing = True
            # Blank line before/after headings (unless preceded by nothing)
            if isinstance(node, Heading) and not isinstance(prev_node, Heading):
                needs_spacing = True
            if isinstance(prev_node, Heading):
                needs_spacing = True
            # Blank line before/after lists (transition from non-list to list or vice versa)
            if isinstance(node, ListItem) and not isinstance(prev_node, ListItem):
                needs_spacing = True
            if isinstance(prev_node, ListItem) and not isinstance(node, ListItem):
                needs_spacing = True
            # Blank line before/after code blocks
            if isinstance(node, CodeBlock) or isinstance(prev_node, CodeBlock):
                needs_spacing = True
            # Blank line before/after tables
            if isinstance(node, Table) or isinstance(prev_node, Table):
                needs_spacing = True
            if needs_spacing:
                text_parts.append('\n')

        start = sum(_utf16_len(p) for p in text_parts) + 1  # 1-based index

        if isinstance(node, Heading):
            text_parts.append(node.text + '\n')
            end = start + _utf16_len(node.text)
            format_actions.append((start, end, 'heading', node.level))
            # Apply inline formatting within heading
            offset = start
            for child in node.children:
                child_end = offset + _utf16_len(child.text)
                if child.bold:
                    format_actions.append((offset, child_end, 'bold', None))
                if child.italic:
                    format_actions.append((offset, child_end, 'italic', None))
                if child.url:
                    format_actions.append((offset, child_end, 'link', child.url))
                offset = child_end

        elif isinstance(node, Paragraph):
            _append_inline(text_parts, format_actions, node.children)
            text_parts.append('\n')

        elif isinstance(node, ListItem):
            list_start = sum(_utf16_len(p) for p in text_parts) + 1
            _append_inline(text_parts, format_actions, node.children)
            text_parts.append('\n')
            list_end = sum(_utf16_len(p) for p in text_parts) + 1
            if node.ordered:
                format_actions.append((list_start, list_end - 1, 'ordered_list', node.depth))
            else:
                format_actions.append((list_start, list_end - 1, 'unordered_list', node.depth))

        elif isinstance(node, CodeBlock):
            # WORKAROUND: Google Docs has no native code block support.
            # Code fences (```) are stripped on pull, leaving bare lines that
            # get re-parsed as separate paragraphs with blank-line spacing
            # injected between them — causing instability across round-trips.
            # We project code blocks to a single paragraph with "> " prefix
            # on each line. This survives pull and re-push as one paragraph
            # (no inter-line spacing inserted). Google escapes ">" to "\>"
            # on export, so we unescape on the pull side (native_md.py).
            prefixed = '\n'.join(f'> {line}' for line in node.text.split('\n'))
            text_parts.append(prefixed + '\n')

        elif isinstance(node, Table):
            table_start = sum(_utf16_len(p) for p in text_parts) + 1
            num_rows = len(node.rows)
            num_cols = max(len(row) for row in node.rows) if node.rows else 0

            # Build placeholder text (will be replaced by actual table)
            def _cell_plain(spans: list[Text]) -> str:
                return ''.join(s.text for s in spans)
            table_text = '\n'.join(
                '\t'.join(_cell_plain(cell) for cell in row)
                for row in node.rows
            ) + '\n'
            text_parts.append(table_text)
            table_end = sum(_utf16_len(p) for p in text_parts) + 1

            format_actions.append((table_start, table_end - 1, 'table', (num_rows, num_cols, node.rows)))

        prev_node = node

    plain_text = ''.join(text_parts)
    total_utf16 = sum(_utf16_len(p) for p in text_parts)

    # Second pass: generate API requests
    requests = []

    # Insert text
    insert_loc = {'index': 1}
    if tab_id:
        insert_loc['tabId'] = tab_id
    requests.append({'insertText': {'text': plain_text, 'location': insert_loc}})

    # Split into two groups: non-table styles (applied first) and table operations
    # (applied last). Table ops (deleteContentRange + insertTable) shift positions
    # of content that follows them; applying non-table styles first ensures they
    # land on the correct paragraphs before positions shift.
    table_actions = [(s, e, a, p) for s, e, a, p in format_actions if a == 'table']
    other_actions = [(s, e, a, p) for s, e, a, p in format_actions if a != 'table']

    def _build_style_requests(actions):
        result = []
        for start, end, action, params in reversed(actions):
            range_spec = {'startIndex': start, 'endIndex': end}
            if tab_id:
                range_spec['tabId'] = tab_id

            if action == 'heading':
                style_map = {1: 'HEADING_1', 2: 'HEADING_2', 3: 'HEADING_3',
                            4: 'HEADING_4', 5: 'HEADING_5', 6: 'HEADING_6'}
                result.append({
                    'updateParagraphStyle': {
                        'range': range_spec,
                        'paragraphStyle': {'namedStyleType': style_map.get(params, 'HEADING_1')},
                        'fields': 'namedStyleType'
                    }
                })
            elif action == 'bold':
                result.append({
                    'updateTextStyle': {
                        'range': range_spec,
                        'textStyle': {'bold': True},
                        'fields': 'bold'
                    }
                })
            elif action == 'italic':
                result.append({
                    'updateTextStyle': {
                        'range': range_spec,
                        'textStyle': {'italic': True},
                        'fields': 'italic'
                    }
                })
            elif action == 'link':
                result.append({
                    'updateTextStyle': {
                        'range': range_spec,
                        'textStyle': {'link': {'url': params}},
                        'fields': 'link'
                    }
                })
            elif action == 'unordered_list':
                result.append({
                    'createParagraphBullets': {
                        'range': range_spec,
                        'bulletPreset': 'BULLET_DISC_CIRCLE_SQUARE',
                    }
                })
            elif action == 'ordered_list':
                result.append({
                    'createParagraphBullets': {
                        'range': range_spec,
                        'bulletPreset': 'NUMBERED_DECIMAL_NESTED',
                    }
                })
        return result

    # 1. Apply non-table styles (heading, bold, italic, link) at stable positions
    requests.extend(_build_style_requests(other_actions))

    # 2. Apply table operations last (delete placeholder + insert empty table).
    #    Process in reverse order so earlier tables' positions remain stable.
    for start, end, action, params in reversed(table_actions):
        # Cap endIndex to avoid deleting the segment-ending newline.
        # After inserting plain_text at index 1, the body spans indices
        # 1..total_utf16+1, with the trailing \n at total_utf16+1.
        # deleteContentRange endIndex is exclusive, so total_utf16+1 is the
        # max safe value (deletes up to but not including the trailing \n).
        if end > total_utf16 + 1:
            end = total_utf16 + 1
        range_spec = {'startIndex': start, 'endIndex': end}
        if tab_id:
            range_spec['tabId'] = tab_id

        num_rows, num_cols, rows = params
        # Delete the placeholder text and insert empty table
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


def extract_tables(md: str) -> list[list[list[list[Text]]]]:
    """Extract table cell data from markdown.

    Returns list of tables, each a list of rows, each a list of cells,
    each cell a list of Text spans.
    """
    nodes = parse_markdown(md)
    return [node.rows for node in nodes if isinstance(node, Table)]


def markdown_to_requests(md: str, tab_id: str | None = None) -> list[dict]:
    """Convert markdown to Docs API batchUpdate requests."""
    nodes = parse_markdown(md)
    _, requests = generate_requests(nodes, tab_id)
    return requests
