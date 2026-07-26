"""Diff-based push for Google Docs tabs.

See ADR 034 (run-level splicing) and ADR 037 (single-editor sync).

Pipeline (ADR 037)
==================

    1. Fetch remote — ``ir.from_doc_json(tab_body)`` produces a Block
                      list where every block carries ``doc_range``
                      (Google Docs ``startIndex``/``endIndex``).

    2. Parse local  — ``ir.from_markdown(edited_md)`` produces a Block
                      list without ``doc_range``.

    3. Diff         — ``difflib.SequenceMatcher`` over block keys,
                      emitting ``EditOp`` (update / insert / delete).

    4. Mutations    — translate each ``EditOp`` into Docs API
                      ``batchUpdate`` requests, using ``doc_range``
                      from the remote blocks to resolve positions.

    5. Apply        — ``batchUpdate`` call.

Under the single-editor model (ADR 037) a revision guard ensures the
remote has not moved since the last pull, so the remote blocks carry
the correct indices for mutation — no baseline load, no alignment, no
drift detection.

Key invariants
==============

* **UTF-16 indices.** Google Docs addresses content in UTF-16 code
  units, not Python characters. All index math uses ``_utf16_len``.

* **Paragraph ranges include the trailing newline.** Deletions stop
  at ``endIndex - 1`` to preserve paragraph structure.

* **Mutations applied in reverse index order.** Each request only
  shifts indices below the ones already processed.
"""

import difflib
import logging
from dataclasses import dataclass, field

from .ir import (
    _utf16_len,
    Block,
    CodeBlock,
    Heading,
    HEADING_STYLE_MAP,
    ListItem,
    Paragraph,
    Span,
    Table,
    from_doc_json,
    from_markdown,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Block helpers
# =============================================================================


def _block_type(block: Block) -> str:
    """Map block to comparable type string."""
    if isinstance(block, Heading):
        return "heading"
    elif isinstance(block, Paragraph):
        return "paragraph"
    elif isinstance(block, ListItem):
        return "list_item"
    elif isinstance(block, Table):
        return "table"
    elif isinstance(block, CodeBlock):
        return "code_block"
    return "unknown"


def _block_text(block: Block) -> str:
    """Extract plain text from a block."""
    if isinstance(block, (Heading, Paragraph, ListItem, CodeBlock)):
        return block.text
    elif isinstance(block, Table):
        num_rows = len(block.rows)
        num_cols = max(len(r) for r in block.rows) if block.rows else 0
        return f"[table {num_rows}x{num_cols}]"
    return ""


# =============================================================================
# AST Diff
# =============================================================================


@dataclass
class EditOp:
    """A single edit operation between base and edited block lists."""

    type: str  # 'update', 'insert', 'delete'
    base_idx: int | None  # index in base blocks (None for inserts)
    edit_idx: int | None  # index in edited blocks (None for deletes)
    base_block: Block | None
    edit_block: Block | None
    insert_after: int | None = None


def _block_key(block: Block) -> str:
    """Produce a hashable key for sequence matching."""
    if isinstance(block, Table):
        cell_texts = []
        for row in block.rows:
            for cell in row:
                cell_texts.append("".join(s.text for s in cell))
        return f"table:{','.join(cell_texts)}"
    return f"{_block_type(block)}:{_block_text(block)}"


def _spans_differ(a: list[Span], b: list[Span]) -> bool:
    """Check if two span lists have different formatting."""
    if len(a) != len(b):
        return True
    for sa, sb in zip(a, b):
        if (
            sa.text != sb.text
            or sa.bold != sb.bold
            or sa.italic != sb.italic
            or sa.strikethrough != sb.strikethrough
            or sa.url != sb.url
        ):
            return True
    return False


def _formatting_differs(a: Block, b: Block) -> bool:
    """Check if two same-text blocks have different inline formatting."""
    if not isinstance(a, type(b)):
        return True
    if isinstance(a, Heading) and isinstance(b, Heading):
        if a.level != b.level:
            return True
        return _spans_differ(a.spans, b.spans)
    if isinstance(a, (Paragraph, ListItem)) and isinstance(b, (Paragraph, ListItem)):
        return _spans_differ(a.spans, b.spans)
    if isinstance(a, Table) and isinstance(b, Table):
        if len(a.rows) != len(b.rows):
            return True
        for ar, br in zip(a.rows, b.rows):
            if len(ar) != len(br):
                return True
            for ac, bc in zip(ar, br):
                if _spans_differ(ac, bc):
                    return True
        return False
    return False


def ast_diff(base_blocks: list[Block], edited_blocks: list[Block]) -> list[EditOp]:
    """Diff two block lists, producing edit operations."""
    base_keys = [_block_key(b) for b in base_blocks]
    edit_keys = [_block_key(b) for b in edited_blocks]

    sm = difflib.SequenceMatcher(None, base_keys, edit_keys)
    ops: list[EditOp] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for bi, ei in zip(range(i1, i2), range(j1, j2)):
                if _formatting_differs(base_blocks[bi], edited_blocks[ei]):
                    ops.append(
                        EditOp("update", bi, ei, base_blocks[bi], edited_blocks[ei])
                    )
        elif tag == "replace":
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                ops.append(
                    EditOp(
                        "update",
                        i1 + k,
                        j1 + k,
                        base_blocks[i1 + k],
                        edited_blocks[j1 + k],
                    )
                )
            for k in range(pairs, i2 - i1):
                ops.append(EditOp("delete", i1 + k, None, base_blocks[i1 + k], None))
            insert_after = i1 + pairs - 1 if pairs > 0 else i1 - 1
            for k in range(pairs, j2 - j1):
                ops.append(
                    EditOp(
                        "insert",
                        None,
                        j1 + k,
                        None,
                        edited_blocks[j1 + k],
                        insert_after=insert_after if insert_after >= 0 else None,
                    )
                )
        elif tag == "delete":
            for k in range(i1, i2):
                ops.append(EditOp("delete", k, None, base_blocks[k], None))
        elif tag == "insert":
            insert_after = i1 - 1 if i1 > 0 else None
            for k in range(j1, j2):
                ops.append(
                    EditOp(
                        "insert",
                        None,
                        k,
                        None,
                        edited_blocks[k],
                        insert_after=insert_after,
                    )
                )

    return ops


# =============================================================================
# Mutation translator
# =============================================================================


def diff_to_mutations(
    ops: list[EditOp],
    base_blocks: list[Block],
    tab_id: str,
) -> list[dict]:
    """Translate edit operations into Docs API batchUpdate requests.

    Uses ``doc_range`` from base_blocks for index resolution.
    No alignment parameter needed — indices come from the blocks.
    """
    requests: list[dict] = []

    for op in ops:
        if op.type == "update":
            if op.base_idx is None or op.base_idx >= len(base_blocks):
                raise ValueError(
                    f"Update op references invalid base block {op.base_idx}"
                )

            base = op.base_block
            edit = op.edit_block

            if isinstance(base, (Paragraph, Heading)) and isinstance(
                edit, (Paragraph, Heading)
            ):
                requests.extend(_update_paragraph_requests(base, edit, tab_id))
            elif isinstance(base, ListItem) and isinstance(edit, ListItem):
                requests.extend(_update_paragraph_requests(base, edit, tab_id))
            elif isinstance(base, Table) and isinstance(edit, Table):
                requests.extend(_update_table_requests(base, edit, tab_id))
            else:
                raise ValueError(
                    f"Cannot translate update for {_block_type(base)} → {_block_type(edit)}"
                )

        elif op.type == "insert":
            if op.edit_block is None:
                continue
            if op.insert_after is not None and op.insert_after < len(base_blocks):
                anchor = base_blocks[op.insert_after]
                insert_idx = anchor.doc_range[1] if anchor.doc_range else 1
            elif base_blocks:
                first = base_blocks[0]
                insert_idx = first.doc_range[0] if first.doc_range else 1
            else:
                insert_idx = 1
            requests.extend(_insert_block_requests(op.edit_block, insert_idx, tab_id))

        elif op.type == "delete":
            if op.base_idx is None or op.base_idx >= len(base_blocks):
                continue
            base = base_blocks[op.base_idx]
            if base.doc_range:
                requests.append(
                    {
                        "deleteContentRange": {
                            "range": {
                                "startIndex": base.doc_range[0],
                                "endIndex": base.doc_range[1],
                                "tabId": tab_id,
                            }
                        }
                    }
                )

    # Sort by descending start index (stable sort preserves emit order
    # for requests at the same index)
    def _sort_key(req):
        for val in req.values():
            if isinstance(val, dict):
                r = val.get("range") or val.get("location")
                if r and "startIndex" in r:
                    return -r["startIndex"]
                if r and "index" in r:
                    return -r["index"]
        return 0

    requests.sort(key=_sort_key)
    return requests


def _normalize_spans(spans: list[Span]) -> list[Span]:
    """Merge adjacent spans with identical formatting into one.

    Makes run boundaries non-semantic: different splits of same text+style
    produce identical normalized form, avoiding spurious diffs.
    """
    if not spans:
        return []
    result: list[Span] = []
    cur = spans[0]
    for s in spans[1:]:
        if (
            s.bold == cur.bold
            and s.italic == cur.italic
            and s.strikethrough == cur.strikethrough
            and s.url == cur.url
        ):
            cur = Span(
                text=cur.text + s.text,
                bold=cur.bold,
                italic=cur.italic,
                strikethrough=cur.strikethrough,
                url=cur.url,
            )
        else:
            result.append(cur)
            cur = s
    result.append(cur)
    return result


def _update_paragraph_requests(
    base: Block,
    new_block: Block,
    tab_id: str,
) -> list[dict]:
    """Generate requests to update a paragraph/heading/list_item in place.

    Uses run-level splicing: character-level diff within the paragraph
    so that sibling runs (bold, link, color) survive word edits.
    Two-pass style application: text mutations first, then style updates
    on newly inserted text whose intended style differs from context.
    """
    requests: list[dict] = []

    if not base.doc_range:
        return requests

    start = base.doc_range[0]
    end = base.doc_range[1] - 1  # preserve trailing newline

    if end <= start:
        return requests

    if not isinstance(new_block, (Heading, Paragraph, ListItem)):
        return requests
    if not isinstance(base, (Heading, Paragraph, ListItem)):
        return requests

    base_text = base.text
    new_text = new_block.text
    new_spans = new_block.spans

    # --- Pass 1: Text splicing (character-level diff) ---
    if base_text != new_text:
        requests.extend(
            _splice_text_requests(base_text, new_text, start, tab_id)
        )
    # After text splice, the paragraph text is new_text starting at `start`.

    # --- Pass 2: Style application on the resulting text ---
    # Apply formatting for all spans in the new block.
    # When text changed, inserted characters inherit the style at the
    # insertion point; we must explicitly set any differing styles.
    requests.extend(_span_style_requests(new_spans, start, tab_id))

    # --- Paragraph style (heading level changes) ---
    if isinstance(new_block, Heading):
        named_style = HEADING_STYLE_MAP.get(new_block.level, "HEADING_1")
    elif isinstance(base, Heading):
        # Heading → Paragraph/ListItem: reset to NORMAL_TEXT
        named_style = "NORMAL_TEXT"
    else:
        named_style = None

    if named_style:
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": start,
                        "endIndex": start + _utf16_len(new_text),
                        "tabId": tab_id,
                    },
                    "paragraphStyle": {"namedStyleType": named_style},
                    "fields": "namedStyleType",
                }
            }
        )

    return requests


def _splice_text_requests(
    base_text: str,
    new_text: str,
    block_start: int,
    tab_id: str,
) -> list[dict]:
    """Character-level diff producing minimal deleteContentRange/insertText.

    Uses SequenceMatcher to find the minimal set of text edits.
    All index math uses UTF-16 offsets (Google Docs API convention).

    Opcodes are iterated in REVERSE order (descending base index) so that
    each mutation only shifts content below the regions already processed.
    This matches ADR 034's requirement that mutations be applied in reverse
    index order, and makes the function self-contained (no external sort
    needed for correctness within a single paragraph's splice requests).
    """
    sm = difflib.SequenceMatcher(None, base_text, new_text)
    opcodes = [oc for oc in sm.get_opcodes() if oc[0] != "equal"]
    requests: list[dict] = []

    for tag, i1, i2, j1, j2 in reversed(opcodes):
        if tag == "replace":
            del_start = block_start + _utf16_len(base_text[:i1])
            del_end = block_start + _utf16_len(base_text[:i2])
            ins_text = new_text[j1:j2]
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": del_start,
                            "endIndex": del_end,
                            "tabId": tab_id,
                        }
                    }
                }
            )
            requests.append(
                {
                    "insertText": {
                        "text": ins_text,
                        "location": {"index": del_start, "tabId": tab_id},
                    }
                }
            )
        elif tag == "delete":
            del_start = block_start + _utf16_len(base_text[:i1])
            del_end = block_start + _utf16_len(base_text[:i2])
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": del_start,
                            "endIndex": del_end,
                            "tabId": tab_id,
                        }
                    }
                }
            )
        elif tag == "insert":
            ins_point = block_start + _utf16_len(base_text[:i1])
            ins_text = new_text[j1:j2]
            requests.append(
                {
                    "insertText": {
                        "text": ins_text,
                        "location": {"index": ins_point, "tabId": tab_id},
                    }
                }
            )

    return requests


def _span_style_requests(
    spans: list[Span], start_offset: int, tab_id: str
) -> list[dict]:
    """Generate updateTextStyle requests for inline formatting on spans."""
    requests: list[dict] = []
    offset = start_offset
    for span in spans:
        span_end = offset + _utf16_len(span.text)
        if span.bold:
            requests.append(
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
            requests.append(
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
            requests.append(
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
            requests.append(
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
    return requests


def _cell_plain(spans: list[Span]) -> str:
    return "".join(s.text for s in spans)


def _update_table_requests(
    base_table: Table,
    edit_table: Table,
    tab_id: str,
) -> list[dict]:
    """Generate requests to patch changed cells in a same-shape table."""
    if len(base_table.rows) != len(edit_table.rows):
        raise ValueError(
            f"Table row count changed ({len(base_table.rows)} → {len(edit_table.rows)}). Patch cannot add/remove rows."
        )

    for ri, (br, er) in enumerate(zip(base_table.rows, edit_table.rows)):
        if len(br) != len(er):
            raise ValueError(
                f"Table column count changed in row {ri} ({len(br)} → {len(er)}). Patch cannot add/remove columns."
            )

    # Need raw table JSON for cell indices
    if not base_table._raw_table or "table" not in base_table._raw_table:
        raise ValueError("Base table has no raw JSON for cell index resolution")

    doc_table = base_table._raw_table["table"]
    doc_rows = doc_table.get("tableRows", [])
    requests: list[dict] = []

    for ri, (base_row, edit_row) in enumerate(zip(base_table.rows, edit_table.rows)):
        if ri >= len(doc_rows):
            break
        doc_row = doc_rows[ri]
        doc_cells = doc_row.get("tableCells", [])

        for ci, (base_spans, edit_spans) in enumerate(zip(base_row, edit_row)):
            if ci >= len(doc_cells):
                break

            base_text = _cell_plain(base_spans)
            edit_text = _cell_plain(edit_spans)

            if base_text == edit_text and not _spans_differ(base_spans, edit_spans):
                continue

            cell_content = doc_cells[ci].get("content", [])
            if len(cell_content) > 1:
                raise ValueError(
                    f"Cell [{ri},{ci}] has {len(cell_content)} paragraphs. Patch cannot edit multi-paragraph cells."
                )
            if not cell_content:
                continue

            para_wrapper = cell_content[0]
            if "paragraph" not in para_wrapper:
                continue

            # startIndex/endIndex live on the structural element wrapper,
            # not inside the "paragraph" dict (same as from_doc_json reads).
            cell_start = para_wrapper.get("startIndex")
            cell_end = para_wrapper.get("endIndex")
            if cell_start is None or cell_end is None:
                # Fallback: try elements inside the paragraph
                para = para_wrapper["paragraph"]
                elements = para.get("elements", [])
                if elements:
                    cell_start = elements[0].get("startIndex")
                    cell_end = elements[-1].get("endIndex")
            if cell_start is None or cell_end is None:
                continue

            content_end = cell_end - 1
            if content_end > cell_start:
                requests.append(
                    {
                        "deleteContentRange": {
                            "range": {
                                "startIndex": cell_start,
                                "endIndex": content_end,
                                "tabId": tab_id,
                            }
                        }
                    }
                )

            if edit_text:
                requests.append(
                    {
                        "insertText": {
                            "text": edit_text,
                            "location": {"index": cell_start, "tabId": tab_id},
                        }
                    }
                )

            requests.extend(_span_style_requests(edit_spans, cell_start, tab_id))

    return requests


def _insert_block_requests(
    block: Block,
    insert_idx: int,
    tab_id: str,
) -> list[dict]:
    """Generate requests to insert a new block at a given index."""
    requests: list[dict] = []

    if isinstance(block, (Heading, Paragraph, ListItem)):
        text = block.text + "\n"
        spans = block.spans
    elif isinstance(block, CodeBlock):
        prefixed = "\n".join(f"> {line}" for line in block.code.split("\n"))
        text = prefixed + "\n"
        spans = []
    else:
        return requests

    requests.append(
        {
            "insertText": {
                "text": text,
                "location": {"index": insert_idx, "tabId": tab_id},
            }
        }
    )

    if isinstance(block, Heading):
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": insert_idx,
                        "endIndex": insert_idx + _utf16_len(block.text),
                        "tabId": tab_id,
                    },
                    "paragraphStyle": {
                        "namedStyleType": HEADING_STYLE_MAP.get(
                            block.level, "HEADING_1"
                        )
                    },
                    "fields": "namedStyleType",
                }
            }
        )

    if isinstance(block, ListItem):
        text_len = _utf16_len(text)
        preset = (
            "NUMBERED_DECIMAL_NESTED" if block.ordered else "BULLET_DISC_CIRCLE_SQUARE"
        )
        requests.append(
            {
                "createParagraphBullets": {
                    "range": {
                        "startIndex": insert_idx,
                        "endIndex": insert_idx + text_len - 1,
                        "tabId": tab_id,
                    },
                    "bulletPreset": preset,
                }
            }
        )

    requests.extend(_span_style_requests(spans, insert_idx, tab_id))

    return requests


# =============================================================================
# Helpers: doc fetch + tab lookup
# =============================================================================


def _fetch_tab(document_id: str, tab_name: str, *, docs_service=None):
    """Fetch doc JSON and locate a tab by name.

    Returns (doc, tab_id, tab_body). Raises ValueError if tab not found.
    """
    from googleapiclient.discovery import build
    from ..auth import get_authenticated_credentials

    if docs_service is None:
        creds = get_authenticated_credentials()
        docs_service = build("docs", "v1", credentials=creds)

    doc = (
        docs_service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )

    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("title") == tab_name:
            tab_id = props.get("tabId")
            tab_body = tab.get("documentTab", {}).get("body", {}).get("content", [])
            return doc, tab_id, tab_body, docs_service

    raise ValueError(f"Tab not found: {tab_name}")


# =============================================================================
# Preview
# =============================================================================


@dataclass
class DiffPreview:
    """Result of a dry-run diff computation for user preview."""

    ops: list[EditOp]
    summary_lines: list[str]
    error: str | None = None      # None = patch applicable; str = reason it is not
    fatal: bool = False           # True = tab not found; don't offer bulk fallback
    docs_service: object = field(default=None, repr=False)


def _op_summary(op: EditOp) -> str:
    """Human-readable one-line summary of an edit operation."""
    if op.type == "update":
        btype = _block_type(op.base_block) if op.base_block else "?"
        old_text = (_block_text(op.base_block) if op.base_block else "")[:50]
        new_text = (_block_text(op.edit_block) if op.edit_block else "")[:50]
        if old_text == new_text:
            return f"  restyle {btype}: {old_text!r}"
        return f"  update {btype}: {old_text!r} → {new_text!r}"
    elif op.type == "insert":
        btype = _block_type(op.edit_block) if op.edit_block else "?"
        text = (_block_text(op.edit_block) if op.edit_block else "")[:50]
        return f"  insert {btype}: {text!r}"
    elif op.type == "delete":
        btype = _block_type(op.base_block) if op.base_block else "?"
        text = (_block_text(op.base_block) if op.base_block else "")[:50]
        return f"  delete {btype}: {text!r}"
    return f"  {op.type}: ?"


def preview_diff(
    document_id: str,
    tab_name: str,
    edited_markdown: str,
    *,
    docs_service=None,
) -> DiffPreview:
    """Compute the diff without applying it. Returns a preview for the user."""
    try:
        doc, tab_id, tab_body, docs_service = _fetch_tab(
            document_id, tab_name, docs_service=docs_service
        )
    except ValueError as e:
        return DiffPreview(
            ops=[],
            summary_lines=[],
            error=str(e),
            fatal=True,
            docs_service=docs_service,
        )

    remote_blocks = from_doc_json(tab_body, lists=doc.get("lists"))
    local_blocks = from_markdown(edited_markdown)

    ops = ast_diff(remote_blocks, local_blocks)

    if not ops:
        return DiffPreview(
            ops=[],
            summary_lines=[],
            docs_service=docs_service,
        )

    # Dry-run mutation translator
    error: str | None = None
    try:
        diff_to_mutations(ops, remote_blocks, tab_id)
    except ValueError as e:
        error = str(e)

    # Build summary
    updates = [op for op in ops if op.type == "update"]
    inserts = [op for op in ops if op.type == "insert"]
    deletes = [op for op in ops if op.type == "delete"]

    summary: list[str] = []
    if updates:
        summary.append(f"{len(updates)} update(s):")
        for op in updates:
            summary.append(_op_summary(op))
    if inserts:
        summary.append(f"{len(inserts)} insert(s):")
        for op in inserts:
            summary.append(_op_summary(op))
    if deletes:
        summary.append(f"{len(deletes)} delete(s):")
        for op in deletes:
            summary.append(_op_summary(op))

    return DiffPreview(
        ops=ops, summary_lines=summary, error=error, docs_service=docs_service
    )


# =============================================================================
# Top-level orchestrator
# =============================================================================


def diff_push(
    document_id: str,
    tab_name: str,
    edited_markdown: str,
    *,
    docs_service=None,
) -> list[str]:
    """Push local markdown changes using diff-based mutations.

    Single API call to read remote state, diff against local, apply mutations.
    No Drive API export or fuzzy alignment needed.
    """
    warnings: list[str] = []

    doc, tab_id, tab_body, docs_service = _fetch_tab(
        document_id, tab_name, docs_service=docs_service
    )

    remote_blocks = from_doc_json(tab_body, lists=doc.get("lists"))
    local_blocks = from_markdown(edited_markdown)

    # Step 3: Diff
    ops = ast_diff(remote_blocks, local_blocks)

    if not ops:
        warnings.append("No differences found between base and edited markdown.")
        return warnings

    updates = [op for op in ops if op.type == "update"]
    inserts = [op for op in ops if op.type == "insert"]
    deletes = [op for op in ops if op.type == "delete"]
    logger.info(
        f"Diff: {len(updates)} updates, {len(inserts)} inserts, {len(deletes)} deletes"
    )

    # Step 4: Translate to mutations
    mutations = diff_to_mutations(ops, remote_blocks, tab_id)

    if not mutations:
        warnings.append("Diff produced no API mutations.")
        return warnings

    # Step 5: Apply
    logger.info(f"Applying {len(mutations)} API requests")
    docs_service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": mutations},
    ).execute()

    return warnings


# =============================================================================
# Push plan (ADR 037 — single-editor sync)
# =============================================================================


@dataclass
class ThreeWayPlan:
    """Result of a push-plan computation.

    Under ADR 037 (single-editor sync) the push path diffs remote vs
    local directly — the remote IS the state you pulled when the
    revision guard passes.  No baseline load, no drift detection, no
    alignment.  The name ``ThreeWayPlan`` is retained for API
    compatibility with the tree-plan front-end.
    """

    ops: list[EditOp]
    mutations: list[dict]
    summary_lines: list[str]
    error: str | None = None
    revision_changed: bool = False  # True if remote revisionId differs from stored

    @property
    def is_empty(self) -> bool:
        return not self.ops and not self.mutations


def compute_three_way_plan(
    local_markdown: str,
    remote_body: list[dict],
    remote_revision: str,
    stored_revision: str,
    tab_id: str,
    *,
    lists: dict | None = None,
) -> ThreeWayPlan:
    """Compute a push plan: diff remote vs local, produce mutations.

    ADR 037 (single-editor sync): under the revision guard the remote
    IS the state you pulled, so diffing against it gives exactly your
    edits with correct indices.  One code path — no baseline, no drift
    detection, no alignment.

    Args:
        local_markdown: The user's edited markdown.
        remote_body: Current remote tab body content (from documents().get()).
        remote_revision: Current remote revisionId.
        stored_revision: revisionId stored at pull time.
        tab_id: Google Docs tab ID for mutation targeting.
        lists: Lists metadata from the document.

    Returns:
        ThreeWayPlan with ops and mutations.
    """
    # --- Revision guard (ADR 037: mismatch = refuse) ---
    revision_changed = bool(
        stored_revision and remote_revision and stored_revision != remote_revision
    )
    if revision_changed:
        return ThreeWayPlan(
            ops=[],
            mutations=[],
            summary_lines=[],
            error=(
                f"Remote changed since pull "
                f"(rev {stored_revision} -> {remote_revision}). "
                f"Pull first."
            ),
            revision_changed=True,
        )

    # --- Diff remote vs local ---
    remote_blocks = from_doc_json(remote_body, lists=lists)
    local_blocks = from_markdown(local_markdown)
    ops = ast_diff(remote_blocks, local_blocks)

    if not ops:
        return ThreeWayPlan(
            ops=[],
            mutations=[],
            summary_lines=[],
        )

    error = None
    try:
        mutations = diff_to_mutations(ops, remote_blocks, tab_id)
    except ValueError as e:
        mutations = []
        error = str(e)

    summary = _build_summary(ops)
    return ThreeWayPlan(
        ops=ops,
        mutations=mutations,
        summary_lines=summary,
        error=error,
    )


def _build_summary(ops: list[EditOp]) -> list[str]:
    """Build human-readable summary lines from edit ops."""
    updates = [op for op in ops if op.type == "update"]
    inserts = [op for op in ops if op.type == "insert"]
    deletes = [op for op in ops if op.type == "delete"]

    summary: list[str] = []
    if updates:
        summary.append(f"{len(updates)} update(s):")
        for op in updates:
            summary.append(_op_summary(op))
    if inserts:
        summary.append(f"{len(inserts)} insert(s):")
        for op in inserts:
            summary.append(_op_summary(op))
    if deletes:
        summary.append(f"{len(deletes)} delete(s):")
        for op in deletes:
            summary.append(_op_summary(op))
    return summary


# =============================================================================
# Tree plan front-end (ADR 035 / gax-cvi.7)
# =============================================================================
#
# The tree plan front-end takes compressed tree IR bodies (from tree.py)
# instead of markdown and produces ThreeWayPlan results using the shared
# mutation machinery.
#
# Key differences from the markdown front-end:
# - Style-only edits emit updateTextStyle (not delete+insert)
# - Run boundaries are non-semantic (normalized before diff)
# - Appendix/ref/raw modifications rejected pre-plan
# - Full style vocabulary: color, font, size, bg, baseline, etc.

_TREE_HEADING_KEYS = frozenset(f"h{i}" for i in range(1, 7))
_TREE_STYLE_KEYS = frozenset({
    "b", "i", "s", "u", "color", "bg", "font", "size", "url", "baseline",
})


def _tree_block_type(block: dict) -> str:
    """Detect tree block type key (h1-h6, p, li, table, toc, etc.)."""
    for key in ("h1", "h2", "h3", "h4", "h5", "h6"):
        if key in block:
            return key
    for key in ("p", "li", "table", "toc", "_verbatim", "ul", "ol"):
        if key in block:
            return key
    return "unknown"


def _tree_block_runs(block: dict) -> list:
    """Extract runs from a paragraph-like tree block."""
    btype = _tree_block_type(block)
    if btype not in _TREE_HEADING_KEYS and btype not in ("p", "li"):
        return []
    val = block[btype]
    if isinstance(val, str):
        return [val]
    if isinstance(val, dict):
        if "runs" in val:
            return val["runs"]
        if "t" in val:
            return [val]  # Single styled run — return the whole dict
    return []


def _tree_run_text(run: object) -> str:
    """Extract text from a tree run."""
    if isinstance(run, str):
        return run
    if isinstance(run, dict):
        return run.get("t", "")
    return ""


def _tree_run_style(run: object) -> dict:
    """Extract style dict from a tree run (string -> empty style)."""
    if isinstance(run, str):
        return {}
    if not isinstance(run, dict):
        return {}
    return {k: v for k, v in run.items() if k in _TREE_STYLE_KEYS}


def _tree_block_text(block: dict) -> str:
    """Get plain text from a tree block (joins all run texts)."""
    btype = _tree_block_type(block)
    if btype == "table":
        table_data = block.get("table", {})
        cell_texts = []
        for row in table_data.get("rows", []):
            if isinstance(row, list):
                for cell in row:
                    cell_texts.append(_tree_cell_text(cell))
        return ",".join(cell_texts)
    runs = _tree_block_runs(block)
    return "".join(_tree_run_text(r) for r in runs)


def _tree_cell_text(cell: object) -> str:
    """Extract text from a tree table cell."""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, dict):
        if "runs" in cell:
            return "".join(_tree_run_text(r) for r in cell["runs"])
        return cell.get("t", "")
    if isinstance(cell, list):
        return "".join(_tree_run_text(r) for r in cell)
    return ""


def _tree_block_para_style(block: dict) -> dict:
    """Extract editable paragraph style (no _ prefixed keys)."""
    btype = _tree_block_type(block)
    if btype in _TREE_HEADING_KEYS or btype in ("p", "li"):
        val = block[btype]
        if isinstance(val, dict):
            style = val.get("style", {})
            return {k: v for k, v in style.items() if not k.startswith("_")}
    return {}


def _tree_block_raw_attrs(block: dict) -> dict:
    """Extract raw/opaque (_ prefixed) paragraph style keys."""
    btype = _tree_block_type(block)
    if btype in _TREE_HEADING_KEYS or btype in ("p", "li"):
        val = block[btype]
        if isinstance(val, dict):
            style = val.get("style", {})
            return {k: v for k, v in style.items() if k.startswith("_")}
    return {}


def _tree_block_match_key(block: dict) -> str:
    """Block key compatible with _block_key() for ir.Block alignment."""
    btype = _tree_block_type(block)
    if btype in _TREE_HEADING_KEYS:
        text = "".join(_tree_run_text(r) for r in _tree_block_runs(block))
        return f"heading:{text}"
    if btype == "p":
        text = "".join(_tree_run_text(r) for r in _tree_block_runs(block))
        return f"paragraph:{text}"
    if btype == "li":
        text = "".join(_tree_run_text(r) for r in _tree_block_runs(block))
        return f"list_item:{text}"
    if btype == "table":
        return f"table:{_tree_block_text(block)}"
    return f"{btype}:opaque"


def _normalize_tree_runs(runs: list) -> list[tuple[str, dict]]:
    """Merge adjacent runs with identical style.  Returns [(text, style)].

    Makes run boundaries non-semantic: different splits of same
    text+style produce identical normalized form.
    """
    if not runs:
        return []
    result: list[tuple[str, dict]] = []
    cur_text = _tree_run_text(runs[0])
    cur_style = _tree_run_style(runs[0])
    for run in runs[1:]:
        text = _tree_run_text(run)
        style = _tree_run_style(run)
        if style == cur_style:
            cur_text += text
        else:
            if cur_text:
                result.append((cur_text, cur_style))
            cur_text = text
            cur_style = style
    if cur_text:
        result.append((cur_text, cur_style))
    return result


def _build_tree_style_delta(old_style: dict, new_style: dict) -> tuple[dict, str]:
    """Build Docs API textStyle dict and fields from old->new tree style delta."""
    from .tree import _hex_to_color

    api_style: dict = {}
    fields: list[str] = []

    for key, api_key in [
        ("b", "bold"), ("i", "italic"), ("s", "strikethrough"), ("u", "underline"),
    ]:
        old_val = old_style.get(key, False)
        new_val = new_style.get(key, False)
        if old_val != new_val:
            api_style[api_key] = bool(new_val)
            fields.append(api_key)

    old_color = old_style.get("color")
    new_color = new_style.get("color")
    if old_color != new_color:
        api_style["foregroundColor"] = _hex_to_color(new_color) if new_color else {}
        fields.append("foregroundColor")

    old_bg = old_style.get("bg")
    new_bg = new_style.get("bg")
    if old_bg != new_bg:
        api_style["backgroundColor"] = _hex_to_color(new_bg) if new_bg else {}
        fields.append("backgroundColor")

    old_font = old_style.get("font")
    new_font = new_style.get("font")
    if old_font != new_font:
        if new_font:
            api_style["weightedFontFamily"] = {"fontFamily": new_font, "weight": 400}
        else:
            api_style["weightedFontFamily"] = {}
        fields.append("weightedFontFamily")

    old_size = old_style.get("size")
    new_size = new_style.get("size")
    if old_size != new_size:
        if new_size:
            api_style["fontSize"] = {"magnitude": new_size, "unit": "PT"}
        else:
            api_style["fontSize"] = {}
        fields.append("fontSize")

    old_url = old_style.get("url")
    new_url = new_style.get("url")
    if old_url != new_url:
        if new_url:
            api_style["link"] = {"url": new_url}
        else:
            api_style["link"] = {}
        fields.append("link")

    old_bl = old_style.get("baseline")
    new_bl = new_style.get("baseline")
    if old_bl != new_bl:
        api_style["baselineOffset"] = new_bl if new_bl else "NONE"
        fields.append("baselineOffset")

    return api_style, ",".join(fields)


def _tree_style_to_api(style: dict) -> tuple[dict, str]:
    """Convert tree run style dict to Docs API textStyle + fields."""
    from .tree import _hex_to_color

    api_style: dict = {}
    fields: list[str] = []

    if style.get("b"):
        api_style["bold"] = True
        fields.append("bold")
    if style.get("i"):
        api_style["italic"] = True
        fields.append("italic")
    if style.get("s"):
        api_style["strikethrough"] = True
        fields.append("strikethrough")
    if style.get("u"):
        api_style["underline"] = True
        fields.append("underline")
    if "color" in style:
        api_style["foregroundColor"] = _hex_to_color(style["color"])
        fields.append("foregroundColor")
    if "bg" in style:
        api_style["backgroundColor"] = _hex_to_color(style["bg"])
        fields.append("backgroundColor")
    if "font" in style:
        api_style["weightedFontFamily"] = {"fontFamily": style["font"], "weight": 400}
        fields.append("weightedFontFamily")
    if "size" in style:
        api_style["fontSize"] = {"magnitude": style["size"], "unit": "PT"}
        fields.append("fontSize")
    if "url" in style:
        api_style["link"] = {"url": style["url"]}
        fields.append("link")
    if "baseline" in style:
        api_style["baselineOffset"] = style["baseline"]
        fields.append("baselineOffset")

    return api_style, ",".join(fields)


def _tree_style_diff_requests(
    base_runs: list,
    local_runs: list,
    block_start: int,
    tab_id: str,
) -> list[dict]:
    """Generate updateTextStyle for style-only changes (text must match).

    Normalizes runs first so different splits of same text+style produce
    zero mutations.  Then walks per-character styles and emits a single
    updateTextStyle for each contiguous range where style differs.
    """
    base_norm = _normalize_tree_runs(base_runs)
    local_norm = _normalize_tree_runs(local_runs)

    base_text = "".join(t for t, _ in base_norm)
    local_text = "".join(t for t, _ in local_norm)

    if base_text != local_text:
        return []  # Text differs — not a style-only change

    if not base_text:
        return []

    # Build per-character style maps
    def _char_styles(norm_runs: list[tuple[str, dict]]) -> list[dict]:
        result: list[dict] = []
        for text, style in norm_runs:
            for _ in text:
                result.append(style)
        return result

    base_cs = _char_styles(base_norm)
    local_cs = _char_styles(local_norm)

    requests: list[dict] = []
    i = 0
    while i < len(base_cs):
        if base_cs[i] != local_cs[i]:
            range_start = i
            new_style = local_cs[i]
            # Extend while style differs and local keeps same new style
            while (
                i < len(base_cs)
                and base_cs[i] != local_cs[i]
                and local_cs[i] == new_style
            ):
                i += 1
            range_end = i

            start_off = block_start + _utf16_len(base_text[:range_start])
            end_off = block_start + _utf16_len(base_text[:range_end])
            api_style, api_fields = _build_tree_style_delta(
                base_cs[range_start], new_style
            )
            if api_style and api_fields:
                requests.append({
                    "updateTextStyle": {
                        "range": {
                            "startIndex": start_off,
                            "endIndex": end_off,
                            "tabId": tab_id,
                        },
                        "textStyle": api_style,
                        "fields": api_fields,
                    }
                })
        else:
            i += 1

    return requests


def _tree_full_style_requests(
    normalized_runs: list[tuple[str, dict]],
    block_start: int,
    tab_id: str,
) -> list[dict]:
    """Apply full style for each run (after text splice)."""
    requests: list[dict] = []
    offset = block_start
    for text, style in normalized_runs:
        if not text:
            continue
        span_end = offset + _utf16_len(text)
        if style:
            api_style, api_fields = _tree_style_to_api(style)
            if api_style:
                requests.append({
                    "updateTextStyle": {
                        "range": {
                            "startIndex": offset,
                            "endIndex": span_end,
                            "tabId": tab_id,
                        },
                        "textStyle": api_style,
                        "fields": api_fields,
                    }
                })
        offset = span_end
    return requests


def _tree_para_style_diff_requests(
    base_style: dict,
    local_style: dict,
    block_start: int,
    block_end: int,
    tab_id: str,
) -> list[dict]:
    """Generate updateParagraphStyle for paragraph-style changes."""
    if base_style == local_style:
        return []

    api_style: dict = {}
    fields: list[str] = []

    old_align = base_style.get("align")
    new_align = local_style.get("align")
    if old_align != new_align:
        api_style["alignment"] = (new_align or "START").upper()
        fields.append("alignment")

    old_ls = base_style.get("line_spacing")
    new_ls = local_style.get("line_spacing")
    if old_ls != new_ls:
        if new_ls:
            api_style["lineSpacing"] = new_ls
        fields.append("lineSpacing")

    old_sa = base_style.get("space_above")
    new_sa = local_style.get("space_above")
    if old_sa != new_sa:
        api_style["spaceAbove"] = (
            {"magnitude": new_sa, "unit": "PT"} if new_sa else {}
        )
        fields.append("spaceAbove")

    old_sb = base_style.get("space_below")
    new_sb = local_style.get("space_below")
    if old_sb != new_sb:
        api_style["spaceBelow"] = (
            {"magnitude": new_sb, "unit": "PT"} if new_sb else {}
        )
        fields.append("spaceBelow")

    if not fields:
        return []

    return [{
        "updateParagraphStyle": {
            "range": {
                "startIndex": block_start,
                "endIndex": block_end,
                "tabId": tab_id,
            },
            "paragraphStyle": api_style,
            "fields": ",".join(fields),
        }
    }]


def _tree_heading_level_requests(
    base_type: str,
    local_type: str,
    block_start: int,
    block_end: int,
    tab_id: str,
) -> list[dict]:
    """Generate updateParagraphStyle for heading level changes."""
    base_is_heading = base_type in _TREE_HEADING_KEYS
    local_is_heading = local_type in _TREE_HEADING_KEYS

    if base_is_heading and local_is_heading:
        base_level = int(base_type[1:])
        local_level = int(local_type[1:])
        if base_level == local_level:
            return []
        return [{
            "updateParagraphStyle": {
                "range": {
                    "startIndex": block_start,
                    "endIndex": block_end,
                    "tabId": tab_id,
                },
                "paragraphStyle": {
                    "namedStyleType": HEADING_STYLE_MAP.get(local_level, "HEADING_1"),
                },
                "fields": "namedStyleType",
            }
        }]
    elif base_is_heading and not local_is_heading:
        return [{
            "updateParagraphStyle": {
                "range": {
                    "startIndex": block_start,
                    "endIndex": block_end,
                    "tabId": tab_id,
                },
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        }]
    elif not base_is_heading and local_is_heading:
        local_level = int(local_type[1:])
        return [{
            "updateParagraphStyle": {
                "range": {
                    "startIndex": block_start,
                    "endIndex": block_end,
                    "tabId": tab_id,
                },
                "paragraphStyle": {
                    "namedStyleType": HEADING_STYLE_MAP.get(local_level, "HEADING_1"),
                },
                "fields": "namedStyleType",
            }
        }]
    return []


def compute_tree_plan(
    local_tree_body: list,
    local_appendix: dict[str, object] | None,
    remote_body: list[dict],
    remote_revision: str,
    stored_revision: str,
    tab_id: str,
    *,
    lists: dict | None = None,
) -> ThreeWayPlan:
    """Compute a tree plan: diff remote-tree vs local-tree.

    Simple model (ADR 037 / gax-cvi.7): no baseline, no drift.
    Under the revision guard the remote IS the state you pulled,
    so diffing against it gives exactly your edits with correct
    indices.

    Guarantees:
    - Style-only edits emit updateTextStyle (no delete+insert)
    - Identical content with different run splits -> zero mutations
    - Appendix modifications rejected pre-plan
    - Raw/opaque attribute edits rejected pre-plan
    - Unsupported block edits (TOC, _verbatim) rejected pre-plan

    Args:
        local_tree_body: Compressed body from edited YAML.
        local_appendix: Appendix dict from edited file (None if absent).
        remote_body: Current raw doc JSON body from documents().get().
        remote_revision: Current remote revisionId.
        stored_revision: revisionId stored at pull time.
        tab_id: Google Docs tab ID.
        lists: Document lists metadata.

    Returns:
        ThreeWayPlan with mutations targeting the remote document.
    """
    from .tree import (
        _ungroup_list_containers,
        compress_doc,
        extract_appendix,
        resolve_appendix,
        validate_appendix_immutable,
    )

    # --- Revision guard (ADR 037: mismatch = refuse) ---
    if (
        stored_revision
        and remote_revision
        and stored_revision != remote_revision
    ):
        return ThreeWayPlan(
            ops=[], mutations=[], summary_lines=[],
            error=(
                f"Remote changed since pull "
                f"(rev {stored_revision} -> {remote_revision}). "
                f"Pull first."
            ),
            revision_changed=True,
        )

    # --- Compress remote to tree (the "base" for comparison) ---
    remote_compressed = compress_doc(remote_body, lists=lists)

    # --- Appendix immutability check ---
    if local_appendix is not None:
        remote_app = extract_appendix(remote_compressed.body)
        app_errors = validate_appendix_immutable(
            remote_app.appendix, local_appendix,
        )
        if app_errors:
            error_msg = "; ".join(str(e) for e in app_errors)
            return ThreeWayPlan(
                ops=[], mutations=[], summary_lines=[],
                error=f"Appendix modification rejected: {error_msg}",
            )

    # --- Resolve local appendix (bring payloads inline) ---
    if local_appendix:
        local_resolved = resolve_appendix(local_tree_body, local_appendix)
    else:
        local_resolved = list(local_tree_body)

    # --- Flatten list containers ---
    remote_flat = _ungroup_list_containers(remote_compressed.body)
    local_flat = _ungroup_list_containers(local_resolved)

    # --- Remote blocks for doc_range ---
    remote_blocks = from_doc_json(remote_body, lists=lists)

    # --- Align remote-tree <-> remote-blocks (for doc_range lookup) ---
    remote_tree_keys = [
        _tree_block_match_key(b) if isinstance(b, dict) else "unknown"
        for b in remote_flat
    ]
    remote_ir_keys = [_block_key(b) for b in remote_blocks]
    align_sm = difflib.SequenceMatcher(None, remote_tree_keys, remote_ir_keys)

    tree_to_block: dict[int, int] = {}
    for atag, ai1, ai2, aj1, aj2 in align_sm.get_opcodes():
        if atag == "equal":
            for ti, bi in zip(range(ai1, ai2), range(aj1, aj2)):
                tree_to_block[ti] = bi
        elif atag == "replace":
            pairs = min(ai2 - ai1, aj2 - aj1)
            for k in range(pairs):
                tree_to_block[ai1 + k] = aj1 + k

    # --- Block-level diff: remote-tree vs local-tree ---
    local_keys = [
        _tree_block_match_key(b) if isinstance(b, dict) else "unknown"
        for b in local_flat
    ]
    sm = difflib.SequenceMatcher(None, remote_tree_keys, local_keys)

    # --- Generate mutations ---
    mutations: list[dict] = []
    summary: list[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            result = _tree_diff_equal_range(
                remote_flat, local_flat, i1, i2, j1, j2,
                tree_to_block, remote_blocks, tab_id,
            )
            if isinstance(result, str):
                return ThreeWayPlan(
                    ops=[], mutations=[], summary_lines=[],
                    error=result,
                )
            if result is not None:
                muts, summ, _ = result
                mutations.extend(muts)
                summary.extend(summ)

        elif tag == "replace":
            result = _tree_diff_replace_range(
                remote_flat, local_flat, i1, i2, j1, j2,
                tree_to_block, remote_blocks, tab_id,
            )
            if isinstance(result, str):
                return ThreeWayPlan(
                    ops=[], mutations=[], summary_lines=[],
                    error=result,
                )
            muts, summ, _ = result
            mutations.extend(muts)
            summary.extend(summ)

        elif tag == "delete":
            for k in range(i1, i2):
                if k not in tree_to_block:
                    continue
                bi = tree_to_block[k]
                rb = remote_blocks[bi]
                if not rb.doc_range:
                    continue
                mutations.append({
                    "deleteContentRange": {
                        "range": {
                            "startIndex": rb.doc_range[0],
                            "endIndex": rb.doc_range[1],
                            "tabId": tab_id,
                        }
                    }
                })
                summary.append(
                    f"  delete: '{_tree_block_text(remote_flat[k])[:40]}'"
                )

        elif tag == "insert":
            insert_idx = _tree_find_insert_point(
                i1, tree_to_block, remote_blocks,
            )
            for k in range(j1, j2):
                lb = local_flat[k]
                if not isinstance(lb, dict):
                    continue
                imuts = _tree_insert_block_mutations(lb, insert_idx, tab_id)
                mutations.extend(imuts)
                summary.append(f"  insert: '{_tree_block_text(lb)[:40]}'")

    # Sort by descending start index (safe application order)
    def _sort_key(req: dict) -> int:
        for val in req.values():
            if isinstance(val, dict):
                r = val.get("range") or val.get("location")
                if r and "startIndex" in r:
                    return -r["startIndex"]
                if r and "index" in r:
                    return -r["index"]
        return 0

    mutations.sort(key=_sort_key)

    return ThreeWayPlan(
        ops=[],  # Tree mode produces mutations directly
        mutations=mutations,
        summary_lines=summary,
    )


def _tree_diff_equal_range(
    base_flat: list,
    local_flat: list,
    i1: int, i2: int,
    j1: int, j2: int,
    base_to_remote: dict[int, int],
    remote_blocks: list[Block],
    tab_id: str,
) -> tuple[list[dict], list[str], set[int]] | str | None:
    """Diff blocks matched as 'equal' (same key = same type+text).

    Returns (mutations, summary, edited_indices), error string, or None.
    """
    mutations: list[dict] = []
    summary: list[str] = []
    edited: set[int] = set()

    for bi, li in zip(range(i1, i2), range(j1, j2)):
        base_block = base_flat[bi]
        local_block = local_flat[li]
        if not isinstance(base_block, dict) or not isinstance(local_block, dict):
            continue

        btype = _tree_block_type(base_block)
        ltype = _tree_block_type(local_block)

        # Reject edits to unsupported block types
        if btype in ("toc", "_verbatim", "unknown"):
            if base_block != local_block:
                return f"Unsupported edit to {btype} block at index {bi}"
            continue

        # Reject raw attribute edits
        base_raw = _tree_block_raw_attrs(base_block)
        local_raw = _tree_block_raw_attrs(local_block)
        if base_raw != local_raw:
            changed = set(base_raw.keys()) ^ set(local_raw.keys())
            if not changed:
                changed = {k for k in base_raw if base_raw.get(k) != local_raw.get(k)}
            return (
                f"Unsupported edit to raw attribute(s) {sorted(changed)} "
                f"at block {bi}"
            )

        if bi not in base_to_remote:
            continue
        ri = base_to_remote[bi]
        rb = remote_blocks[ri]
        if not rb.doc_range:
            continue

        block_start = rb.doc_range[0]
        block_end = rb.doc_range[1] - 1  # exclude trailing newline

        # Style diff
        style_muts = _tree_style_diff_requests(
            _tree_block_runs(base_block),
            _tree_block_runs(local_block),
            block_start, tab_id,
        )

        # Paragraph style diff
        para_muts = _tree_para_style_diff_requests(
            _tree_block_para_style(base_block),
            _tree_block_para_style(local_block),
            block_start, block_end, tab_id,
        )

        # Heading level change
        heading_muts = _tree_heading_level_requests(
            btype, ltype, block_start, block_end, tab_id,
        )

        if style_muts or para_muts or heading_muts:
            edited.add(bi)
            mutations.extend(style_muts)
            mutations.extend(para_muts)
            mutations.extend(heading_muts)
            text = _tree_block_text(base_block)[:40]
            summary.append(f"  restyle: '{text}'")

    return mutations, summary, edited


def _tree_diff_replace_range(
    base_flat: list,
    local_flat: list,
    i1: int, i2: int,
    j1: int, j2: int,
    base_to_remote: dict[int, int],
    remote_blocks: list[Block],
    tab_id: str,
) -> tuple[list[dict], list[str], set[int]] | str:
    """Diff blocks matched as 'replace' (keys differ)."""
    mutations: list[dict] = []
    summary: list[str] = []
    edited: set[int] = set()

    pairs = min(i2 - i1, j2 - j1)

    # Paired updates
    for k in range(pairs):
        bi = i1 + k
        li = j1 + k
        base_block = base_flat[bi]
        local_block = local_flat[li]

        if not isinstance(base_block, dict) or not isinstance(local_block, dict):
            continue

        btype = _tree_block_type(base_block)
        ltype = _tree_block_type(local_block)

        # Reject unsupported types
        if btype in ("toc", "_verbatim", "unknown") or ltype in ("toc", "_verbatim", "unknown"):
            return f"Unsupported edit: {btype} -> {ltype} at block {bi}"

        if bi not in base_to_remote:
            continue
        ri = base_to_remote[bi]
        rb = remote_blocks[ri]
        if not rb.doc_range:
            continue

        edited.add(bi)
        block_start = rb.doc_range[0]
        block_end = rb.doc_range[1] - 1

        base_text = _tree_block_text(base_block)
        local_text = _tree_block_text(local_block)

        if base_text != local_text:
            # Text changed -- splice + full style application
            mutations.extend(
                _splice_text_requests(base_text, local_text, block_start, tab_id)
            )
            local_norm = _normalize_tree_runs(_tree_block_runs(local_block))
            mutations.extend(
                _tree_full_style_requests(local_norm, block_start, tab_id)
            )
        else:
            # Same text -- style diff only
            style_muts = _tree_style_diff_requests(
                _tree_block_runs(base_block),
                _tree_block_runs(local_block),
                block_start, tab_id,
            )
            mutations.extend(style_muts)

        # Paragraph style diff
        para_muts = _tree_para_style_diff_requests(
            _tree_block_para_style(base_block),
            _tree_block_para_style(local_block),
            block_start, block_end, tab_id,
        )
        mutations.extend(para_muts)

        # Heading level
        heading_muts = _tree_heading_level_requests(
            btype, ltype, block_start, block_end, tab_id,
        )
        mutations.extend(heading_muts)

        base_t = base_text[:30]
        local_t = local_text[:30]
        if base_t == local_t:
            summary.append(f"  restyle: '{base_t}'")
        else:
            summary.append(f"  update: '{base_t}' -> '{local_t}'")

    # Remaining deletes
    for k in range(pairs, i2 - i1):
        bi = i1 + k
        if bi not in base_to_remote:
            continue
        ri = base_to_remote[bi]
        rb = remote_blocks[ri]
        if not rb.doc_range:
            continue
        edited.add(bi)
        mutations.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": rb.doc_range[0],
                    "endIndex": rb.doc_range[1],
                    "tabId": tab_id,
                }
            }
        })
        summary.append(f"  delete: '{_tree_block_text(base_flat[bi])[:40]}'")

    # Remaining inserts
    for k in range(pairs, j2 - j1):
        li = j1 + k
        insert_idx = _tree_find_insert_point_after(
            i1 + pairs - 1 if pairs > 0 else i1 - 1,
            base_to_remote, remote_blocks,
        )
        local_block = local_flat[li]
        if isinstance(local_block, dict):
            imuts = _tree_insert_block_mutations(local_block, insert_idx, tab_id)
            mutations.extend(imuts)
            summary.append(f"  insert: '{_tree_block_text(local_block)[:40]}'")

    return mutations, summary, edited


def _tree_find_insert_point(
    base_idx: int,
    base_to_remote: dict[int, int],
    remote_blocks: list[Block],
) -> int:
    """Find insertion index: after the base block preceding the insert."""
    anchor_bi = base_idx - 1 if base_idx > 0 else None
    return _tree_find_insert_point_after(anchor_bi, base_to_remote, remote_blocks)


def _tree_find_insert_point_after(
    anchor_bi: int | None,
    base_to_remote: dict[int, int],
    remote_blocks: list[Block],
) -> int:
    """Find insertion index after a given base block."""
    if anchor_bi is not None and anchor_bi >= 0 and anchor_bi in base_to_remote:
        ri = base_to_remote[anchor_bi]
        rb = remote_blocks[ri]
        if rb.doc_range:
            return rb.doc_range[1]
    if remote_blocks:
        first = remote_blocks[0]
        if first.doc_range:
            return first.doc_range[0]
    return 1


def _tree_insert_block_mutations(
    block: dict,
    insert_idx: int,
    tab_id: str,
) -> list[dict]:
    """Generate mutations to insert a tree block."""
    btype = _tree_block_type(block)
    if btype not in _TREE_HEADING_KEYS and btype not in ("p", "li"):
        return []  # Only paragraph-like blocks supported for insert

    text = _tree_block_text(block)
    if not text:
        return []

    mutations: list[dict] = []

    mutations.append({
        "insertText": {
            "text": text + "\n",
            "location": {"index": insert_idx, "tabId": tab_id},
        }
    })

    # Apply styles
    local_norm = _normalize_tree_runs(_tree_block_runs(block))
    mutations.extend(_tree_full_style_requests(local_norm, insert_idx, tab_id))

    # Heading style
    if btype in _TREE_HEADING_KEYS:
        level = int(btype[1:])
        mutations.append({
            "updateParagraphStyle": {
                "range": {
                    "startIndex": insert_idx,
                    "endIndex": insert_idx + _utf16_len(text),
                    "tabId": tab_id,
                },
                "paragraphStyle": {
                    "namedStyleType": HEADING_STYLE_MAP.get(level, "HEADING_1"),
                },
                "fields": "namedStyleType",
            }
        })

    return mutations
