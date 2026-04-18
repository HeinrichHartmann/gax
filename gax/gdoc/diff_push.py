"""Diff-based push for Google Docs tabs (experimental).

Gated behind ``gax doc tab push --patch``. See ADR 027 and ADR 030.

Strategy
========

Full-replace push destroys all non-markdown formatting on every push.
Diff-based push computes the minimal set of Docs API mutations needed
to turn the live document into the edited markdown, so collaborator
formatting, comments, suggestions, etc. survive.

Pipeline
--------

    1. Pull remote  — ``ir.from_doc_json(tab_body)`` produces a Block
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

Key invariants
==============

* **UTF-16 indices.** Google Docs addresses content in UTF-16 code
  units, not Python characters. All index math uses ``_utf16_len``.

* **Paragraph ranges include the trailing newline.** Deletions stop
  at ``endIndex - 1`` to preserve paragraph structure.

* **Mutations applied in reverse index order.** Each request only
  shifts indices below the ones already processed.

* **No alignment step.** Unlike ADR 027's original approach, we read
  the remote state directly from Doc JSON via ``ir.from_doc_json``,
  which populates ``doc_range`` on every block. No fuzzy alignment
  between Drive API markdown and Doc JSON is needed.
"""

import difflib
import logging
from dataclasses import dataclass, field

from .ir import (
    _utf16_len,
    Block,
    CodeBlock,
    Heading,
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
    if isinstance(block, Heading):
        return block.text
    elif isinstance(block, Paragraph):
        return block.text
    elif isinstance(block, ListItem):
        return "".join(s.text for s in block.spans)
    elif isinstance(block, Table):
        num_rows = len(block.rows)
        num_cols = max(len(r) for r in block.rows) if block.rows else 0
        return f"[table {num_rows}x{num_cols}]"
    elif isinstance(block, CodeBlock):
        return block.code
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


def _update_paragraph_requests(
    base: Block,
    new_block: Block,
    tab_id: str,
) -> list[dict]:
    """Generate requests to update a paragraph/heading/list_item in place."""
    requests: list[dict] = []

    if not base.doc_range:
        return requests

    start = base.doc_range[0]
    end = base.doc_range[1] - 1  # preserve trailing newline

    if end <= start:
        return requests

    # Step 1: Delete existing text
    requests.append(
        {
            "deleteContentRange": {
                "range": {"startIndex": start, "endIndex": end, "tabId": tab_id}
            }
        }
    )

    # Step 2: Insert new text
    if isinstance(new_block, Heading):
        new_text = new_block.text
        spans = new_block.spans
    elif isinstance(new_block, (Paragraph, ListItem)):
        new_text = "".join(s.text for s in new_block.spans)
        spans = new_block.spans
    else:
        return requests

    requests.append(
        {
            "insertText": {
                "text": new_text,
                "location": {"index": start, "tabId": tab_id},
            }
        }
    )

    # Step 3: Apply heading style
    if isinstance(new_block, Heading):
        style_map = {
            1: "HEADING_1",
            2: "HEADING_2",
            3: "HEADING_3",
            4: "HEADING_4",
            5: "HEADING_5",
            6: "HEADING_6",
        }
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": start,
                        "endIndex": start + _utf16_len(new_text),
                        "tabId": tab_id,
                    },
                    "paragraphStyle": {
                        "namedStyleType": style_map.get(new_block.level, "HEADING_1")
                    },
                    "fields": "namedStyleType",
                }
            }
        )

    # Step 4: Apply inline formatting
    offset = start
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

            para = para_wrapper["paragraph"]
            cell_start = para.get("startIndex")
            cell_end = para.get("endIndex")
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

            offset = cell_start
            for span in edit_spans:
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


def _insert_block_requests(
    block: Block,
    insert_idx: int,
    tab_id: str,
) -> list[dict]:
    """Generate requests to insert a new block at a given index."""
    requests: list[dict] = []

    if isinstance(block, Heading):
        text = block.text + "\n"
        spans = block.spans
    elif isinstance(block, (Paragraph, ListItem)):
        text = "".join(s.text for s in block.spans) + "\n"
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
        style_map = {
            1: "HEADING_1",
            2: "HEADING_2",
            3: "HEADING_3",
            4: "HEADING_4",
            5: "HEADING_5",
            6: "HEADING_6",
        }
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": insert_idx,
                        "endIndex": insert_idx + _utf16_len(block.text),
                        "tabId": tab_id,
                    },
                    "paragraphStyle": {
                        "namedStyleType": style_map.get(block.level, "HEADING_1")
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

    offset = insert_idx
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


# =============================================================================
# Preview
# =============================================================================


@dataclass
class DiffPreview:
    """Result of a dry-run diff computation for user preview."""

    ops: list[EditOp]
    summary_lines: list[str]
    warnings: list[str]
    docs_service: object = field(default=None, repr=False)
    drive_service: object = field(default=None, repr=False)


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
    drive_service=None,
) -> DiffPreview:
    """Compute the diff without applying it. Returns a preview for the user."""
    from googleapiclient.discovery import build
    from ..auth import get_authenticated_credentials

    if docs_service is None:
        creds = get_authenticated_credentials()
        docs_service = build("docs", "v1", credentials=creds)

    warnings: list[str] = []

    # Single API call: get doc JSON with tab content
    doc = (
        docs_service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )

    tab_id = None
    tab_body = None
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("title") == tab_name:
            tab_id = props.get("tabId")
            tab_body = tab.get("documentTab", {}).get("body", {}).get("content", [])
            break

    if not tab_id or tab_body is None:
        return DiffPreview(
            ops=[],
            summary_lines=[],
            warnings=[f"Tab not found: {tab_name}"],
            docs_service=docs_service,
        )

    # Build remote blocks (with doc_range) and local blocks
    remote_blocks = from_doc_json(tab_body, lists=doc.get("lists"))
    local_blocks = from_markdown(edited_markdown)

    ops = ast_diff(remote_blocks, local_blocks)

    if not ops:
        return DiffPreview(
            ops=[],
            summary_lines=[],
            warnings=["No differences found."],
            docs_service=docs_service,
        )

    # Dry-run mutation translator
    try:
        diff_to_mutations(ops, remote_blocks, tab_id)
    except ValueError as e:
        warnings.append(f"Patch cannot be applied: {e}")

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
        ops=ops, summary_lines=summary, warnings=warnings, docs_service=docs_service
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
    drive_service=None,
) -> list[str]:
    """Push local markdown changes using diff-based mutations.

    Single API call to read remote state, diff against local, apply mutations.
    No Drive API export or fuzzy alignment needed.
    """
    from googleapiclient.discovery import build
    from ..auth import get_authenticated_credentials

    if docs_service is None:
        creds = get_authenticated_credentials()
        docs_service = build("docs", "v1", credentials=creds)

    warnings: list[str] = []

    # Step 1: Read doc JSON (single API call)
    doc = (
        docs_service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )

    tab_id = None
    tab_body = None
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("title") == tab_name:
            tab_id = props.get("tabId")
            tab_body = tab.get("documentTab", {}).get("body", {}).get("content", [])
            break

    if not tab_id or not tab_body:
        raise ValueError(f"Tab not found: {tab_name}")

    # Step 2: Build remote blocks (with doc_range) and local blocks
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
