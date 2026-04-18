"""Diff-based push for Google Docs tabs (experimental).

Gated behind ``gax doc tab push --patch``. See ADR 027 for rationale.

Strategy
========

Full-replace push destroys all non-markdown formatting on every push.
Diff-based push instead computes the minimal set of Docs API mutations
needed to turn the live document into the edited markdown, so
collaborator formatting, comments, suggestions, etc. survive.

Pipeline (see the four sections below in source order):

    1. Alignment  — walk the pulled markdown AST and the live doc JSON
                    in parallel; produce an ``AlignedNode`` per AST node
                    carrying the Google Doc index range it occupies.
                    (section: "Alignment")

    2. AST Diff   — ``difflib.SequenceMatcher`` over
                    ``(node_type, node_text)`` keys of base vs edited
                    AST, emitting ``EditOp`` (update / insert / delete).
                    (section: "AST Diff")

    3. Mutations  — translate each ``EditOp`` into Docs API
                    ``batchUpdate`` requests, using the alignment to
                    resolve edit positions to doc indices.
                    (section: "Mutation translator")

    4. Orchestrate — ``diff_push`` / ``preview_diff`` stitch it together
                    and call the API.
                    (sections: "Preview", "Top-level orchestrator")

Key invariants / gotchas
========================

* **UTF-16 indices.** Google Docs addresses content in UTF-16 code
  units, not Python characters. All index math uses ``_utf16_len``
  (borrowed from ``md2docs``). Do not substitute ``len(text)``.

* **Paragraph ranges include the trailing newline.** A paragraph's
  ``[startIndex, endIndex)`` covers its text PLUS its terminal ``\\n``.
  Deletions that want to preserve paragraph structure must stop at
  ``endIndex - 1`` so the newline survives. See
  ``_update_paragraph_requests``.

* **Mutations are applied in reverse index order.** ``diff_to_mutations``
  sorts requests by ``-startIndex`` so each applied request only shifts
  indices *below* an already-processed range, leaving the earlier
  (lower-index) requests' captured indices valid. All requests for a
  single "update" op share the same start index and rely on stable sort
  to keep their emit order (delete → insert → restyle). Multiple
  inserts at the *same* anchor are a known weak spot — they end up in
  reversed doc order because each insert pushes its predecessor down.

* **No revisionId gating.** We do NOT use ``requiredRevisionId`` on
  ``batchUpdate``. The concurrency model is: pull base state, diff,
  push, all in one short window (ms–seconds). If a collaborator edits
  inside that window the push can clobber their change. This is
  accepted for the experimental path; see ADR 027.

* **Drive API markdown export is lossy.** It merges consecutive
  paragraphs (no blank line between them) into one markdown paragraph,
  flattens nested lists, and renders code blocks as blockquotes.
  Alignment accommodates the first via accumulation; see ``align``
  and ``_insert_node_requests`` for the code-block workaround.
"""

import difflib
import logging
from dataclasses import dataclass, field

from .md2docs import (
    parse_markdown,
    _utf16_len,
    Node,
    Heading,
    Paragraph,
    ListItem,
    Table,
    CodeBlock,
    Text,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Alignment: map markdown AST nodes to Google Doc body elements
# =============================================================================

HEADING_STYLES = {
    "HEADING_1": 1,
    "HEADING_2": 2,
    "HEADING_3": 3,
    "HEADING_4": 4,
    "HEADING_5": 5,
    "HEADING_6": 6,
}


@dataclass
class DocElement:
    """A classified Google Doc body element."""

    type: str  # 'heading', 'paragraph', 'list_item', 'table', 'empty'
    text: str
    start_index: int
    end_index: int
    details: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class AlignedNode:
    """A markdown AST node aligned to one or more Google Doc elements."""

    node: Node
    doc_elements: list[DocElement]
    # Convenience: overall index range in the Google Doc
    start_index: int
    end_index: int


def classify_doc_element(elem: dict) -> DocElement:
    """Classify a Google Doc body element."""
    start = elem.get("startIndex", 0)
    end = elem.get("endIndex", 0)

    if "table" in elem:
        table = elem["table"]
        rows = []
        for row in table.get("tableRows", []):
            cells = []
            for cell in row.get("tableCells", []):
                cell_text = ""
                for ce in cell.get("content", []):
                    if "paragraph" in ce:
                        for e in ce["paragraph"].get("elements", []):
                            if "textRun" in e:
                                cell_text += e["textRun"]["content"]
                cells.append(cell_text.strip())
            rows.append(cells)
        num_rows = len(rows)
        num_cols = len(rows[0]) if rows else 0
        return DocElement(
            type="table",
            text=f"[table {num_rows}x{num_cols}]",
            start_index=start,
            end_index=end,
            details={"rows": rows, "num_rows": num_rows, "num_cols": num_cols},
            raw=elem,
        )

    if "paragraph" not in elem:
        return DocElement(
            type="other", text="", start_index=start, end_index=end, raw=elem
        )

    para = elem["paragraph"]
    style = para.get("paragraphStyle", {})
    named_style = style.get("namedStyleType", "NORMAL_TEXT")
    bullet = para.get("bullet")

    text = ""
    for e in para.get("elements", []):
        if "textRun" in e:
            text += e["textRun"]["content"]
    text_stripped = text.strip()

    if not text_stripped:
        return DocElement(
            type="empty", text="", start_index=start, end_index=end, raw=elem
        )

    if named_style in HEADING_STYLES:
        return DocElement(
            type="heading",
            text=text_stripped,
            start_index=start,
            end_index=end,
            details={"level": HEADING_STYLES[named_style]},
            raw=elem,
        )

    if bullet is not None:
        return DocElement(
            type="list_item",
            text=text_stripped,
            start_index=start,
            end_index=end,
            details={
                "nesting": bullet.get("nestingLevel", 0),
                "list_id": bullet.get("listId", ""),
            },
            raw=elem,
        )

    return DocElement(
        type="paragraph",
        text=text_stripped,
        start_index=start,
        end_index=end,
        details={},
        raw=elem,
    )


def walk_doc_body(body_content: list[dict]) -> list[DocElement]:
    """Walk Google Doc body content and classify each element."""
    elements = []
    for elem in body_content:
        classified = classify_doc_element(elem)
        if classified.type != "other":
            elements.append(classified)
    return elements


def _node_type(node: Node) -> str:
    """Map AST node to comparable type string."""
    if isinstance(node, Heading):
        return "heading"
    elif isinstance(node, Paragraph):
        return "paragraph"
    elif isinstance(node, ListItem):
        return "list_item"
    elif isinstance(node, Table):
        return "table"
    elif isinstance(node, CodeBlock):
        return "code_block"
    return "unknown"


def _node_text(node: Node) -> str:
    """Extract plain text from an AST node."""
    if isinstance(node, Heading):
        return node.text
    elif isinstance(node, Paragraph):
        return "".join(c.text for c in node.children)
    elif isinstance(node, ListItem):
        return "".join(c.text for c in node.children)
    elif isinstance(node, Table):
        num_rows = len(node.rows)
        num_cols = max(len(r) for r in node.rows) if node.rows else 0
        return f"[table {num_rows}x{num_cols}]"
    elif isinstance(node, CodeBlock):
        return node.text
    return ""


def _text_len_normalized(text: str) -> int:
    """Character count ignoring whitespace, for fuzzy length matching."""
    return len(text.replace("\n", "").replace(" ", ""))


def align(doc_elements: list[DocElement], ast_nodes: list[Node]) -> list[AlignedNode]:
    """Align markdown AST nodes to Google Doc body elements.

    The Google Doc JSON is always finer-grained: the Drive API markdown
    export sometimes merges consecutive paragraphs into one. We handle
    this by accumulating doc elements until their combined text matches
    the AST node.

    Returns one AlignedNode per AST node that was successfully matched.
    Raises ValueError if alignment fails catastrophically.
    """
    result = []
    di = 0  # doc element index
    ai = 0  # ast node index

    while di < len(doc_elements) and ai < len(ast_nodes):
        d = doc_elements[di]
        node = ast_nodes[ai]
        ntype = _node_type(node)
        ntext = _node_text(node)

        # Skip empty doc paragraphs
        if d.type == "empty":
            di += 1
            continue

        # Direct match
        if d.type == ntype and d.text.strip() == ntext.strip():
            result.append(
                AlignedNode(
                    node=node,
                    doc_elements=[d],
                    start_index=d.start_index,
                    end_index=d.end_index,
                )
            )
            di += 1
            ai += 1
            continue

        # Types agree but text differs — try accumulating doc elements
        if d.type == ntype:
            ast_len = _text_len_normalized(ntext)
            accumulated = [d]
            acc_text = d.text
            acc_len = _text_len_normalized(acc_text)
            di_peek = di + 1

            while acc_len < ast_len and di_peek < len(doc_elements):
                next_d = doc_elements[di_peek]
                if next_d.type == "empty":
                    di_peek += 1
                    continue
                if next_d.type != d.type:
                    break
                accumulated.append(next_d)
                acc_text += "\n" + next_d.text
                acc_len = _text_len_normalized(acc_text)
                di_peek += 1

            text_match = acc_text.strip() == ntext.strip()
            len_match = abs(acc_len - ast_len) <= 2

            if text_match or (len_match and len(accumulated) > 1):
                result.append(
                    AlignedNode(
                        node=node,
                        doc_elements=accumulated,
                        start_index=accumulated[0].start_index,
                        end_index=accumulated[-1].end_index,
                    )
                )
                di = di_peek
                ai += 1
                continue

        # ======================================================================
        # UNSAFE PATH — experimental fallback on alignment mismatch.
        # ----------------------------------------------------------------------
        # We could not match this doc element to this AST node (types disagree,
        # or accumulation didn't converge). We advance both cursors anyway and
        # emit an AlignedNode that pairs the AST node with WHATEVER doc element
        # happens to sit at `di`. That means downstream mutations for this
        # node will target an index range that does NOT correspond to its
        # text — a patch for a heading may delete a paragraph, etc.
        #
        # Kept as a "best effort" because refusing to align at all makes
        # --patch unusable on any doc the Drive API export surprises us with.
        # Prefer a partly-wrong push that the user can inspect over a hard
        # failure, while --patch is still experimental. Revisit when we have
        # either a hand-rolled pull converter (ADR 027 "Alternatives") or
        # enough field data to classify the mismatches.
        #
        # If you're debugging a corrupted doc after --patch, this is the
        # first place to look.
        # ======================================================================
        logger.warning(
            f"Alignment mismatch at doc[{di}]={d.type}:{d.text[:30]!r} "
            f"vs ast[{ai}]={ntype}:{ntext[:30]!r} — "
            "downstream mutations for this node may target wrong indices"
        )
        result.append(
            AlignedNode(
                node=node,
                doc_elements=[d],
                start_index=d.start_index,
                end_index=d.end_index,
            )
        )
        di += 1
        ai += 1

    # Remaining AST nodes without doc matches get no alignment
    while ai < len(ast_nodes):
        logger.warning(f"AST node {ai} ({_node_type(ast_nodes[ai])}) has no doc match")
        ai += 1

    return result


# =============================================================================
# AST Diff: compare base AST against edited AST
# =============================================================================


@dataclass
class EditOp:
    """A single edit operation between base and edited AST."""

    type: str  # 'update', 'insert', 'delete'
    base_idx: int | None  # index in base AST (None for inserts)
    edit_idx: int | None  # index in edited AST (None for deletes)
    base_node: Node | None
    edit_node: Node | None
    # For inserts: the base index after which to insert (None = insert at start)
    insert_after: int | None = None


def _node_key(node: Node) -> str:
    """Produce a hashable key for sequence matching."""
    if isinstance(node, Table):
        # Include cell content so cell edits are detected
        cell_texts = []
        for row in node.rows:
            for cell in row:
                cell_texts.append("".join(s.text for s in cell))
        return f"table:{','.join(cell_texts)}"
    return f"{_node_type(node)}:{_node_text(node)}"


def ast_diff(base_nodes: list[Node], edited_nodes: list[Node]) -> list[EditOp]:
    """Diff two AST node lists, producing edit operations.

    Uses SequenceMatcher on node keys (type + text) to find
    matching blocks, then emits update/insert/delete ops.
    """
    base_keys = [_node_key(n) for n in base_nodes]
    edit_keys = [_node_key(n) for n in edited_nodes]

    sm = difflib.SequenceMatcher(None, base_keys, edit_keys)
    ops = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # Check if inline formatting changed even though text is the same
            for bi, ei in zip(range(i1, i2), range(j1, j2)):
                if _node_key(base_nodes[bi]) == _node_key(edited_nodes[ei]):
                    # Text matches — but check if formatting differs
                    if _formatting_differs(base_nodes[bi], edited_nodes[ei]):
                        ops.append(
                            EditOp("update", bi, ei, base_nodes[bi], edited_nodes[ei])
                        )
                    # else: truly equal, no op needed
        elif tag == "replace":
            # Pair up replacements, then handle length differences
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                ops.append(
                    EditOp(
                        "update",
                        i1 + k,
                        j1 + k,
                        base_nodes[i1 + k],
                        edited_nodes[j1 + k],
                    )
                )
            # Extra base nodes → deletes
            for k in range(pairs, i2 - i1):
                ops.append(EditOp("delete", i1 + k, None, base_nodes[i1 + k], None))
            # Extra edited nodes → inserts (after last base node in this block)
            insert_after = i1 + pairs - 1 if pairs > 0 else i1 - 1
            for k in range(pairs, j2 - j1):
                ops.append(
                    EditOp(
                        "insert",
                        None,
                        j1 + k,
                        None,
                        edited_nodes[j1 + k],
                        insert_after=insert_after if insert_after >= 0 else None,
                    )
                )
        elif tag == "delete":
            for k in range(i1, i2):
                ops.append(EditOp("delete", k, None, base_nodes[k], None))
        elif tag == "insert":
            # Insert before base position i1 → after base node i1-1
            insert_after = i1 - 1 if i1 > 0 else None
            for k in range(j1, j2):
                ops.append(
                    EditOp(
                        "insert",
                        None,
                        k,
                        None,
                        edited_nodes[k],
                        insert_after=insert_after,
                    )
                )

    return ops


def _formatting_differs(a: Node, b: Node) -> bool:
    """Check if two same-text nodes have different inline formatting."""
    if not isinstance(a, type(b)):
        return True
    if isinstance(a, Heading) and isinstance(b, Heading):
        if a.level != b.level:
            return True
        return _spans_differ(a.children, b.children)
    if isinstance(a, (Paragraph, ListItem)) and isinstance(b, (Paragraph, ListItem)):
        return _spans_differ(a.children, b.children)
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


def _spans_differ(a: list[Text], b: list[Text]) -> bool:
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


# =============================================================================
# Mutation translator: diff ops → Docs API requests
# =============================================================================


def diff_to_mutations(
    ops: list[EditOp],
    alignment: list[AlignedNode],
    tab_id: str,
) -> list[dict]:
    """Translate edit operations into Docs API batchUpdate requests.

    Supported ops:

    * ``update`` for paragraph / heading / list item (delete existing
      text, insert new text, re-apply paragraph style and inline spans).
    * ``update`` for table cells of unchanged shape (per-cell patch).
    * ``insert`` of paragraph / heading / list item / code block at the
      end of an aligned base node (or at doc start if inserting before
      the first element).
    * ``delete`` of an aligned base node's full range.

    Raises ``ValueError`` for shapes we refuse to touch: changing table
    row/column count, multi-paragraph table cells, and update ops where
    base and edit node types disagree.

    Requests are returned sorted by descending start index; see the
    module docstring for why that ordering matters.
    """
    requests = []

    for op in ops:
        if op.type == "update":
            if op.base_idx is None or op.base_idx >= len(alignment):
                raise ValueError(
                    f"Update op references unaligned base node {op.base_idx}"
                )

            aligned = alignment[op.base_idx]
            base = op.base_node
            edit = op.edit_node

            if isinstance(base, (Paragraph, Heading)) and isinstance(
                edit, (Paragraph, Heading)
            ):
                requests.extend(_update_paragraph_requests(aligned, edit, tab_id))
            elif isinstance(base, ListItem) and isinstance(edit, ListItem):
                requests.extend(_update_paragraph_requests(aligned, edit, tab_id))
            elif isinstance(base, Table) and isinstance(edit, Table):
                requests.extend(_update_table_requests(aligned, base, edit, tab_id))
            else:
                raise ValueError(
                    f"Cannot translate update for {_node_type(base)} → {_node_type(edit)}"
                )

        elif op.type == "insert":
            if op.edit_node is None:
                continue
            # Find insertion index: after the aligned node, or at doc start
            if op.insert_after is not None and op.insert_after < len(alignment):
                insert_idx = alignment[op.insert_after].end_index
            elif alignment:
                # Insert before the first node
                insert_idx = alignment[0].start_index
            else:
                insert_idx = 1  # start of document

            requests.extend(_insert_node_requests(op.edit_node, insert_idx, tab_id))

        elif op.type == "delete":
            if op.base_idx is None or op.base_idx >= len(alignment):
                continue
            aligned = alignment[op.base_idx]
            # Delete the full range including the trailing newline
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": aligned.start_index,
                            "endIndex": aligned.end_index,
                            "tabId": tab_id,
                        }
                    }
                }
            )

    # Apply requests in descending start-index order, so each request only
    # shifts content at indices BELOW the ones we've already processed — the
    # captured indices on not-yet-applied requests stay valid.
    #
    # Assumptions this relies on (not asserted; break at your peril):
    #   * Python's sort is stable, so the delete+insert+style requests that
    #     share the same start index keep their emit order (delete → insert →
    #     restyle → inline styles). `_update_paragraph_requests` depends on
    #     this.
    #   * `_sort_key` looks for a `range.startIndex` or `location.index`
    #     on the first dict value — OK for every request shape we emit
    #     today (deleteContentRange, insertText, updateParagraphStyle,
    #     updateTextStyle, createParagraphBullets). A new request shape
    #     without either will silently sort to 0.
    #
    # Known weak spot: multiple `insert` ops with the same anchor will
    # arrive at the same insert_idx, share the same sort key, and execute
    # in emit order — but each insertText pushes its predecessor down, so
    # the final doc order is REVERSED relative to the diff's intent. We
    # accept this while --patch is experimental; fix by coalescing
    # same-anchor inserts into a single insertText when it bites us.
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
    aligned: AlignedNode,
    new_node: Node,
    tab_id: str,
) -> list[dict]:
    """Generate requests to update a paragraph/heading/list_item in place.

    Strategy: delete the old text content, insert new text, apply formatting.
    We preserve the paragraph structure (heading style, bullet) and only
    replace the text content and inline styles.
    """
    requests = []

    # The content range to replace: from start of first element to end of last,
    # but we need to be careful about the trailing newline.
    # Each paragraph has a trailing \n that we must preserve.
    start = aligned.start_index
    # endIndex of the last element includes the trailing \n
    # We delete up to but not including the trailing \n
    end = aligned.end_index - 1

    if end <= start:
        return requests

    # Step 1: Delete existing text (preserve trailing newline)
    requests.append(
        {
            "deleteContentRange": {
                "range": {
                    "startIndex": start,
                    "endIndex": end,
                    "tabId": tab_id,
                }
            }
        }
    )

    # Step 2: Insert new text
    if isinstance(new_node, Heading):
        new_text = new_node.text
        children = new_node.children
    elif isinstance(new_node, (Paragraph, ListItem)):
        new_text = "".join(c.text for c in new_node.children)
        children = new_node.children
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

    # Step 3: Apply heading style if it's a heading (or changed level)
    if isinstance(new_node, Heading):
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
                        "namedStyleType": style_map.get(new_node.level, "HEADING_1"),
                    },
                    "fields": "namedStyleType",
                }
            }
        )

    # Step 4: Apply inline formatting (bold, italic, strikethrough, links)
    offset = start
    for span in children:
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


def _cell_plain(spans: list[Text]) -> str:
    """Plain text for a cell's span list."""
    return "".join(s.text for s in spans)


def _update_table_requests(
    aligned: AlignedNode,
    base_table: Table,
    edit_table: Table,
    tab_id: str,
) -> list[dict]:
    """Generate requests to patch changed cells in a same-shape table.

    Walks both tables cell by cell. For cells that differ, deletes the
    old content and inserts the new content with formatting.

    Raises ValueError if table dimensions changed or cells have
    multi-paragraph content that markdown can't represent.
    """
    base_rows = base_table.rows
    edit_rows = edit_table.rows

    if len(base_rows) != len(edit_rows):
        raise ValueError(
            f"Table row count changed ({len(base_rows)} → {len(edit_rows)}). "
            "Patch cannot add/remove table rows."
        )

    for ri, (br, er) in enumerate(zip(base_rows, edit_rows)):
        if len(br) != len(er):
            raise ValueError(
                f"Table column count changed in row {ri} ({len(br)} → {len(er)}). "
                "Patch cannot add/remove table columns."
            )

    # Get the raw Google Doc table JSON from the aligned element
    doc_elem = aligned.doc_elements[0]
    if "table" not in doc_elem.raw:
        raise ValueError("Aligned element is not a table")

    doc_table = doc_elem.raw["table"]
    doc_rows = doc_table.get("tableRows", [])

    requests = []

    for ri, (base_row, edit_row) in enumerate(zip(base_rows, edit_rows)):
        if ri >= len(doc_rows):
            break
        doc_row = doc_rows[ri]
        doc_cells = doc_row.get("tableCells", [])

        for ci, (base_spans, edit_spans) in enumerate(zip(base_row, edit_row)):
            if ci >= len(doc_cells):
                break

            # Check if cell content changed
            base_text = _cell_plain(base_spans)
            edit_text = _cell_plain(edit_spans)

            if base_text == edit_text and not _spans_differ(base_spans, edit_spans):
                continue  # cell unchanged

            # Get cell's paragraph indices from the doc JSON
            cell_content = doc_cells[ci].get("content", [])

            if len(cell_content) > 1:
                # Multi-paragraph cell — bail
                raise ValueError(
                    f"Cell [{ri},{ci}] has {len(cell_content)} paragraphs. "
                    "Patch cannot edit multi-paragraph table cells."
                )

            if not cell_content:
                continue

            para = cell_content[0]
            if "paragraph" not in para:
                continue

            cell_start = para.get("startIndex")
            cell_end = para.get("endIndex")
            if cell_start is None or cell_end is None:
                continue

            # Delete old content (preserve trailing newline)
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

            # Insert new text
            if edit_text:
                requests.append(
                    {
                        "insertText": {
                            "text": edit_text,
                            "location": {"index": cell_start, "tabId": tab_id},
                        }
                    }
                )

            # Apply inline formatting
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


def _insert_node_requests(
    node: Node,
    insert_idx: int,
    tab_id: str,
) -> list[dict]:
    """Generate requests to insert a new node at a given index.

    Inserts the text content with a trailing newline, then applies
    paragraph style and inline formatting.
    """
    requests = []

    if isinstance(node, Heading):
        text = node.text + "\n"
        children = node.children
    elif isinstance(node, (Paragraph, ListItem)):
        text = "".join(c.text for c in node.children) + "\n"
        children = node.children
    elif isinstance(node, CodeBlock):
        # Emit code blocks as blockquote-prefixed lines. This is not a
        # mistake: the Drive API markdown export does not preserve real
        # Google Docs code blocks — it round-trips them as blockquoted
        # text. To stay consistent with what we pull back, insert goes
        # out the same way. Revisit if/when Drive export learns to
        # represent code blocks natively.
        prefixed = "\n".join(f"> {line}" for line in node.text.split("\n"))
        text = prefixed + "\n"
        children = []
    else:
        return requests

    # Insert text
    requests.append(
        {
            "insertText": {
                "text": text,
                "location": {"index": insert_idx, "tabId": tab_id},
            }
        }
    )

    # Apply heading style
    if isinstance(node, Heading):
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
                        "endIndex": insert_idx + _utf16_len(node.text),
                        "tabId": tab_id,
                    },
                    "paragraphStyle": {
                        "namedStyleType": style_map.get(node.level, "HEADING_1"),
                    },
                    "fields": "namedStyleType",
                }
            }
        )

    # Apply bullet style
    if isinstance(node, ListItem):
        text_len = _utf16_len(text)
        preset = (
            "NUMBERED_DECIMAL_NESTED" if node.ordered else "BULLET_DISC_CIRCLE_SQUARE"
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

    # Apply inline formatting
    offset = insert_idx
    for span in children:
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
    # Cached services so the subsequent push doesn't re-authenticate
    docs_service: object = field(default=None, repr=False)
    drive_service: object = field(default=None, repr=False)


def _op_summary(op: EditOp) -> str:
    """Human-readable one-line summary of an edit operation."""
    if op.type == "update":
        ntype = _node_type(op.base_node) if op.base_node else "?"
        old_text = (_node_text(op.base_node) if op.base_node else "")[:50]
        new_text = (_node_text(op.edit_node) if op.edit_node else "")[:50]
        if old_text == new_text:
            return f"  restyle {ntype}: {old_text!r}"
        return f"  update {ntype}: {old_text!r} → {new_text!r}"
    elif op.type == "insert":
        ntype = _node_type(op.edit_node) if op.edit_node else "?"
        text = (_node_text(op.edit_node) if op.edit_node else "")[:50]
        return f"  insert {ntype}: {text!r}"
    elif op.type == "delete":
        ntype = _node_type(op.base_node) if op.base_node else "?"
        text = (_node_text(op.base_node) if op.base_node else "")[:50]
        return f"  delete {ntype}: {text!r}"
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
    from . import native_md
    from googleapiclient.discovery import build
    from ..auth import get_authenticated_credentials

    if docs_service is None or drive_service is None:
        creds = get_authenticated_credentials()
        if docs_service is None:
            docs_service = build("docs", "v1", credentials=creds)
        if drive_service is None:
            drive_service = build("drive", "v3", credentials=creds)

    warnings = []

    # Pull + parse
    base_markdown = native_md.export_tab_markdown(
        document_id,
        tab_name,
        docs_service=docs_service,
        drive_service=drive_service,
    )

    base_nodes = parse_markdown(base_markdown)
    edited_nodes = parse_markdown(edited_markdown)

    ops = ast_diff(base_nodes, edited_nodes)

    if not ops:
        return DiffPreview(
            ops=[],
            summary_lines=[],
            warnings=["No differences found."],
            docs_service=docs_service,
            drive_service=drive_service,
        )

    # Dry-run the mutation translator so unsupported ops (table shape
    # changes, multi-paragraph cells, mismatched types) surface here
    # rather than after the user confirms. We pull the doc JSON and run
    # alignment + diff_to_mutations; the result is discarded — diff_push
    # will redo the work against fresh state right before applying.
    try:
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
        if tab_id and tab_body is not None:
            doc_elements = walk_doc_body(tab_body)
            alignment = align(doc_elements, base_nodes)
            diff_to_mutations(ops, alignment, tab_id)
    except ValueError as e:
        warnings.append(f"Patch cannot be applied: {e}")

    # Build summary
    updates = [op for op in ops if op.type == "update"]
    inserts = [op for op in ops if op.type == "insert"]
    deletes = [op for op in ops if op.type == "delete"]

    summary = []
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
        ops=ops,
        summary_lines=summary,
        warnings=warnings,
        docs_service=docs_service,
        drive_service=drive_service,
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

    Args:
        document_id: Google Docs document ID
        tab_name: Name of the tab to update
        edited_markdown: The locally edited markdown content
        docs_service: Optional Docs API service
        drive_service: Optional Drive API service

    Returns:
        List of warning messages (empty if all went well)

    Raises:
        ValueError: If the diff contains shapes we refuse to translate
            (table row/column-count change, multi-paragraph table
            cells, mismatched node types on an update). The caller
            should fall back to full-replace push in that case.
    """
    from . import native_md
    from googleapiclient.discovery import build
    from ..auth import get_authenticated_credentials

    if docs_service is None or drive_service is None:
        creds = get_authenticated_credentials()
        if docs_service is None:
            docs_service = build("docs", "v1", credentials=creds)
        if drive_service is None:
            drive_service = build("drive", "v3", credentials=creds)

    warnings = []

    # Step 1: Pull current markdown (base state)
    base_markdown = native_md.export_tab_markdown(
        document_id,
        tab_name,
        docs_service=docs_service,
        drive_service=drive_service,
    )

    # Step 2: Read doc JSON for index mapping
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

    # Step 3: Parse both markdowns into AST
    base_nodes = parse_markdown(base_markdown)
    edited_nodes = parse_markdown(edited_markdown)

    # Step 4: Align base AST with doc JSON
    doc_elements = walk_doc_body(tab_body)
    alignment = align(doc_elements, base_nodes)

    if len(alignment) != len(base_nodes):
        warnings.append(
            f"Alignment incomplete: {len(alignment)}/{len(base_nodes)} nodes aligned. "
            "Some changes may not be applied."
        )

    # Step 5: Diff base vs edited
    ops = ast_diff(base_nodes, edited_nodes)

    if not ops:
        warnings.append("No differences found between base and edited markdown.")
        return warnings

    # Summarize what changed
    updates = [op for op in ops if op.type == "update"]
    inserts = [op for op in ops if op.type == "insert"]
    deletes = [op for op in ops if op.type == "delete"]

    logger.info(
        f"Diff: {len(updates)} updates, {len(inserts)} inserts, {len(deletes)} deletes"
    )

    # Step 6: Translate to mutations (will raise ValueError for unsupported ops)
    mutations = diff_to_mutations(ops, alignment, tab_id)

    if not mutations:
        warnings.append("Diff produced no API mutations.")
        return warnings

    # Step 7: Apply mutations
    logger.info(f"Applying {len(mutations)} API requests")
    docs_service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": mutations},
    ).execute()

    return warnings
