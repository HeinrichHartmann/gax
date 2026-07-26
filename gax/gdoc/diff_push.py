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
# Three-way plan (ADR 034 §2 — baseline-aware diff)
# =============================================================================


@dataclass
class ThreeWayPlan:
    """Result of a three-way diff computation.

    User edits are computed as diff(base, local) instead of diff(remote, local).
    Mutations are mapped onto remote block ranges for correct index resolution.
    """

    ops: list[EditOp]
    mutations: list[dict]
    summary_lines: list[str]
    drift_blocks: list[int] = field(default_factory=list)  # remote-changed block indices
    error: str | None = None
    revision_changed: bool = False  # True if remote revisionId differs from stored

    @property
    def is_empty(self) -> bool:
        return len(self.ops) == 0


def compute_three_way_plan(
    baseline_hash: str,
    local_markdown: str,
    remote_body: list[dict],
    remote_revision: str,
    stored_revision: str,
    tab_id: str,
    *,
    lists: dict | None = None,
) -> ThreeWayPlan:
    """Compute a three-way plan: base vs local, mapped onto remote ranges.

    ADR 034 §2: user edits = diff(base-rendered md, working md).
    Mutations resolve indices from the remote blocks (current document state).

    Args:
        baseline_hash: CAS hash of the stored baseline tab JSON.
        local_markdown: The user's edited markdown.
        remote_body: Current remote tab body content (from documents().get()).
        remote_revision: Current remote revisionId.
        stored_revision: revisionId stored at pull time.
        tab_id: Google Docs tab ID for mutation targeting.
        lists: Lists metadata from the document.

    Returns:
        ThreeWayPlan with ops, mutations, and drift information.
    """
    from ..store import load_baseline
    from .ir import render_markdown

    # --- Revision gate ---
    revision_changed = bool(
        stored_revision and remote_revision and stored_revision != remote_revision
    )

    # --- Load baseline ---
    baseline_json = load_baseline(baseline_hash)
    if baseline_json is None:
        # No baseline: degrade to stateless diff (current behavior)
        remote_blocks = from_doc_json(remote_body, lists=lists)
        local_blocks = from_markdown(local_markdown)
        ops = ast_diff(remote_blocks, local_blocks)
        error = None
        try:
            mutations = diff_to_mutations(ops, remote_blocks, tab_id) if ops else []
        except ValueError as e:
            mutations = []
            error = str(e)
        summary = _build_summary(ops)
        return ThreeWayPlan(
            ops=ops,
            mutations=mutations,
            summary_lines=["(no baseline — stateless fallback)"] + summary,
            error=error,
            revision_changed=revision_changed,
        )

    # --- Render base markdown from stored JSON ---
    base_body = baseline_json.get("body", {}).get("content", [])
    base_lists = baseline_json.get("lists")
    base_blocks = from_doc_json(base_body, lists=base_lists)
    base_md = render_markdown(base_blocks)

    # --- Parse local markdown ---
    local_blocks = from_markdown(local_markdown)

    # --- Compute user edits: diff(base, local) ---
    ops = ast_diff(base_blocks, local_blocks)

    if not ops:
        return ThreeWayPlan(
            ops=[],
            mutations=[],
            summary_lines=[],
            revision_changed=revision_changed,
        )

    # --- Drift detection (if revision changed) ---
    drift_blocks: list[int] = []
    if revision_changed:
        # Compare base-rendered md vs remote-rendered md to find drifted blocks
        remote_blocks_for_drift = from_doc_json(remote_body, lists=lists)
        remote_md = render_markdown(remote_blocks_for_drift)
        remote_reparsed = from_markdown(remote_md)
        base_reparsed = from_markdown(base_md)
        drift_ops = ast_diff(base_reparsed, remote_reparsed)
        drift_blocks = [
            op.base_idx for op in drift_ops if op.base_idx is not None
        ]

        # Check for overlap between user edits and drift
        user_edit_indices = {
            op.base_idx for op in ops if op.base_idx is not None
        }
        overlap = user_edit_indices & set(drift_blocks)
        if overlap:
            return ThreeWayPlan(
                ops=ops,
                mutations=[],
                summary_lines=[],
                drift_blocks=drift_blocks,
                error=(
                    f"Remote changed since pull (rev {stored_revision} → {remote_revision}). "
                    f"Conflicting blocks: {sorted(overlap)}. Pull first to resolve."
                ),
                revision_changed=True,
            )

    # --- Alignment-based doc_range transfer ---
    # Use SequenceMatcher to align base↔remote blocks so that doc_range
    # is transferred to the correct base block even when remote drift
    # inserted or deleted blocks (replaces broken positional transfer).
    remote_blocks = from_doc_json(remote_body, lists=lists)

    base_keys = [_block_key(b) for b in base_blocks]
    remote_keys = [_block_key(b) for b in remote_blocks]
    align_sm = difflib.SequenceMatcher(None, base_keys, remote_keys)

    base_to_remote: dict[int, int] = {}
    unmapped_base: set[int] = set()

    for atag, ai1, ai2, aj1, aj2 in align_sm.get_opcodes():
        if atag == "equal":
            for bi, ri in zip(range(ai1, ai2), range(aj1, aj2)):
                base_to_remote[bi] = ri
        elif atag == "replace":
            pairs = min(ai2 - ai1, aj2 - aj1)
            for k in range(pairs):
                base_to_remote[ai1 + k] = aj1 + k
            for k in range(pairs, ai2 - ai1):
                unmapped_base.add(ai1 + k)
        elif atag == "delete":
            for k in range(ai1, ai2):
                unmapped_base.add(k)

    # Transfer doc_range for aligned pairs only
    for bi, ri in base_to_remote.items():
        base_blocks[bi].doc_range = remote_blocks[ri].doc_range

    # User-edited blocks that have no remote match → conflict
    user_edit_indices = {op.base_idx for op in ops if op.base_idx is not None}
    unmapped_edits = user_edit_indices & unmapped_base
    if unmapped_edits:
        return ThreeWayPlan(
            ops=ops,
            mutations=[],
            summary_lines=[],
            drift_blocks=drift_blocks,
            error=(
                f"Cannot map user edits to remote document: base blocks "
                f"{sorted(unmapped_edits)} no longer exist in remote. "
                f"Pull first to resolve."
            ),
            revision_changed=revision_changed,
        )

    error = None
    try:
        mutations = diff_to_mutations(ops, base_blocks, tab_id)
    except ValueError as e:
        mutations = []
        error = str(e)

    summary = _build_summary(ops)
    return ThreeWayPlan(
        ops=ops,
        mutations=mutations,
        summary_lines=summary,
        drift_blocks=drift_blocks,
        error=error,
        revision_changed=revision_changed,
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
