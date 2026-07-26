"""Three-way diff and plan engine for Tree IR.

Computes minimal mutations by diffing base IR vs local IR,
then translates to Docs API batchUpdate requests.

Key design points (ADR 034):
- Run boundaries are non-semantic: different splits of same text+style → zero mutations
- Style-only edits produce updateTextStyle, NOT delete+insert
- Paragraph style edits produce updateParagraphStyle
- Untouched content produces zero mutations
- Mutations applied in reverse index order
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Optional

from .enriched_ir import (
    _utf16_len,
    Block,
    Heading,
    ListItem,
    Paragraph,
    ParagraphStyle,
    Span,
    Table,
    TextStyle,
    HEADING_STYLE_MAP,
    _hex_to_color,
)


# =============================================================================
# Normalize: flatten runs to canonical form for comparison
# =============================================================================


def _normalize_spans(spans: list[Span]) -> list[Span]:
    """Merge adjacent spans with identical style into one span.

    This makes run boundaries non-semantic: two IRs that represent the same
    text+style with different run splits will normalize to identical spans.
    """
    if not spans:
        return []
    result: list[Span] = []
    current = Span(text=spans[0].text, style=spans[0].style)
    for span in spans[1:]:
        if span.style.style_equal(current.style) and span.style.raw == current.style.raw:
            current = Span(text=current.text + span.text, style=current.style)
        else:
            result.append(current)
            current = Span(text=span.text, style=span.style)
    result.append(current)
    return result


def _get_block_text(block: Block) -> str:
    """Get the plain text of a block."""
    if isinstance(block, (Heading, Paragraph, ListItem)):
        return block.text
    elif isinstance(block, Table):
        parts = []
        for row in block.rows:
            for cell in row:
                parts.append("".join(s.text for s in cell))
        return "|".join(parts)
    return ""


def _get_block_spans(block: Block) -> list[Span]:
    """Get the spans of a block (for paragraph-like blocks)."""
    if isinstance(block, (Heading, Paragraph, ListItem)):
        return block.spans
    return []


def _get_para_style(block: Block) -> ParagraphStyle:
    """Get the paragraph style of a block."""
    if isinstance(block, (Heading, Paragraph, ListItem)):
        return block.para_style
    return ParagraphStyle()


# =============================================================================
# Diff: compare base and local IR
# =============================================================================


@dataclass
class Mutation:
    """A single planned mutation."""

    type: str  # "delete_text", "insert_text", "update_text_style",
    # "update_paragraph_style", "delete_block", "insert_block"
    description: str  # Human-readable summary
    # Fields for API translation (populated during plan)
    start_index: Optional[int] = None
    end_index: Optional[int] = None
    text: Optional[str] = None
    style: Optional[dict] = None
    fields: Optional[str] = None
    block: Optional[Block] = None


@dataclass
class Plan:
    """A complete edit plan: list of mutations with preview."""

    mutations: list[Mutation]
    summary: list[str]

    @property
    def is_empty(self) -> bool:
        return len(self.mutations) == 0


def _diff_spans_for_text_edit(
    base_spans: list[Span],
    local_spans: list[Span],
    block_start: int,
    tab_id: str,
) -> list[Mutation]:
    """Diff spans at the run level to find text edits.

    Returns mutations for text changes within a paragraph.
    Uses character-level diff for surgical edits.
    """
    base_norm = _normalize_spans(base_spans)
    local_norm = _normalize_spans(local_spans)

    base_text = "".join(s.text for s in base_norm)
    local_text = "".join(s.text for s in local_norm)

    if base_text == local_text:
        return []  # No text change — style-only handled elsewhere

    # Character-level diff using SequenceMatcher
    sm = difflib.SequenceMatcher(None, base_text, local_text)
    mutations: list[Mutation] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        elif tag == "replace":
            # Delete old text, insert new text
            del_start = block_start + _utf16_len(base_text[:i1])
            del_end = block_start + _utf16_len(base_text[:i2])
            new_text = local_text[j1:j2]
            mutations.append(Mutation(
                type="delete_text",
                description=f"Delete '{base_text[i1:i2][:30]}'",
                start_index=del_start,
                end_index=del_end,
            ))
            mutations.append(Mutation(
                type="insert_text",
                description=f"Insert '{new_text[:30]}'",
                start_index=del_start,
                text=new_text,
            ))
        elif tag == "delete":
            del_start = block_start + _utf16_len(base_text[:i1])
            del_end = block_start + _utf16_len(base_text[:i2])
            mutations.append(Mutation(
                type="delete_text",
                description=f"Delete '{base_text[i1:i2][:30]}'",
                start_index=del_start,
                end_index=del_end,
            ))
        elif tag == "insert":
            ins_point = block_start + _utf16_len(base_text[:i1])
            new_text = local_text[j1:j2]
            mutations.append(Mutation(
                type="insert_text",
                description=f"Insert '{new_text[:30]}'",
                start_index=ins_point,
                text=new_text,
            ))

    return mutations


def _diff_text_styles(
    base_spans: list[Span],
    local_spans: list[Span],
    block_start: int,
    tab_id: str,
) -> list[Mutation]:
    """Find style-only changes (no text change assumed).

    Walk the local text character-by-character, map each char back to its
    base style, and emit updateTextStyle for any differences.
    """
    base_norm = _normalize_spans(base_spans)
    local_norm = _normalize_spans(local_spans)

    # Build character→style mapping for both
    def _char_styles(spans: list[Span]) -> list[TextStyle]:
        result = []
        for s in spans:
            for _ in s.text:
                result.append(s.style)
        return result

    base_styles = _char_styles(base_norm)
    local_styles = _char_styles(local_norm)

    base_text = "".join(s.text for s in base_norm)
    local_text = "".join(s.text for s in local_norm)

    # Only handle style diffs when text is identical
    if base_text != local_text:
        return []

    mutations: list[Mutation] = []

    # Find contiguous ranges where style differs
    i = 0
    while i < len(base_styles):
        if not base_styles[i].style_equal(local_styles[i]):
            # Start of a style-diff range
            range_start = i
            while i < len(base_styles) and not base_styles[i].style_equal(local_styles[i]):
                i += 1
            range_end = i

            # Compute the UTF-16 offset
            start_offset = block_start + _utf16_len(base_text[:range_start])
            end_offset = block_start + _utf16_len(base_text[:range_end])

            # Build style delta
            new_style = local_styles[range_start]
            old_style = base_styles[range_start]
            style_dict, fields_list = _build_style_update(old_style, new_style)

            if style_dict and fields_list:
                mutations.append(Mutation(
                    type="update_text_style",
                    description=f"Update style on '{base_text[range_start:range_end][:20]}': {fields_list}",
                    start_index=start_offset,
                    end_index=end_offset,
                    style=style_dict,
                    fields=fields_list,
                ))
        else:
            i += 1

    return mutations


def _build_style_update(old: TextStyle, new: TextStyle) -> tuple[dict, str]:
    """Build the textStyle dict and fields string for an updateTextStyle request."""
    style_dict: dict = {}
    fields: list[str] = []

    if old.bold != new.bold:
        style_dict["bold"] = new.bold
        fields.append("bold")
    if old.italic != new.italic:
        style_dict["italic"] = new.italic
        fields.append("italic")
    if old.strikethrough != new.strikethrough:
        style_dict["strikethrough"] = new.strikethrough
        fields.append("strikethrough")
    if old.underline != new.underline:
        style_dict["underline"] = new.underline
        fields.append("underline")
    if old.foreground_color != new.foreground_color:
        if new.foreground_color:
            style_dict["foregroundColor"] = _hex_to_color(new.foreground_color)
        else:
            style_dict["foregroundColor"] = {}
        fields.append("foregroundColor")
    if old.background_color != new.background_color:
        if new.background_color:
            style_dict["backgroundColor"] = _hex_to_color(new.background_color)
        else:
            style_dict["backgroundColor"] = {}
        fields.append("backgroundColor")
    if old.font_family != new.font_family:
        if new.font_family:
            style_dict["weightedFontFamily"] = {"fontFamily": new.font_family, "weight": 400}
        else:
            style_dict["weightedFontFamily"] = {}
        fields.append("weightedFontFamily")
    if old.font_size != new.font_size:
        if new.font_size:
            style_dict["fontSize"] = {"magnitude": new.font_size, "unit": "PT"}
        else:
            style_dict["fontSize"] = {}
        fields.append("fontSize")
    if old.url != new.url:
        if new.url:
            style_dict["link"] = {"url": new.url}
        else:
            style_dict["link"] = {}
        fields.append("link")

    return style_dict, ",".join(fields)


def _diff_para_style(
    base_style: ParagraphStyle,
    local_style: ParagraphStyle,
    block_start: int,
    block_end: int,
    tab_id: str,
) -> list[Mutation]:
    """Detect paragraph-style-only changes."""
    if base_style.style_equal(local_style):
        return []

    style_dict: dict = {}
    fields: list[str] = []

    if base_style.alignment != local_style.alignment:
        style_dict["alignment"] = local_style.alignment or "START"
        fields.append("alignment")

    if base_style.line_spacing != local_style.line_spacing:
        if local_style.line_spacing:
            style_dict["lineSpacing"] = local_style.line_spacing
        fields.append("lineSpacing")

    if base_style.space_above != local_style.space_above:
        if local_style.space_above:
            style_dict["spaceAbove"] = {"magnitude": local_style.space_above, "unit": "PT"}
        fields.append("spaceAbove")

    if base_style.space_below != local_style.space_below:
        if local_style.space_below:
            style_dict["spaceBelow"] = {"magnitude": local_style.space_below, "unit": "PT"}
        fields.append("spaceBelow")

    if not style_dict:
        return []

    return [Mutation(
        type="update_paragraph_style",
        description=f"Update paragraph style: {','.join(fields)}",
        start_index=block_start,
        end_index=block_end,
        style=style_dict,
        fields=",".join(fields),
    )]


# =============================================================================
# Plan: diff base vs local → list of mutations
# =============================================================================


def compute_plan(
    base_blocks: list[Block],
    local_blocks: list[Block],
    tab_id: str,
) -> Plan:
    """Compute a minimal edit plan from base IR to local IR.

    Returns a Plan with mutations and human-readable summary.
    """
    mutations: list[Mutation] = []
    summary: list[str] = []

    # Align blocks using SequenceMatcher on normalized text
    base_keys = [_block_key(b) for b in base_blocks]
    local_keys = [_block_key(b) for b in local_blocks]

    sm = difflib.SequenceMatcher(None, base_keys, local_keys)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # Check for within-block changes (style-only, para-style)
            for bi, li in zip(range(i1, i2), range(j1, j2)):
                base_b = base_blocks[bi]
                local_b = local_blocks[li]
                block_muts = _diff_equal_blocks(base_b, local_b, tab_id)
                mutations.extend(block_muts)
                if block_muts:
                    summary.append(f"  restyle block {bi}: '{_get_block_text(base_b)[:40]}'")

        elif tag == "replace":
            # Pair up replacements
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                base_b = base_blocks[i1 + k]
                local_b = local_blocks[j1 + k]
                block_muts = _diff_changed_block(base_b, local_b, tab_id)
                mutations.extend(block_muts)
                summary.append(
                    f"  update block {i1+k}: '{_get_block_text(base_b)[:30]}' → '{_get_block_text(local_b)[:30]}'"
                )
            # Remaining deletes
            for k in range(pairs, i2 - i1):
                base_b = base_blocks[i1 + k]
                block_muts = _delete_block_mutations(base_b, tab_id)
                mutations.extend(block_muts)
                summary.append(f"  delete block {i1+k}: '{_get_block_text(base_b)[:40]}'")
            # Remaining inserts
            for k in range(pairs, j2 - j1):
                local_b = local_blocks[j1 + k]
                # Find insertion point
                if i1 + pairs - 1 >= 0 and i1 + pairs - 1 < len(base_blocks):
                    anchor = base_blocks[i1 + pairs - 1]
                    insert_idx = anchor.doc_range[1] if anchor.doc_range else 1
                elif i1 > 0:
                    anchor = base_blocks[i1 - 1]
                    insert_idx = anchor.doc_range[1] if anchor.doc_range else 1
                else:
                    insert_idx = 1
                block_muts = _insert_block_mutations(local_b, insert_idx, tab_id)
                mutations.extend(block_muts)
                summary.append(f"  insert block: '{_get_block_text(local_b)[:40]}'")

        elif tag == "delete":
            for k in range(i1, i2):
                base_b = base_blocks[k]
                block_muts = _delete_block_mutations(base_b, tab_id)
                mutations.extend(block_muts)
                summary.append(f"  delete block {k}: '{_get_block_text(base_b)[:40]}'")

        elif tag == "insert":
            # Find insertion point from base
            if i1 > 0:
                anchor = base_blocks[i1 - 1]
                insert_idx = anchor.doc_range[1] if anchor.doc_range else 1
            elif base_blocks:
                insert_idx = base_blocks[0].doc_range[0] if base_blocks[0].doc_range else 1
            else:
                insert_idx = 1
            for k in range(j1, j2):
                local_b = local_blocks[k]
                block_muts = _insert_block_mutations(local_b, insert_idx, tab_id)
                mutations.extend(block_muts)
                summary.append(f"  insert block: '{_get_block_text(local_b)[:40]}'")

    return Plan(mutations=mutations, summary=summary)


def _block_key(block: Block) -> str:
    """Produce a key for sequence matching (normalized text)."""
    if isinstance(block, Heading):
        return f"h{block.level}:{_normalize_text(block.spans)}"
    elif isinstance(block, Paragraph):
        return f"p:{_normalize_text(block.spans)}"
    elif isinstance(block, ListItem):
        kind = "ol" if block.ordered else "ul"
        return f"{kind}:{_normalize_text(block.spans)}"
    elif isinstance(block, Table):
        parts = []
        for row in block.rows:
            for cell in row:
                parts.append("".join(s.text for s in cell))
        return f"table:{','.join(parts)}"
    return "unknown"


def _normalize_text(spans: list[Span]) -> str:
    """Get plain text from spans (for block matching)."""
    return "".join(s.text for s in spans)


def _diff_equal_blocks(base: Block, local: Block, tab_id: str) -> list[Mutation]:
    """Diff two blocks matched as 'equal' (same text) for style changes."""
    mutations = []

    if isinstance(base, (Heading, Paragraph, ListItem)) and isinstance(local, (Heading, Paragraph, ListItem)):
        if not base.doc_range:
            return []
        block_start = base.doc_range[0]
        block_end = base.doc_range[1] - 1  # exclude trailing newline

        # Text style changes
        style_muts = _diff_text_styles(
            base.spans, local.spans, block_start, tab_id
        )
        mutations.extend(style_muts)

        # Paragraph style changes
        para_muts = _diff_para_style(
            base.para_style, local.para_style, block_start, block_end, tab_id
        )
        mutations.extend(para_muts)

        # Heading level change
        if isinstance(base, Heading) and isinstance(local, Heading):
            if base.level != local.level:
                mutations.append(Mutation(
                    type="update_paragraph_style",
                    description=f"Change heading level {base.level} → {local.level}",
                    start_index=block_start,
                    end_index=block_end,
                    style={"namedStyleType": HEADING_STYLE_MAP.get(local.level, "HEADING_1")},
                    fields="namedStyleType",
                ))

    elif isinstance(base, Table) and isinstance(local, Table):
        # Check individual cell text/style changes
        mutations.extend(_diff_table_cells(base, local, tab_id))

    return mutations


def _diff_changed_block(base: Block, local: Block, tab_id: str) -> list[Mutation]:
    """Diff a block that was matched as changed (text differs)."""
    if not base.doc_range:
        return []

    mutations = []
    block_start = base.doc_range[0]
    block_end = base.doc_range[1] - 1

    if isinstance(base, (Heading, Paragraph, ListItem)) and isinstance(local, (Heading, Paragraph, ListItem)):
        # Run-level text splicing
        text_muts = _diff_spans_for_text_edit(
            base.spans, local.spans, block_start, tab_id
        )
        mutations.extend(text_muts)

        # After text change, also check for style updates on the new text
        # (this will be applied after text mutations resolve)
        # For now, apply styles on newly inserted runs via a second pass
        # The Docs API applies insertions with the style at the insertion point

        # Paragraph style
        para_muts = _diff_para_style(
            _get_para_style(base), _get_para_style(local),
            block_start, block_end, tab_id
        )
        mutations.extend(para_muts)

        # Heading level change
        if isinstance(base, Heading) and isinstance(local, Heading):
            if base.level != local.level:
                mutations.append(Mutation(
                    type="update_paragraph_style",
                    description=f"Change heading level {base.level} → {local.level}",
                    start_index=block_start,
                    end_index=block_end,
                    style={"namedStyleType": HEADING_STYLE_MAP.get(local.level, "HEADING_1")},
                    fields="namedStyleType",
                ))
        elif isinstance(base, Heading) and not isinstance(local, Heading):
            mutations.append(Mutation(
                type="update_paragraph_style",
                description="Reset heading to normal",
                start_index=block_start,
                end_index=block_end,
                style={"namedStyleType": "NORMAL_TEXT"},
                fields="namedStyleType",
            ))
        elif not isinstance(base, Heading) and isinstance(local, Heading):
            mutations.append(Mutation(
                type="update_paragraph_style",
                description=f"Set heading level {local.level}",
                start_index=block_start,
                end_index=block_end,
                style={"namedStyleType": HEADING_STYLE_MAP.get(local.level, "HEADING_1")},
                fields="namedStyleType",
            ))

    elif isinstance(base, Table) and isinstance(local, Table):
        mutations.extend(_diff_table_cells(base, local, tab_id))
    else:
        # Type changed — delete + insert
        mutations.extend(_delete_block_mutations(base, tab_id))
        insert_idx = base.doc_range[0] if base.doc_range else 1
        mutations.extend(_insert_block_mutations(local, insert_idx, tab_id))

    return mutations


def _diff_table_cells(base: Table, local: Table, tab_id: str) -> list[Mutation]:
    """Diff table cells for text and style changes."""
    if not base._raw_table or "table" not in base._raw_table:
        return []

    mutations = []
    doc_table = base._raw_table["table"]
    doc_rows = doc_table.get("tableRows", [])

    for ri, (base_row, local_row) in enumerate(zip(base.rows, local.rows)):
        if ri >= len(doc_rows):
            break
        doc_row = doc_rows[ri]
        doc_cells = doc_row.get("tableCells", [])

        for ci, (base_spans, local_spans) in enumerate(zip(base_row, local_row)):
            if ci >= len(doc_cells):
                break

            cell_content = doc_cells[ci].get("content", [])
            if not cell_content:
                continue
            para_wrapper = cell_content[0]
            if "paragraph" not in para_wrapper:
                continue

            # startIndex/endIndex live on the structural element wrapper,
            # not inside the "paragraph" dict. Fall back to element indices.
            cell_start = para_wrapper.get("startIndex")
            cell_end = para_wrapper.get("endIndex")
            if cell_start is None or cell_end is None:
                # Try getting from elements
                para = para_wrapper["paragraph"]
                elements = para.get("elements", [])
                if elements:
                    cell_start = elements[0].get("startIndex")
                    cell_end = elements[-1].get("endIndex")
            if cell_start is None or cell_end is None:
                continue
            base_text = "".join(s.text for s in base_spans)
            local_text = "".join(s.text for s in local_spans)

            if base_text != local_text:
                # Text changed in this cell
                text_muts = _diff_spans_for_text_edit(
                    base_spans, local_spans, cell_start, tab_id
                )
                mutations.extend(text_muts)
            else:
                # Check style changes
                style_muts = _diff_text_styles(
                    base_spans, local_spans, cell_start, tab_id
                )
                mutations.extend(style_muts)

    return mutations


def _delete_block_mutations(block: Block, tab_id: str) -> list[Mutation]:
    """Generate mutations to delete a block."""
    if not block.doc_range:
        return []
    return [Mutation(
        type="delete_block",
        description=f"Delete block: '{_get_block_text(block)[:40]}'",
        start_index=block.doc_range[0],
        end_index=block.doc_range[1],
    )]


def _insert_block_mutations(block: Block, insert_idx: int, tab_id: str) -> list[Mutation]:
    """Generate mutations to insert a new block."""
    if isinstance(block, (Heading, Paragraph, ListItem)):
        text = block.text + "\n"
        mutations = [Mutation(
            type="insert_block",
            description=f"Insert block: '{block.text[:40]}'",
            start_index=insert_idx,
            text=text,
            block=block,
        )]
        return mutations
    return []


# =============================================================================
# Plan → Docs API requests
# =============================================================================


def plan_to_requests(plan: Plan, tab_id: str) -> list[dict]:
    """Convert a Plan to Docs API batchUpdate requests.

    Requests are returned in reverse index order (as required by the API).
    """
    requests: list[dict] = []

    for mut in plan.mutations:
        if mut.type == "delete_text":
            requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": mut.start_index,
                        "endIndex": mut.end_index,
                        "tabId": tab_id,
                    }
                }
            })
        elif mut.type == "insert_text":
            requests.append({
                "insertText": {
                    "text": mut.text,
                    "location": {
                        "index": mut.start_index,
                        "tabId": tab_id,
                    }
                }
            })
        elif mut.type == "update_text_style":
            requests.append({
                "updateTextStyle": {
                    "range": {
                        "startIndex": mut.start_index,
                        "endIndex": mut.end_index,
                        "tabId": tab_id,
                    },
                    "textStyle": mut.style,
                    "fields": mut.fields,
                }
            })
        elif mut.type == "update_paragraph_style":
            requests.append({
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": mut.start_index,
                        "endIndex": mut.end_index,
                        "tabId": tab_id,
                    },
                    "paragraphStyle": mut.style,
                    "fields": mut.fields,
                }
            })
        elif mut.type == "delete_block":
            requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": mut.start_index,
                        "endIndex": mut.end_index,
                        "tabId": tab_id,
                    }
                }
            })
        elif mut.type == "insert_block":
            requests.append({
                "insertText": {
                    "text": mut.text,
                    "location": {
                        "index": mut.start_index,
                        "tabId": tab_id,
                    }
                }
            })
            # Apply heading style if needed
            if mut.block and isinstance(mut.block, Heading):
                named_style = HEADING_STYLE_MAP.get(mut.block.level, "HEADING_1")
                text_len = _utf16_len(mut.block.text)
                requests.append({
                    "updateParagraphStyle": {
                        "range": {
                            "startIndex": mut.start_index,
                            "endIndex": mut.start_index + text_len,
                            "tabId": tab_id,
                        },
                        "paragraphStyle": {"namedStyleType": named_style},
                        "fields": "namedStyleType",
                    }
                })

    # Sort by descending start index for safe application
    def _sort_key(req):
        for val in req.values():
            if isinstance(val, dict):
                r = val.get("range") or val.get("location")
                if r:
                    idx = r.get("startIndex") or r.get("index", 0)
                    return -idx
        return 0

    requests.sort(key=_sort_key)
    return requests
