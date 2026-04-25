"""Unified intermediate representation for Google Docs ↔ Markdown.

Block/Span tree that serializes to both markdown (via mistune MarkdownRenderer)
and Google Docs API requests. See ADR 030.

Module structure
================

  Data types         — Span, Block, Heading, Paragraph, ListItem, CodeBlock, Table
  Warnings           — PushWarning, check_unsupported
  Markdown → IR      — from_tokens, from_markdown (parse direction)
  Doc JSON → IR      — from_doc_json (pull from Docs API)
  IR → Mistune AST   — span_to_token, to_tokens
  IR → Markdown      — GaxMarkdownRenderer, render_markdown
  IR → Docs API      — to_docs_requests (push direction)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import mistune
from mistune.core import BlockState
from mistune.renderers.markdown import MarkdownRenderer
from mistune.renderers._list import _render_list_item, _render_unordered_list
from mistune.util import strip_end

logger = logging.getLogger(__name__)


# =============================================================================
# Helpers
# =============================================================================


def _utf16_len(s: str) -> int:
    """Length of s in UTF-16 code units (used by Google Docs API for indices).

    Characters outside the BMP (code points > U+FFFF, e.g. most emoji)
    occupy 2 UTF-16 code units but count as 1 in Python's len().
    """
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


# =============================================================================
# Data types
# =============================================================================


@dataclass
class Span:
    """Inline text with formatting. Leaf of the tree."""

    text: str
    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    url: Optional[str] = None


@dataclass
class Block:
    """Base for block-level nodes."""

    # Google Docs index range (populated when loaded from Doc JSON).
    doc_range: Optional[tuple[int, int]] = field(default=None, repr=False)


@dataclass
class Heading(Block):
    level: int = 1
    spans: list[Span] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class Paragraph(Block):
    spans: list[Span] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class ListItem(Block):
    spans: list[Span] = field(default_factory=list)
    ordered: bool = False
    depth: int = 0

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class CodeBlock(Block):
    code: str = ""
    language: str = ""

    @property
    def text(self) -> str:
        return self.code


@dataclass
class Table(Block):
    rows: list[list[list[Span]]] = field(default_factory=list)
    # Raw Google Doc table JSON — only populated by from_doc_json,
    # used by diff_push for cell-level index resolution.
    _raw_table: Optional[dict] = field(default=None, repr=False, compare=False)


# =============================================================================
# Warnings
# =============================================================================


@dataclass
class PushWarning:
    """Warning about a feature that won't push faithfully."""

    feature: str
    reason: str  # "api_limitation" | "workaround" | "not_implemented"
    detail: str


def check_unsupported(blocks: list[Block]) -> list[PushWarning]:
    """Scan blocks for features that won't push faithfully."""
    warnings: list[PushWarning] = []
    seen: set[str] = set()

    for block in blocks:
        if (
            isinstance(block, ListItem)
            and block.depth > 0
            and "nested lists" not in seen
        ):
            seen.add("nested lists")
            warnings.append(
                PushWarning(
                    feature="nested lists",
                    reason="api_limitation",
                    detail="Nested list items will be flattened to top level (Docs API has no nesting-level support)",
                )
            )
        if isinstance(block, CodeBlock) and "code blocks" not in seen:
            seen.add("code blocks")
            warnings.append(
                PushWarning(
                    feature="code blocks",
                    reason="workaround",
                    detail='Code blocks are converted to "> " prefixed lines (Docs has no code block element)',
                )
            )

    return warnings


# =============================================================================
# Markdown → IR (parse direction)
# =============================================================================

_parser = mistune.create_markdown(renderer=None, plugins=["table", "strikethrough"])


def _flatten_inline(
    tokens: list[dict],
    bold: bool = False,
    italic: bool = False,
    strikethrough: bool = False,
    url: Optional[str] = None,
) -> list[Span]:
    """Recursively flatten mistune inline tokens into Span list."""
    result: list[Span] = []
    for tok in tokens:
        t = tok["type"]
        if t == "text":
            result.append(
                Span(
                    tok["raw"],
                    bold=bold,
                    italic=italic,
                    strikethrough=strikethrough,
                    url=url,
                )
            )
        elif t == "strong":
            result.extend(
                _flatten_inline(
                    tok["children"],
                    bold=True,
                    italic=italic,
                    strikethrough=strikethrough,
                    url=url,
                )
            )
        elif t == "emphasis":
            result.extend(
                _flatten_inline(
                    tok["children"],
                    bold=bold,
                    italic=True,
                    strikethrough=strikethrough,
                    url=url,
                )
            )
        elif t == "strikethrough":
            result.extend(
                _flatten_inline(
                    tok["children"],
                    bold=bold,
                    italic=italic,
                    strikethrough=True,
                    url=url,
                )
            )
        elif t == "link":
            link_url = tok.get("attrs", {}).get("url", "")
            result.extend(
                _flatten_inline(
                    tok["children"],
                    bold=bold,
                    italic=italic,
                    strikethrough=strikethrough,
                    url=link_url,
                )
            )
        elif t == "codespan":
            result.append(
                Span(
                    tok.get("raw", tok.get("text", "")),
                    bold=bold,
                    italic=italic,
                    strikethrough=strikethrough,
                    url=url,
                )
            )
        elif t == "softbreak":
            result.append(
                Span(
                    "\n", bold=bold, italic=italic, strikethrough=strikethrough, url=url
                )
            )
        elif t == "block_text":
            result.extend(
                _flatten_inline(
                    tok.get("children", []),
                    bold=bold,
                    italic=italic,
                    strikethrough=strikethrough,
                    url=url,
                )
            )
        else:
            if "raw" in tok:
                result.append(
                    Span(
                        tok["raw"],
                        bold=bold,
                        italic=italic,
                        strikethrough=strikethrough,
                        url=url,
                    )
                )
            elif "children" in tok:
                result.extend(
                    _flatten_inline(
                        tok["children"],
                        bold=bold,
                        italic=italic,
                        strikethrough=strikethrough,
                        url=url,
                    )
                )
    return result


def _extract_list_items(list_tok: dict, depth: int = 0) -> list[ListItem]:
    """Extract ListItem blocks from a mistune list token, handling nesting."""
    ordered = list_tok.get("attrs", {}).get("ordered", False)
    items: list[ListItem] = []
    for item_tok in list_tok.get("children", []):
        if item_tok["type"] != "list_item":
            continue
        inline_children: list[Span] = []
        nested: list[ListItem] = []
        for child in item_tok.get("children", []):
            if child["type"] == "list":
                nested.extend(_extract_list_items(child, depth=depth + 1))
            else:
                inline_children.extend(_flatten_inline([child]))
        if inline_children:
            items.append(ListItem(spans=inline_children, ordered=ordered, depth=depth))
        items.extend(nested)
    return items


def _decode_br_in_spans(spans: list[Span]) -> list[Span]:
    """Decode <br> tags back to newlines in span text."""
    result = []
    for span in spans:
        if "<br>" in span.text:
            result.append(
                Span(
                    span.text.replace("<br>", "\n"),
                    bold=span.bold,
                    italic=span.italic,
                    strikethrough=span.strikethrough,
                    url=span.url,
                )
            )
        else:
            result.append(span)
    return result


def _table_to_rows(tok: dict) -> list[list[list[Span]]]:
    """Convert mistune table token to list of rows of parsed cells."""
    rows: list[list[list[Span]]] = []
    for section in tok.get("children", []):
        if section["type"] in ("table_head", "table_body"):
            section_children = section.get("children", [])
            if section["type"] == "table_head":
                cells = []
                for cell_tok in section_children:
                    if cell_tok["type"] == "table_cell":
                        spans = _flatten_inline(cell_tok.get("children", []))
                        cells.append(_decode_br_in_spans(spans))
                if cells:
                    rows.append(cells)
            else:
                for row_tok in section_children:
                    if row_tok["type"] != "table_row":
                        continue
                    cells = []
                    for cell_tok in row_tok.get("children", []):
                        if cell_tok["type"] != "table_cell":
                            continue
                        spans = _flatten_inline(cell_tok.get("children", []))
                        cells.append(_decode_br_in_spans(spans))
                    rows.append(cells)
    return rows


def from_tokens(tokens: list[dict]) -> list[Block]:
    """Convert mistune AST tokens to Block list."""
    blocks: list[Block] = []
    for tok in tokens:
        t = tok["type"]
        if t == "blank_line":
            continue
        elif t == "heading":
            spans = _flatten_inline(tok.get("children", []))
            blocks.append(Heading(level=tok["attrs"]["level"], spans=spans))
        elif t == "paragraph":
            spans = _flatten_inline(tok.get("children", []))
            blocks.append(Paragraph(spans=spans))
        elif t == "list":
            blocks.extend(_extract_list_items(tok))
        elif t == "block_code":
            blocks.append(
                CodeBlock(
                    code=tok.get("raw", "").rstrip("\n"),
                    language=tok.get("attrs", {}).get("info", "") or "",
                )
            )
        elif t == "table":
            rows = _table_to_rows(tok)
            if rows:
                blocks.append(Table(rows=rows))
        elif t == "block_quote":
            for child in tok.get("children", []):
                if child["type"] == "paragraph":
                    spans = _flatten_inline(child.get("children", []))
                    blocks.append(Paragraph(spans=spans))
        elif t == "thematic_break":
            pass  # Not representable in Google Docs
    return blocks


def from_markdown(md: str) -> list[Block]:
    """Parse markdown string to Block list."""
    tokens = _parser(md)
    return from_tokens(tokens)


# =============================================================================
# Google Docs JSON → IR
# =============================================================================

HEADING_STYLES = {
    "HEADING_1": 1,
    "HEADING_2": 2,
    "HEADING_3": 3,
    "HEADING_4": 4,
    "HEADING_5": 5,
    "HEADING_6": 6,
}

# Inverse: level → named style (used by push and diff_push)
HEADING_STYLE_MAP = {v: k for k, v in HEADING_STYLES.items()}


def _spans_from_textruns(
    elements: list[dict],
    skipped: dict[str, int] | None = None,
    inline_objects: dict | None = None,
) -> list[Span]:
    """Convert Google Docs textRun elements to Span list.

    The last element's trailing newline is the paragraph boundary and is
    stripped.  Interior standalone newline runs are hard line breaks and
    are preserved.

    If *skipped* dict is passed, non-textRun element types are counted into it.
    If *inline_objects* is passed, inlineObjectElement refs are resolved to images.
    """
    spans: list[Span] = []
    for idx, elem in enumerate(elements):
        tr = elem.get("textRun")
        if not tr:
            # Handle inline images
            ioe = elem.get("inlineObjectElement")
            if ioe and inline_objects:
                obj_id = ioe.get("inlineObjectId", "")
                obj = inline_objects.get(obj_id, {})
                embedded = obj.get("inlineObjectProperties", {}).get("embeddedObject", {})
                content_uri = embedded.get("imageProperties", {}).get("contentUri", "")
                if content_uri:
                    title = embedded.get("title", "image")
                    spans.append(Span(text=f"![{title}]({content_uri})"))
                    continue

            # Track skipped element types
            elem_type = next(
                (k for k in elem if k not in ("startIndex", "endIndex")), "unknown"
            )
            if skipped is not None:
                skipped[elem_type] = skipped.get(elem_type, 0) + 1
            logger.debug("Skipped %s element at index %s", elem_type, elem.get("startIndex", "?"))
            continue
        text = tr["content"]
        is_last = idx == len(elements) - 1

        # Strip the paragraph-terminating newline (always on the last run)
        if is_last and text.endswith("\n"):
            text = text[:-1]

        if not text:
            continue

        style = tr.get("textStyle", {})
        spans.append(
            Span(
                text=text,
                bold=style.get("bold", False),
                italic=style.get("italic", False),
                strikethrough=style.get("strikethrough", False),
                url=style.get("link", {}).get("url") if "link" in style else None,
            )
        )
    return spans


def from_doc_json(
    body_content: list[dict],
    lists: Optional[dict] = None,
    inline_objects: Optional[dict] = None,
) -> list[Block]:
    """Walk Google Docs body content and produce Block list with doc_range.

    Args:
        body_content: The body.content array from documents().get()
        lists: The document's lists dict (for determining ordered vs unordered)
        inline_objects: The document's inlineObjects dict (for resolving images)
    """
    blocks: list[Block] = []
    skipped: dict[str, int] = {}

    for elem in body_content:
        start = elem.get("startIndex", 0)
        end = elem.get("endIndex", 0)
        doc_range = (start, end)

        # Table
        if "table" in elem:
            table_data = elem["table"]
            rows: list[list[list[Span]]] = []
            for row in table_data.get("tableRows", []):
                cells: list[list[Span]] = []
                for cell in row.get("tableCells", []):
                    cell_spans: list[Span] = []
                    for ce in cell.get("content", []):
                        if "paragraph" in ce:
                            cell_spans.extend(
                                _spans_from_textruns(
                                    ce["paragraph"].get("elements", []),
                                    skipped=skipped,
                                    inline_objects=inline_objects,
                                )
                            )
                    cells.append(cell_spans)
                rows.append(cells)
            blocks.append(Table(doc_range=doc_range, rows=rows, _raw_table=elem))
            continue

        if "paragraph" not in elem:
            continue

        para = elem["paragraph"]
        elements = para.get("elements", [])
        spans = _spans_from_textruns(elements, skipped=skipped, inline_objects=inline_objects)

        # Skip empty paragraphs
        if not spans:
            continue

        style = para.get("paragraphStyle", {})
        named_style = style.get("namedStyleType", "NORMAL_TEXT")
        bullet = para.get("bullet")

        # Heading
        if named_style in HEADING_STYLES:
            blocks.append(
                Heading(
                    doc_range=doc_range, level=HEADING_STYLES[named_style], spans=spans
                )
            )
            continue

        # List item
        if bullet is not None:
            nesting = bullet.get("nestingLevel", 0)
            list_id = bullet.get("listId", "")
            # Determine ordered vs unordered from the document's lists property
            ordered = False
            if lists and list_id in lists:
                nesting_levels = (
                    lists[list_id].get("listProperties", {}).get("nestingLevels", [])
                )
                if nesting < len(nesting_levels):
                    glyph = nesting_levels[nesting].get("glyphType", "")
                    ordered = glyph not in ("", "GLYPH_TYPE_UNSPECIFIED")
            blocks.append(
                ListItem(
                    doc_range=doc_range, spans=spans, ordered=ordered, depth=nesting
                )
            )
            continue

        # Regular paragraph
        blocks.append(Paragraph(doc_range=doc_range, spans=spans))

    if skipped:
        parts = ", ".join(f"{count} {typ}" for typ, count in sorted(skipped.items()))
        logger.warning(f"Skipped unsupported elements: {parts}")

    return blocks


# =============================================================================
# IR → Mistune AST tokens
# =============================================================================


def _span_to_token(span: Span) -> dict:
    """Convert a single Span to a mistune AST token (with formatting nesting)."""
    inner: dict = {"type": "text", "raw": span.text}
    if span.url:
        inner = {"type": "link", "attrs": {"url": span.url}, "children": [inner]}
    if span.strikethrough:
        inner = {"type": "strikethrough", "children": [inner]}
    if span.italic:
        inner = {"type": "emphasis", "children": [inner]}
    if span.bold:
        inner = {"type": "strong", "children": [inner]}
    return inner


def _spans_to_children(spans: list[Span]) -> list[dict]:
    """Convert a list of Spans to mistune inline token list."""
    return [_span_to_token(s) for s in spans]


def to_tokens(blocks: list[Block]) -> list[dict]:
    """Convert Block list to mistune AST tokens for rendering."""
    tokens: list[dict] = []

    # Group consecutive ListItems into list tokens
    i = 0
    while i < len(blocks):
        block = blocks[i]

        if isinstance(block, Heading):
            tokens.append(
                {
                    "type": "heading",
                    "attrs": {"level": block.level},
                    "style": "atx",
                    "children": _spans_to_children(block.spans),
                }
            )

        elif isinstance(block, Paragraph):
            tokens.append(
                {
                    "type": "paragraph",
                    "children": _spans_to_children(block.spans),
                }
            )

        elif isinstance(block, ListItem):
            # Collect consecutive list items, splitting when ordered/depth changes
            list_items: list[ListItem] = [block]
            i += 1
            while i < len(blocks) and isinstance(blocks[i], ListItem):
                item = blocks[i]
                assert isinstance(item, ListItem)
                if (
                    item.ordered != list_items[0].ordered
                    or item.depth != list_items[0].depth
                ):
                    break
                list_items.append(item)
                i += 1
            tokens.append(_list_items_to_token(list_items))
            continue  # i already advanced

        elif isinstance(block, CodeBlock):
            tokens.append(
                {
                    "type": "block_code",
                    "raw": block.code + "\n" if block.code else "\n",
                    "style": "fenced",
                    "marker": "```",
                    "attrs": {"info": block.language},
                }
            )

        elif isinstance(block, Table):
            tokens.append(_table_to_token(block))

        i += 1

    return tokens


def _list_items_to_token(items: list[ListItem]) -> dict:
    """Convert a sequence of ListItems into a mistune list token.

    Handles mixed ordered/unordered and nested items by building
    a flat list (matching how mistune parses simple lists).
    """
    if not items:
        return {"type": "paragraph", "children": []}

    first = items[0]
    children = []
    for item in items:
        children.append(
            {
                "type": "list_item",
                "children": [
                    {"type": "block_text", "children": _spans_to_children(item.spans)}
                ],
            }
        )

    return {
        "type": "list",
        "tight": True,
        "bullet": "." if first.ordered else "-",
        "attrs": {"depth": first.depth, "ordered": first.ordered},
        "children": children,
    }


def _table_to_token(table: Table) -> dict:
    """Convert a Table block to a mistune table token."""
    if not table.rows:
        return {"type": "paragraph", "children": []}

    head_cells = []
    for cell_spans in table.rows[0]:
        head_cells.append(
            {
                "type": "table_cell",
                "attrs": {"align": "left", "head": True},
                "children": _spans_to_children(cell_spans),
            }
        )

    body_rows = []
    for row in table.rows[1:]:
        cells = []
        for cell_spans in row:
            cells.append(
                {
                    "type": "table_cell",
                    "attrs": {"align": None, "head": False},
                    "children": _spans_to_children(cell_spans),
                }
            )
        body_rows.append({"type": "table_row", "children": cells})

    return {
        "type": "table",
        "children": [
            {"type": "table_head", "children": head_cells},
            {"type": "table_body", "children": body_rows},
        ],
    }


# =============================================================================
# IR → Markdown (via GaxMarkdownRenderer)
# =============================================================================


class GaxMarkdownRenderer(MarkdownRenderer):
    """MarkdownRenderer configured for Google Docs conventions.

    - Ordered lists always emit '1.' (Google renumbers on import)
    - Table separators use ':----' (4 dashes, left-aligned)
    - Strikethrough and table plugins supported
    """

    def list(self, token, state):
        attrs = token["attrs"]
        if attrs["ordered"]:
            # Google convention: always emit "1." for every item
            parent = {
                "leading": "1" + token["bullet"] + " ",
                "tight": token["tight"],
            }
            children = [
                _render_list_item(self, parent, item, state)
                for item in token["children"]
            ]
        else:
            children = list(_render_unordered_list(self, token, state))

        text = "".join(children)
        parent_tok = token.get("parent")
        if parent_tok:
            if parent_tok["tight"]:
                return text
            return text + "\n"
        return strip_end(text) + "\n"


def _render_strikethrough(renderer, token, state):
    return "~~" + renderer.render_children(token, state) + "~~"


def _render_table(renderer, token, state):
    children = token["children"]
    head = children[0]
    body = children[1] if len(children) > 1 else None

    head_cells = []
    aligns = []
    for cell in head["children"]:
        text = renderer.render_children(cell, state).strip()
        head_cells.append(text.replace("\n", "<br>"))
        aligns.append(cell.get("attrs", {}).get("align"))

    lines = ["| " + " | ".join(head_cells) + " |"]

    seps = []
    for a in aligns:
        if a == "left":
            seps.append(":----")
        elif a == "right":
            seps.append("----:")
        elif a == "center":
            seps.append(":----:")
        else:
            seps.append("----")
    lines.append("| " + " | ".join(seps) + " |")

    if body:
        for row in body["children"]:
            cells = []
            for c in row["children"]:
                text = renderer.render_children(c, state).strip()
                cells.append(text.replace("\n", "<br>"))
            lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines) + "\n\n"


def _render_noop(renderer, token, state):
    return ""


def _make_renderer() -> GaxMarkdownRenderer:
    """Create a configured GaxMarkdownRenderer instance."""
    renderer = GaxMarkdownRenderer()
    renderer.register("strikethrough", _render_strikethrough)
    renderer.register("table", _render_table)
    renderer.register("table_head", _render_noop)
    renderer.register("table_body", _render_noop)
    renderer.register("table_row", _render_noop)
    renderer.register("table_cell", _render_noop)
    return renderer


_renderer = _make_renderer()


def render_markdown(blocks: list[Block]) -> str:
    """Render Block list to markdown string via mistune MarkdownRenderer."""
    tokens = to_tokens(blocks)
    state = BlockState()
    state.env = {"ref_links": {}}
    text = _renderer.render_tokens(tokens, state)
    # Normalize: single trailing newline
    text = text.rstrip("\n") + "\n"
    return text


# =============================================================================
# IR → Google Docs API requests (push direction)
# =============================================================================


def _append_inline(text_parts: list[str], format_actions: list, spans: list[Span]):
    """Append inline-formatted text spans, tracking positions for styling."""
    for span in spans:
        child_start = sum(_utf16_len(p) for p in text_parts) + 1
        text_parts.append(span.text)
        child_end = child_start + _utf16_len(span.text)
        if span.bold:
            format_actions.append((child_start, child_end, "bold", None))
        if span.italic:
            format_actions.append((child_start, child_end, "italic", None))
        if span.strikethrough:
            format_actions.append((child_start, child_end, "strikethrough", None))
        if span.url:
            format_actions.append((child_start, child_end, "link", span.url))


def _generate_requests(
    blocks: list[Block], tab_id: str | None = None
) -> tuple[str, list[dict]]:
    """Generate Docs API requests from Block list.

    Returns: (plain_text, list_of_requests)
    """
    text_parts: list[str] = []
    format_actions: list[tuple] = []

    prev_block = None
    for block in blocks:
        # Insert blank line between blocks that need spacing
        if prev_block is not None:
            needs_spacing = False
            if isinstance(prev_block, Paragraph) and isinstance(block, Paragraph):
                prev_text = prev_block.text
                curr_text = block.text
                if not (prev_text.startswith("> ") and curr_text.startswith("> ")):
                    needs_spacing = True
            if isinstance(block, Heading) and not isinstance(prev_block, Heading):
                needs_spacing = True
            if isinstance(prev_block, Heading):
                needs_spacing = True
            if isinstance(block, ListItem) and not isinstance(prev_block, ListItem):
                needs_spacing = True
            if isinstance(prev_block, ListItem) and not isinstance(block, ListItem):
                needs_spacing = True
            if isinstance(block, CodeBlock) or isinstance(prev_block, CodeBlock):
                needs_spacing = True
            if isinstance(block, Table) or isinstance(prev_block, Table):
                needs_spacing = True
            if needs_spacing:
                text_parts.append("\n")

        start = sum(_utf16_len(p) for p in text_parts) + 1

        if isinstance(block, Heading):
            text_parts.append(block.text + "\n")
            end = start + _utf16_len(block.text)
            format_actions.append((start, end, "heading", block.level))
            offset = start
            for span in block.spans:
                span_end = offset + _utf16_len(span.text)
                if span.bold:
                    format_actions.append((offset, span_end, "bold", None))
                if span.italic:
                    format_actions.append((offset, span_end, "italic", None))
                if span.strikethrough:
                    format_actions.append((offset, span_end, "strikethrough", None))
                if span.url:
                    format_actions.append((offset, span_end, "link", span.url))
                offset = span_end

        elif isinstance(block, Paragraph):
            _append_inline(text_parts, format_actions, block.spans)
            text_parts.append("\n")

        elif isinstance(block, ListItem):
            list_start = sum(_utf16_len(p) for p in text_parts) + 1
            _append_inline(text_parts, format_actions, block.spans)
            text_parts.append("\n")
            list_end = sum(_utf16_len(p) for p in text_parts) + 1
            if block.ordered:
                format_actions.append(
                    (list_start, list_end - 1, "ordered_list", block.depth)
                )
            else:
                format_actions.append(
                    (list_start, list_end - 1, "unordered_list", block.depth)
                )

        elif isinstance(block, CodeBlock):
            prefixed = "\n".join(f"> {line}" for line in block.code.split("\n"))
            text_parts.append(prefixed + "\n")

        elif isinstance(block, Table):
            table_start = sum(_utf16_len(p) for p in text_parts) + 1
            num_rows = len(block.rows)
            num_cols = max(len(row) for row in block.rows) if block.rows else 0

            def _cell_plain(spans: list[Span]) -> str:
                return "".join(s.text for s in spans)

            table_text = (
                "\n".join(
                    "\t".join(_cell_plain(cell) for cell in row) for row in block.rows
                )
                + "\n"
            )
            text_parts.append(table_text)
            table_end = sum(_utf16_len(p) for p in text_parts) + 1
            format_actions.append(
                (table_start, table_end - 1, "table", (num_rows, num_cols, block.rows))
            )

        prev_block = block

    plain_text = "".join(text_parts)
    total_utf16 = sum(_utf16_len(p) for p in text_parts)

    # Build API requests
    requests: list[dict] = []

    insert_loc: dict = {"index": 1}
    if tab_id:
        insert_loc["tabId"] = tab_id
    requests.append({"insertText": {"text": plain_text, "location": insert_loc}})

    table_actions = [(s, e, a, p) for s, e, a, p in format_actions if a == "table"]
    other_actions = [(s, e, a, p) for s, e, a, p in format_actions if a != "table"]

    def _build_style_requests(actions):
        result = []
        for start, end, action, params in reversed(actions):
            range_spec: dict = {"startIndex": start, "endIndex": end}
            if tab_id:
                range_spec["tabId"] = tab_id

            if action == "heading":
                result.append(
                    {
                        "updateParagraphStyle": {
                            "range": range_spec,
                            "paragraphStyle": {
                                "namedStyleType": HEADING_STYLE_MAP.get(
                                    params, "HEADING_1"
                                )
                            },
                            "fields": "namedStyleType",
                        }
                    }
                )
            elif action == "bold":
                result.append(
                    {
                        "updateTextStyle": {
                            "range": range_spec,
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        }
                    }
                )
            elif action == "italic":
                result.append(
                    {
                        "updateTextStyle": {
                            "range": range_spec,
                            "textStyle": {"italic": True},
                            "fields": "italic",
                        }
                    }
                )
            elif action == "strikethrough":
                result.append(
                    {
                        "updateTextStyle": {
                            "range": range_spec,
                            "textStyle": {"strikethrough": True},
                            "fields": "strikethrough",
                        }
                    }
                )
            elif action == "link":
                result.append(
                    {
                        "updateTextStyle": {
                            "range": range_spec,
                            "textStyle": {"link": {"url": params}},
                            "fields": "link",
                        }
                    }
                )
            elif action == "unordered_list":
                result.append(
                    {
                        "createParagraphBullets": {
                            "range": range_spec,
                            "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                        }
                    }
                )
            elif action == "ordered_list":
                result.append(
                    {
                        "createParagraphBullets": {
                            "range": range_spec,
                            "bulletPreset": "NUMBERED_DECIMAL_NESTED",
                        }
                    }
                )
        return result

    requests.extend(_build_style_requests(other_actions))

    for start, end, action, params in reversed(table_actions):
        if end > total_utf16 + 1:
            end = total_utf16 + 1
        range_spec: dict = {"startIndex": start, "endIndex": end}
        if tab_id:
            range_spec["tabId"] = tab_id

        num_rows, num_cols, rows = params
        requests.append({"deleteContentRange": {"range": range_spec}})
        table_loc: dict = {"index": start}
        if tab_id:
            table_loc["tabId"] = tab_id
        requests.append(
            {
                "insertTable": {
                    "rows": num_rows,
                    "columns": num_cols,
                    "location": table_loc,
                }
            }
        )

    return plain_text, requests


def to_docs_requests(
    blocks: list[Block], tab_id: str | None = None
) -> tuple[list[dict], list[list[list[list[Span]]]], list[PushWarning]]:
    """Convert Block list to Docs API batchUpdate requests.

    Returns: (requests, tables_data, warnings)
    """
    _, requests = _generate_requests(blocks, tab_id)
    tables = [block.rows for block in blocks if isinstance(block, Table)]
    warnings = check_unsupported(blocks)
    return requests, tables, warnings
