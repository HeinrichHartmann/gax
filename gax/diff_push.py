"""Diff-based push for Google Docs tabs (experimental).

Instead of replacing all content, this module:
1. Aligns the markdown AST with the live Google Doc JSON structure
2. Diffs the base AST (from pull) against the edited AST (local file)
3. Translates diff operations into minimal Docs API mutations
4. Applies only the changed elements

This preserves Google Docs formatting, comments, and other elements
that markdown doesn't represent.

See ADR 027 for design rationale and experimental validation.
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
    "HEADING_1": 1, "HEADING_2": 2, "HEADING_3": 3,
    "HEADING_4": 4, "HEADING_5": 5, "HEADING_6": 6,
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
        return DocElement(type="other", text="", start_index=start, end_index=end, raw=elem)

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
        return DocElement(type="empty", text="", start_index=start, end_index=end, raw=elem)

    if named_style in HEADING_STYLES:
        return DocElement(
            type="heading", text=text_stripped, start_index=start, end_index=end,
            details={"level": HEADING_STYLES[named_style]}, raw=elem,
        )

    if bullet is not None:
        return DocElement(
            type="list_item", text=text_stripped, start_index=start, end_index=end,
            details={"nesting": bullet.get("nestingLevel", 0), "list_id": bullet.get("listId", "")},
            raw=elem,
        )

    return DocElement(
        type="paragraph", text=text_stripped, start_index=start, end_index=end,
        details={}, raw=elem,
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
            result.append(AlignedNode(
                node=node,
                doc_elements=[d],
                start_index=d.start_index,
                end_index=d.end_index,
            ))
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
                result.append(AlignedNode(
                    node=node,
                    doc_elements=accumulated,
                    start_index=accumulated[0].start_index,
                    end_index=accumulated[-1].end_index,
                ))
                di = di_peek
                ai += 1
                continue

        # Type mismatch or accumulation failed — still advance both
        # (best effort; the diff will detect these as changes)
        logger.warning(
            f"Alignment mismatch at doc[{di}]={d.type}:{d.text[:30]!r} "
            f"vs ast[{ai}]={ntype}:{ntext[:30]!r}"
        )
        result.append(AlignedNode(
            node=node,
            doc_elements=[d],
            start_index=d.start_index,
            end_index=d.end_index,
        ))
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
                        ops.append(EditOp("update", bi, ei, base_nodes[bi], edited_nodes[ei]))
                    # else: truly equal, no op needed
        elif tag == "replace":
            # Pair up replacements, then handle length differences
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                ops.append(EditOp("update", i1 + k, j1 + k, base_nodes[i1 + k], edited_nodes[j1 + k]))
            # Extra base nodes → deletes
            for k in range(pairs, i2 - i1):
                ops.append(EditOp("delete", i1 + k, None, base_nodes[i1 + k], None))
            # Extra edited nodes → inserts (after last base node in this block)
            insert_after = i1 + pairs - 1 if pairs > 0 else i1 - 1
            for k in range(pairs, j2 - j1):
                ops.append(EditOp("insert", None, j1 + k, None, edited_nodes[j1 + k],
                                  insert_after=insert_after if insert_after >= 0 else None))
        elif tag == "delete":
            for k in range(i1, i2):
                ops.append(EditOp("delete", k, None, base_nodes[k], None))
        elif tag == "insert":
            # Insert before base position i1 → after base node i1-1
            insert_after = i1 - 1 if i1 > 0 else None
            for k in range(j1, j2):
                ops.append(EditOp("insert", None, k, None, edited_nodes[k],
                                  insert_after=insert_after))

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
    return False


def _spans_differ(a: list[Text], b: list[Text]) -> bool:
    """Check if two span lists have different formatting."""
    if len(a) != len(b):
        return True
    for sa, sb in zip(a, b):
        if (sa.text != sb.text or sa.bold != sb.bold or sa.italic != sb.italic
                or sa.strikethrough != sb.strikethrough or sa.url != sb.url):
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

    Currently supports:
    - Update paragraph/heading text (delete + insert + restyle)
    - Update inline formatting

    Unsupported operations (insert, delete, table changes) raise ValueError.
    """
    requests = []

    for op in ops:
        if op.type == "update":
            if op.base_idx is None or op.base_idx >= len(alignment):
                raise ValueError(f"Update op references unaligned base node {op.base_idx}")

            aligned = alignment[op.base_idx]
            base = op.base_node
            edit = op.edit_node

            if isinstance(base, (Paragraph, Heading)) and isinstance(edit, (Paragraph, Heading)):
                requests.extend(
                    _update_paragraph_requests(aligned, edit, tab_id)
                )
            elif isinstance(base, ListItem) and isinstance(edit, ListItem):
                requests.extend(
                    _update_paragraph_requests(aligned, edit, tab_id)
                )
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

            requests.extend(
                _insert_node_requests(op.edit_node, insert_idx, tab_id)
            )

        elif op.type == "delete":
            if op.base_idx is None or op.base_idx >= len(alignment):
                continue
            aligned = alignment[op.base_idx]
            # Delete the full range including the trailing newline
            requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": aligned.start_index,
                        "endIndex": aligned.end_index,
                        "tabId": tab_id,
                    }
                }
            })

    # Sort requests by index in reverse order so earlier indices stay stable
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
    requests.append({
        "deleteContentRange": {
            "range": {
                "startIndex": start,
                "endIndex": end,
                "tabId": tab_id,
            }
        }
    })

    # Step 2: Insert new text
    if isinstance(new_node, Heading):
        new_text = new_node.text
        children = new_node.children
    elif isinstance(new_node, (Paragraph, ListItem)):
        new_text = "".join(c.text for c in new_node.children)
        children = new_node.children
    else:
        return requests

    requests.append({
        "insertText": {
            "text": new_text,
            "location": {"index": start, "tabId": tab_id},
        }
    })

    # Step 3: Apply heading style if it's a heading (or changed level)
    if isinstance(new_node, Heading):
        style_map = {
            1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
            4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6",
        }
        requests.append({
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
        })

    # Step 4: Apply inline formatting (bold, italic, strikethrough, links)
    offset = start
    for span in children:
        span_end = offset + _utf16_len(span.text)
        if span.bold:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": offset, "endIndex": span_end, "tabId": tab_id},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
        if span.italic:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": offset, "endIndex": span_end, "tabId": tab_id},
                    "textStyle": {"italic": True},
                    "fields": "italic",
                }
            })
        if span.strikethrough:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": offset, "endIndex": span_end, "tabId": tab_id},
                    "textStyle": {"strikethrough": True},
                    "fields": "strikethrough",
                }
            })
        if span.url:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": offset, "endIndex": span_end, "tabId": tab_id},
                    "textStyle": {"link": {"url": span.url}},
                    "fields": "link",
                }
            })
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
        prefixed = "\n".join(f"> {line}" for line in node.text.split("\n"))
        text = prefixed + "\n"
        children = []
    else:
        return requests

    # Insert text
    requests.append({
        "insertText": {
            "text": text,
            "location": {"index": insert_idx, "tabId": tab_id},
        }
    })

    # Apply heading style
    if isinstance(node, Heading):
        style_map = {
            1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
            4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6",
        }
        requests.append({
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
        })

    # Apply bullet style
    if isinstance(node, ListItem):
        text_len = _utf16_len(text)
        preset = "NUMBERED_DECIMAL_NESTED" if node.ordered else "BULLET_DISC_CIRCLE_SQUARE"
        requests.append({
            "createParagraphBullets": {
                "range": {
                    "startIndex": insert_idx,
                    "endIndex": insert_idx + text_len - 1,
                    "tabId": tab_id,
                },
                "bulletPreset": preset,
            }
        })

    # Apply inline formatting
    offset = insert_idx
    for span in children:
        span_end = offset + _utf16_len(span.text)
        if span.bold:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": offset, "endIndex": span_end, "tabId": tab_id},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
        if span.italic:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": offset, "endIndex": span_end, "tabId": tab_id},
                    "textStyle": {"italic": True},
                    "fields": "italic",
                }
            })
        if span.strikethrough:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": offset, "endIndex": span_end, "tabId": tab_id},
                    "textStyle": {"strikethrough": True},
                    "fields": "strikethrough",
                }
            })
        if span.url:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": offset, "endIndex": span_end, "tabId": tab_id},
                    "textStyle": {"link": {"url": span.url}},
                    "fields": "link",
                }
            })
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
    from .auth import get_authenticated_credentials

    if docs_service is None or drive_service is None:
        creds = get_authenticated_credentials()
        if docs_service is None:
            docs_service = build("docs", "v1", credentials=creds)
        if drive_service is None:
            drive_service = build("drive", "v3", credentials=creds)

    warnings = []

    # Pull + parse
    base_markdown = native_md.export_tab_markdown(
        document_id, tab_name,
        docs_service=docs_service,
        drive_service=drive_service,
    )

    base_nodes = parse_markdown(base_markdown)
    edited_nodes = parse_markdown(edited_markdown)

    ops = ast_diff(base_nodes, edited_nodes)

    if not ops:
        return DiffPreview(
            ops=[], summary_lines=[], warnings=["No differences found."],
            docs_service=docs_service, drive_service=drive_service,
        )

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
        ValueError: If the diff contains unsupported operations
            (inserts, deletes, table changes). Caller should fall
            back to full-replace push.
    """
    from . import native_md
    from googleapiclient.discovery import build
    from .auth import get_authenticated_credentials

    if docs_service is None or drive_service is None:
        creds = get_authenticated_credentials()
        if docs_service is None:
            docs_service = build("docs", "v1", credentials=creds)
        if drive_service is None:
            drive_service = build("drive", "v3", credentials=creds)

    warnings = []

    # Step 1: Pull current markdown (base state)
    base_markdown = native_md.export_tab_markdown(
        document_id, tab_name,
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

    logger.info(f"Diff: {len(updates)} updates, {len(inserts)} inserts, {len(deletes)} deletes")

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
