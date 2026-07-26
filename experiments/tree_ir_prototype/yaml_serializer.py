"""YAML serializer/parser for the enriched Tree IR.

Design goals (from ADR 035):
- Default elision: omit every attribute equal to its default
- Compact runs: plain string when unstyled, small map when styled
- No noise: no IDs, no revision metadata, no indexes in the body
- Token-lean: minimal YAML that an LLM can hold in context

Format: doc-tree/v1
"""

from __future__ import annotations

from typing import Any, Optional

import yaml

from .enriched_ir import (
    Block,
    Heading,
    ListItem,
    Paragraph,
    ParagraphStyle,
    Span,
    Table,
    TextStyle,
)


# =============================================================================
# Serialize: IR → YAML dict → YAML string
# =============================================================================


def _serialize_text_style(style: TextStyle) -> dict[str, Any]:
    """Serialize a TextStyle to a compact dict (omitting defaults)."""
    d: dict[str, Any] = {}
    if style.bold:
        d["b"] = True
    if style.italic:
        d["i"] = True
    if style.strikethrough:
        d["s"] = True
    if style.underline:
        d["u"] = True
    if style.url:
        d["url"] = style.url
    if style.foreground_color:
        d["color"] = style.foreground_color
    if style.background_color:
        d["bg"] = style.background_color
    if style.font_family:
        d["font"] = style.font_family
    if style.font_size:
        d["size"] = style.font_size
    if style.baseline_offset:
        d["baseline"] = style.baseline_offset
    if style.raw:
        d["raw"] = style.raw
    return d


def _serialize_para_style(style: ParagraphStyle) -> dict[str, Any]:
    """Serialize a ParagraphStyle to a compact dict (omitting defaults)."""
    d: dict[str, Any] = {}
    if style.alignment:
        d["align"] = style.alignment.lower()
    if style.indent_start:
        d["indent_start"] = style.indent_start
    if style.indent_end:
        d["indent_end"] = style.indent_end
    if style.indent_first_line:
        d["indent_first"] = style.indent_first_line
    if style.line_spacing:
        d["line_spacing"] = style.line_spacing
    if style.space_above:
        d["space_above"] = style.space_above
    if style.space_below:
        d["space_below"] = style.space_below
    if style.raw:
        d["raw"] = style.raw
    return d


def _serialize_span(span: Span) -> str | dict[str, Any]:
    """Serialize a span: plain string if unstyled, dict if styled."""
    if span.style.is_default():
        return span.text
    d = _serialize_text_style(span.style)
    d["t"] = span.text
    return d


def _serialize_runs(spans: list[Span]) -> list[str | dict] | str:
    """Serialize a list of runs.

    If there's only one unstyled run, return just the string.
    Otherwise return a list of run objects.
    """
    if len(spans) == 1 and spans[0].style.is_default():
        return spans[0].text
    return [_serialize_span(s) for s in spans]


def _serialize_block(block: Block) -> dict[str, Any]:
    """Serialize a single block to its YAML-dict representation."""
    if isinstance(block, Heading):
        runs = _serialize_runs(block.spans)
        ps = _serialize_para_style(block.para_style)
        # Compact: single unstyled heading → { hN: "text" }
        if isinstance(runs, str) and not ps:
            return {f"h{block.level}": runs}
        d: dict[str, Any] = {f"h{block.level}": {}}
        inner = d[f"h{block.level}"]
        if isinstance(runs, str):
            inner["t"] = runs
        else:
            inner["runs"] = runs
        if ps:
            inner["style"] = ps
        return d

    elif isinstance(block, Paragraph):
        runs = _serialize_runs(block.spans)
        ps = _serialize_para_style(block.para_style)
        raw = block.raw
        # Compact: single unstyled paragraph → { p: "text" }
        if isinstance(runs, str) and not ps and not raw:
            return {"p": runs}
        d = {}
        inner: dict[str, Any] = {}
        if isinstance(runs, str):
            inner["t"] = runs
        else:
            inner["runs"] = runs
        if ps:
            inner["style"] = ps
        if raw:
            inner["raw"] = raw
        d["p"] = inner
        return d

    elif isinstance(block, ListItem):
        runs = _serialize_runs(block.spans)
        ps = _serialize_para_style(block.para_style)
        kind = "ol" if block.ordered else "ul"
        # Compact: single unstyled list item → { ul: "text" } or { ol: "text" }
        if isinstance(runs, str) and not ps and block.depth == 0:
            return {kind: runs}
        d = {}
        inner = {}
        if isinstance(runs, str):
            inner["t"] = runs
        else:
            inner["runs"] = runs
        if block.depth > 0:
            inner["depth"] = block.depth
        if ps:
            inner["style"] = ps
        d[kind] = inner
        return d

    elif isinstance(block, Table):
        rows_data = []
        for ri, row in enumerate(block.rows):
            row_data = []
            for ci, cell_spans in enumerate(row):
                cell_runs = _serialize_runs(cell_spans)
                # Check if cell has non-default style
                cell_ps = None
                if block.cell_styles and ri < len(block.cell_styles) and ci < len(block.cell_styles[ri]):
                    ps_obj = block.cell_styles[ri][ci]
                    if not ps_obj.is_default():
                        cell_ps = _serialize_para_style(ps_obj)
                if cell_ps:
                    if isinstance(cell_runs, str):
                        row_data.append({"t": cell_runs, "style": cell_ps})
                    else:
                        row_data.append({"runs": cell_runs, "style": cell_ps})
                else:
                    row_data.append(cell_runs)
            rows_data.append(row_data)
        return {"table": {"rows": rows_data}}

    return {"unknown": str(block)}


def serialize_tree(blocks: list[Block], source: str = "") -> str:
    """Serialize a list of enriched IR blocks to YAML string."""
    body = [_serialize_block(b) for b in blocks]
    doc: dict[str, Any] = {}
    if source:
        doc["source"] = source
    doc["kind"] = "doc-tree/v1"
    doc["body"] = body

    return yaml.dump(
        doc,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


# =============================================================================
# Parse: YAML string → IR
# =============================================================================


def _parse_text_style(d: dict) -> TextStyle:
    """Parse a text style dict back to TextStyle."""
    return TextStyle(
        bold=d.get("b", False),
        italic=d.get("i", False),
        strikethrough=d.get("s", False),
        underline=d.get("u", False),
        url=d.get("url"),
        foreground_color=d.get("color"),
        background_color=d.get("bg"),
        font_family=d.get("font"),
        font_size=d.get("size"),
        baseline_offset=d.get("baseline"),
        raw=d.get("raw"),
    )


def _parse_para_style(d: dict) -> ParagraphStyle:
    """Parse a paragraph style dict back to ParagraphStyle."""
    alignment = d.get("align")
    if alignment:
        alignment = alignment.upper()
    return ParagraphStyle(
        alignment=alignment,
        indent_start=d.get("indent_start"),
        indent_end=d.get("indent_end"),
        indent_first_line=d.get("indent_first"),
        line_spacing=d.get("line_spacing"),
        space_above=d.get("space_above"),
        space_below=d.get("space_below"),
        raw=d.get("raw"),
    )


def _parse_span(item: str | dict) -> Span:
    """Parse a run (string or dict) back to Span."""
    if isinstance(item, str):
        return Span(text=item, style=TextStyle())
    # It's a dict with 't' for text and style keys
    text = item.get("t", "")
    style_keys = {k: v for k, v in item.items() if k != "t"}
    style = _parse_text_style(style_keys)
    return Span(text=text, style=style)


def _parse_runs(runs_data) -> list[Span]:
    """Parse runs data (string or list) to list of Spans."""
    if isinstance(runs_data, str):
        return [Span(text=runs_data, style=TextStyle())]
    if isinstance(runs_data, list):
        return [_parse_span(item) for item in runs_data]
    return []


def _parse_block(d: dict) -> Optional[Block]:
    """Parse a single block dict back to a Block."""
    # Heading
    for level in range(1, 7):
        key = f"h{level}"
        if key in d:
            val = d[key]
            if isinstance(val, str):
                return Heading(level=level, spans=[Span(text=val, style=TextStyle())])
            # Dict with runs/t and optional style
            if isinstance(val, dict):
                runs_data = val.get("runs", val.get("t", ""))
                spans = _parse_runs(runs_data)
                ps = _parse_para_style(val.get("style", {}))
                return Heading(level=level, spans=spans, para_style=ps)

    # Paragraph
    if "p" in d:
        val = d["p"]
        if isinstance(val, str):
            return Paragraph(spans=[Span(text=val, style=TextStyle())])
        if isinstance(val, dict):
            runs_data = val.get("runs", val.get("t", ""))
            spans = _parse_runs(runs_data)
            ps = _parse_para_style(val.get("style", {}))
            raw = val.get("raw")
            block = Paragraph(spans=spans, para_style=ps)
            block.raw = raw
            return block

    # List items
    for kind, ordered in [("ul", False), ("ol", True)]:
        if kind in d:
            val = d[kind]
            if isinstance(val, str):
                return ListItem(
                    spans=[Span(text=val, style=TextStyle())],
                    ordered=ordered,
                )
            if isinstance(val, dict):
                runs_data = val.get("runs", val.get("t", ""))
                spans = _parse_runs(runs_data)
                depth = val.get("depth", 0)
                ps = _parse_para_style(val.get("style", {}))
                return ListItem(
                    spans=spans,
                    ordered=ordered,
                    depth=depth,
                    para_style=ps,
                )

    # Table
    if "table" in d:
        table_data = d["table"]
        rows_raw = table_data.get("rows", [])
        rows: list[list[list[Span]]] = []
        cell_styles: list[list[ParagraphStyle]] = []
        for row_raw in rows_raw:
            row_spans: list[list[Span]] = []
            row_ps: list[ParagraphStyle] = []
            for cell_raw in row_raw:
                if isinstance(cell_raw, str):
                    row_spans.append([Span(text=cell_raw, style=TextStyle())])
                    row_ps.append(ParagraphStyle())
                elif isinstance(cell_raw, list):
                    row_spans.append(_parse_runs(cell_raw))
                    row_ps.append(ParagraphStyle())
                elif isinstance(cell_raw, dict):
                    runs_data = cell_raw.get("runs", cell_raw.get("t", ""))
                    row_spans.append(_parse_runs(runs_data))
                    row_ps.append(_parse_para_style(cell_raw.get("style", {})))
                else:
                    row_spans.append([])
                    row_ps.append(ParagraphStyle())
            rows.append(row_spans)
            cell_styles.append(row_ps)
        return Table(rows=rows, cell_styles=cell_styles)

    return None


def parse_tree(yaml_str: str) -> list[Block]:
    """Parse YAML tree string back to a list of enriched IR blocks."""
    doc = yaml.safe_load(yaml_str)
    if not doc or "body" not in doc:
        return []

    blocks: list[Block] = []
    for item in doc["body"]:
        block = _parse_block(item)
        if block:
            blocks.append(block)
    return blocks
