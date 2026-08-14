"""Google Docs resource module for gax.

Resource module — follows the draft.py reference pattern.

Two resource classes that share this module:

  Tab(Resource)  — single tab, single file (.doc.gax.md / .tab.gax.md)
  Doc(Resource)  — whole document, folder (.doc.gax.md.d/)

Module structure
================

  Data classes         — DocSection, Comment, CommentReply
  Multipart format     — format/parse .doc.gax.md files
  API helpers          — extract_doc_id, pull_doc, pull_single_tab
  Tab mutations        — get_tabs_list, create_tab_with_content, update_tab_content
  Comments             — fetch_comments, format_comment, format_comments_section
  Tab(Resource)        — single-tab resource (clone/pull/diff/push)
  Doc(Resource)        — whole-document resource (clone/pull/diff/push + tab_list, tab_import)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).
Additional notes specific to Google Docs:

  Tab is the primary editing unit. A single-tab doc clones to one file;
  a multi-tab doc clones to a folder with one file per tab. Both use
  the same .doc.gax.md file format.

  Tab.push supports two modes: full-replace (default) and incremental
  patch (patch=True, experimental — see ADR 027).

  Doc and Tab share all the helper functions in this module. They live
  in the same file because they are tightly coupled.
"""

import difflib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ..auth import get_service
from .. import gaxfile
from .native_md import extract_images_to_store, inline_images_from_store
from ..syncstate import write_sync_header
from ..ui import operation
from ..resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class TabInfo:
    """Metadata for a single tab in a Google Doc."""

    id: str
    title: str
    index: int
    depth: int = 0
    has_children: bool = False


@dataclass
class DocSection:
    """A section of a Google Doc."""

    title: str  # Document title (repeated in each section)
    source: str  # Source URL (repeated in each section)
    time: str  # ISO timestamp (repeated in each section)
    section: int  # Section number (1-based)
    section_title: str  # Title of this section/tab
    content: str  # Markdown content
    section_type: Optional[str] = None  # 'comments' for comment sections
    tab_depth: int = 0  # 0 = top-level, 1 = child, etc.
    tab_has_children: bool = False  # True if tab has child tabs
    tab_id: str = ""  # Google Docs tabId
    baseline: str = ""  # CAS hash of raw tab JSON at pull time (ADR 034)
    revision: str = ""  # Document revisionId at pull time (ADR 034)


@dataclass
class Comment:
    """A comment from Google Docs."""

    comment_id: str
    author: str
    date: str  # YYYY-MM-DD
    quoted_text: str
    content: str
    resolved: bool
    replies: list["CommentReply"]


@dataclass
class CommentReply:
    """A reply to a comment."""

    reply_id: str
    author: str
    date: str  # YYYY-MM-DD
    content: str


# =============================================================================
# Baseline helpers (ADR 034 §2 — deterministic base rendering)
# =============================================================================


def render_baseline(baseline_hash: str) -> Optional[str]:
    """Render stored baseline JSON to markdown deterministically.

    This produces the "base" state for three-way diff: the markdown as it
    was at pull time. Returns None if the baseline is not found.
    """
    from . import ir
    from ..store import load_baseline

    tab_json = load_baseline(baseline_hash)
    if tab_json is None:
        return None

    body = tab_json.get("body", {}).get("content", [])
    lists = tab_json.get("lists")
    inline_objects = tab_json.get("inlineObjects")
    blocks = ir.from_doc_json(body, lists=lists, inline_objects=inline_objects)
    return ir.render_markdown(blocks)


# =============================================================================
# Multipart format helpers
# =============================================================================


def _doc_section_to_multipart(section: DocSection) -> gaxfile.Section:
    """Convert DocSection to generic multipart Section."""
    headers = {
        "type": "gax/doc",
        "title": section.title,
        "source": section.source,
        "time": section.time,
        "tab": section.section_title,
    }
    if section.section_type:
        headers["tab_type"] = section.section_type
    if section.baseline:
        headers["baseline"] = section.baseline
    if section.revision:
        headers["revision"] = section.revision
    headers = write_sync_header(headers, rev=section.revision)
    return gaxfile.Section(headers=headers, content=section.content)


def _multipart_to_doc_section(section: gaxfile.Section) -> DocSection:
    """Convert generic multipart Section to DocSection."""
    # Support both new (tab) and old (section/section_title) header names
    h = section.headers
    tab_name = h.get("tab", h.get("tab_title", h.get("section_title", "")))
    return DocSection(
        title=h.get("title", ""),
        source=h.get("source", ""),
        time=h.get("time", ""),
        section=int(h.get("section", 1)),  # Keep for internal ordering
        section_title=tab_name,
        content=section.content,
        section_type=h.get("tab_type", h.get("section_type")),
        baseline=h.get("baseline", ""),
        revision=h.get("revision", ""),
    )


def format_section(section: DocSection) -> str:
    """Format a single section as YAML header + markdown body."""
    mp_section = _doc_section_to_multipart(section)
    return gaxfile.format_section(mp_section.headers, mp_section.content)


def format_multipart(sections: list[DocSection]) -> str:
    """Assemble sections into multipart markdown string."""
    mp_sections = [_doc_section_to_multipart(s) for s in sections]
    return gaxfile.format_multipart(mp_sections)


def parse_multipart(text: str) -> list[DocSection]:
    """Parse multipart markdown into sections."""
    mp_sections = gaxfile.parse_multipart(text)
    return [_multipart_to_doc_section(s) for s in mp_sections]


# =============================================================================
# Google Docs API functions
# =============================================================================


def extract_doc_id(url: str) -> str:
    """Extract document ID from Google Docs URL or return as-is."""
    match = re.search(r"/document/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9-_]+", url):
        return url
    raise ValueError(f"Cannot extract document ID from: {url}")


def _fetch_doc(document_id: str, *, docs_service=None, num_retries: int = 0) -> dict:
    """Fetch full document JSON with tab content."""
    if docs_service is None:
        docs_service = get_service("docs", "v1")
    return (
        docs_service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute(num_retries=num_retries)
    )


def _tab_content_to_markdown(doc: dict, tab: dict) -> tuple[str, str]:
    """Convert a tab's body content to markdown via the IR.

    Returns:
        Tuple of (markdown_content, baseline_hash).
        baseline_hash is the CAS key of the raw tab JSON snapshot.
    """
    from . import ir
    from ..store import store_baseline

    doc_tab = tab.get("documentTab", {})
    body = doc_tab.get("body", {}).get("content", [])
    lists = doc_tab.get("lists") or doc.get("lists")
    inline_objects = doc_tab.get("inlineObjects")

    # Store raw tab JSON as baseline (ADR 034 §1)
    baseline_json = {"body": {"content": body}}
    if lists:
        baseline_json["lists"] = lists
    if inline_objects:
        baseline_json["inlineObjects"] = inline_objects
    footnotes = doc_tab.get("footnotes")
    if footnotes:
        baseline_json["footnotes"] = footnotes
    baseline_hash = store_baseline(baseline_json)

    blocks = ir.from_doc_json(body, lists=lists, inline_objects=inline_objects)
    md = ir.render_markdown(blocks)
    # Post-process: extract base64 images to blob store
    md = extract_images_to_store(md)
    return md, baseline_hash


def _tab_content_to_tree_yaml(doc: dict, tab: dict, source_url: str) -> str:
    """Convert a tab's body content to tree YAML via the tree IR.

    Returns the doc-tree/v1 YAML string. Stamps ``revision:`` from
    the document's ``revisionId`` so the ADR 037 push guard works.
    """
    from .tree import serialize_tree_yaml

    doc_tab = tab.get("documentTab", {})
    body = doc_tab.get("body", {}).get("content", [])
    lists = doc_tab.get("lists") or doc.get("lists")
    tab_title = tab.get("tabProperties", {}).get("title", "Tab")
    revision_id = doc.get("revisionId", "")

    return serialize_tree_yaml(
        body,
        source=source_url,
        tab=tab_title,
        revision=revision_id,
        lists=lists,
    )


def _build_baseline_json(doc: dict, tab: dict) -> dict:
    """Build the baseline JSON dict from a fetched doc/tab pair.

    Same structure stored by _tab_content_to_markdown during pull.
    """
    doc_tab = tab.get("documentTab", {})
    body = doc_tab.get("body", {}).get("content", [])
    lists = doc_tab.get("lists") or doc.get("lists")
    inline_objects = doc_tab.get("inlineObjects")
    baseline_json: dict = {"body": {"content": body}}
    if lists:
        baseline_json["lists"] = lists
    if inline_objects:
        baseline_json["inlineObjects"] = inline_objects
    footnotes = doc_tab.get("footnotes")
    if footnotes:
        baseline_json["footnotes"] = footnotes
    return baseline_json


def _refresh_baseline_after_push(
    path: Path,
    document_id: str,
    tab_name: str,
    *,
    docs_service=None,
) -> None:
    """Re-fetch tab JSON after a successful push, store new baseline.

    Updates the tracking file's baseline: and revision: frontmatter so the
    next three-way diff uses the post-push state as its base (ADR 034 §1).
    """
    from ..store import store_baseline

    doc = _fetch_doc(document_id, docs_service=docs_service)
    revision_id = doc.get("revisionId", "")

    # Find the matching tab
    flat = _flatten_tabs(doc.get("tabs", []))
    matched_tab = None
    for tab, info in flat:
        if info.title == tab_name:
            matched_tab = tab
            break
    if matched_tab is None:
        logger.warning(f"Post-push baseline refresh: tab '{tab_name}' not found")
        return

    baseline_json = _build_baseline_json(doc, matched_tab)
    baseline_hash = store_baseline(baseline_json)

    # Update the tracking file's frontmatter
    raw = path.read_text(encoding="utf-8")
    sections_raw = gaxfile.parse_multipart(raw)
    if not sections_raw:
        return

    sections_raw[0].headers["baseline"] = baseline_hash
    sections_raw[0].headers["revision"] = revision_id
    # Also update sync header if present (section == 1)
    if "sync" in sections_raw[0].headers:
        sections_raw[0].headers = write_sync_header(
            sections_raw[0].headers, rev=revision_id
        )

    if len(sections_raw) > 1:
        text = gaxfile.format_multipart(sections_raw)
    else:
        text = gaxfile.format_section(
            sections_raw[0].headers, sections_raw[0].content
        )
    path.write_text(text, encoding="utf-8")
    logger.info(f"Post-push baseline refresh: {baseline_hash[:20]}… rev={revision_id}")


def _refresh_revision_in_file(path: Path, revision_id: str) -> None:
    """Update only the revisionId in a tab file's frontmatter.

    Used after a sibling tab in the same document is pushed: the push bumps
    the document-level revisionId, so all sibling files need their stored
    revision updated to prevent the ADR 037 revision guard from tripping
    on our own push (gax-zo1).
    """
    raw = path.read_text(encoding="utf-8")
    sections_raw = gaxfile.parse_multipart(raw)
    if not sections_raw:
        return

    sections_raw[0].headers["revision"] = revision_id
    if "sync" in sections_raw[0].headers:
        sections_raw[0].headers = write_sync_header(
            sections_raw[0].headers, rev=revision_id
        )

    if len(sections_raw) > 1:
        text = gaxfile.format_multipart(sections_raw)
    else:
        text = gaxfile.format_section(
            sections_raw[0].headers, sections_raw[0].content
        )
    path.write_text(text, encoding="utf-8")


def _flatten_tabs(tabs: list[dict], depth: int = 0) -> list[tuple[dict, TabInfo]]:
    """Recursively flatten a nested tabs structure from the Docs API.

    Returns list of (raw_tab_dict, TabInfo) pairs in document order.
    """
    result = []
    for tab in tabs:
        props = tab.get("tabProperties", {})
        children = tab.get("childTabs", [])
        info = TabInfo(
            id=props.get("tabId", ""),
            title=props.get("title", "Tab"),
            index=0,  # assigned by caller after flattening
            depth=depth,
            has_children=bool(children),
        )
        result.append((tab, info))
        result.extend(_flatten_tabs(children, depth + 1))
    return result


def pull_doc(
    document_id: str,
    source_url: str,
    *,
    docs_service=None,
    num_retries: int = 0,
) -> list[DocSection]:
    """Fetch document from Google Docs API and return list of sections.

    Reads directly from the Docs API JSON (no Drive API markdown export).
    Each tab's content is converted to markdown via the Block/Span IR.
    """
    doc = _fetch_doc(
        document_id,
        docs_service=docs_service,
        num_retries=num_retries,
    )
    doc_title = doc.get("title", "Untitled")
    revision_id = doc.get("revisionId", "")
    flat = _flatten_tabs(doc.get("tabs", []))

    if not flat:
        return []

    # Assign sequential indices
    for i, (_tab, info) in enumerate(flat):
        info.index = i

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = []

    with operation("Processing tabs", total=len(flat)) as op:
        for i, (tab, info) in enumerate(flat, start=1):
            logger.info(f"Processing tab: {info.title}")

            content, baseline_hash = _tab_content_to_markdown(doc, tab)

            sections.append(
                DocSection(
                    title=doc_title,
                    source=source_url,
                    time=time_str,
                    section=i,
                    section_title=info.title,
                    content=content,
                    tab_depth=info.depth,
                    tab_has_children=info.has_children,
                    tab_id=info.id,
                    baseline=baseline_hash,
                    revision=revision_id,
                )
            )
            op.advance()

    return sections


def pull_single_tab(
    document_id: str,
    tab_name: str,
    source_url: str,
    *,
    docs_service=None,
    num_retries: int = 0,
) -> DocSection:
    """Pull a single tab from a document.

    Searches recursively through nested tabs.
    Supports both simple name ("Details") and path-qualified ("Overview/Details").
    Raises ValueError if ambiguous or not found.
    """
    doc = _fetch_doc(
        document_id,
        docs_service=docs_service,
        num_retries=num_retries,
    )
    doc_title = doc.get("title", "Untitled")
    flat = _flatten_tabs(doc.get("tabs", []))

    # Build path for each tab (e.g. "Overview/Details")
    matches = []
    ancestor_stack: list[str] = []
    for tab, info in flat:
        ancestor_stack = ancestor_stack[: info.depth]
        ancestor_stack.append(info.title)
        tab_path = "/".join(ancestor_stack)

        if tab_name == info.title or tab_name == tab_path:
            matches.append((tab, info, tab_path))

    if not matches:
        raise ValueError(f"Tab not found: {tab_name}")
    if len(matches) > 1:
        paths = [m[2] for m in matches]
        raise ValueError(
            f"Ambiguous tab name '{tab_name}'. Use full path: {', '.join(paths)}"
        )

    tab, info, _path = matches[0]
    revision_id = doc.get("revisionId", "")
    content, baseline_hash = _tab_content_to_markdown(doc, tab)
    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return DocSection(
        title=doc_title,
        source=source_url,
        time=time_str,
        section=1,
        section_title=info.title,
        content=content,
        tab_depth=info.depth,
        tab_has_children=info.has_children,
        tab_id=info.id,
        baseline=baseline_hash,
        revision=revision_id,
    )


# =============================================================================
# Tab mutation helpers
# =============================================================================


def get_tabs_list(document_id: str, *, service=None) -> dict:
    """Get document title and list of tabs (including nested).

    Returns:
        Dict with 'title' and 'tabs' (list of TabInfo)
    """
    if service is None:
        service = get_service("docs", "v1")

    document = (
        service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )

    doc_title = document.get("title", "Untitled")
    flat = _flatten_tabs(document.get("tabs", []))

    tabs = []
    for i, (_tab, info) in enumerate(flat):
        info.index = i
        tabs.append(info)

    # If no tabs, document itself is the only "tab"
    if not tabs:
        tabs = [TabInfo(id="", title=doc_title, index=0)]

    return {"title": doc_title, "tabs": tabs}


def create_tab_with_content(
    document_id: str,
    tab_name: str,
    markdown: str,
    *,
    service=None,
    num_retries: int = 0,
) -> tuple[str, list]:
    """Create a new tab and populate it with markdown content.

    Returns:
        Tuple of (tab_id, push_warnings)
    """
    from .ir import from_markdown, to_docs_requests

    if service is None:
        service = get_service("docs", "v1")

    # Step 1: Create the tab
    create_response = (
        service.documents()
        .batchUpdate(
            documentId=document_id,
            body={
                "requests": [{"addDocumentTab": {"tabProperties": {"title": tab_name}}}]
            },
        )
        .execute(num_retries=num_retries)
    )

    # Get the new tab ID from response
    tab_id = create_response["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

    # Step 2: Insert markdown content
    blocks = from_markdown(markdown)
    content_requests, tables_data, warnings = to_docs_requests(blocks, tab_id)
    if content_requests:
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": content_requests},
        ).execute(num_retries=num_retries)

    # Step 3: Populate table cells (read back real indices from API)
    if tables_data:
        _populate_tables(
            service, document_id, tab_id, tables_data, num_retries=num_retries
        )

    return tab_id, warnings


def _populate_tables(
    service,
    document_id: str,
    tab_id: str,
    tables_data: list,
    num_retries: int = 0,
) -> None:
    """Populate empty table cells by reading back actual document indices.

    After insertTable creates empty tables, this reads the document structure
    to get real cell indices, inserts cell content, and applies inline formatting.
    """
    from .ir import _utf16_len

    doc = (
        service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute(num_retries=num_retries)
    )

    # Find the tab's body content
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("tabId") != tab_id:
            continue

        body = tab.get("documentTab", {}).get("body", {})
        content = body.get("content", [])

        # Find table elements in document
        doc_tables = [elem for elem in content if "table" in elem]

        if len(doc_tables) != len(tables_data):
            logger.warning(
                f"Table count mismatch: {len(doc_tables)} in doc vs {len(tables_data)} in markdown"
            )
            return

        # Pass 1: Insert plain text into cells (strip markdown syntax)
        insert_requests = []
        # Track cells that need formatting: (insert_idx, spans)
        cells_to_format = []

        for doc_table, md_rows in zip(doc_tables, tables_data):
            table = doc_table["table"]
            for r, doc_row in enumerate(table.get("tableRows", [])):
                if r >= len(md_rows):
                    break
                md_row = md_rows[r]
                for c, doc_cell in enumerate(doc_row.get("tableCells", [])):
                    spans = md_row[c] if c < len(md_row) else []
                    if not spans:
                        continue
                    cell_content = doc_cell.get("content", [])
                    if not cell_content:
                        continue
                    para = cell_content[0]
                    insert_idx = para.get("startIndex")
                    if insert_idx is None:
                        continue
                    plain = "".join(s.text for s in spans)
                    if not plain:
                        continue

                    loc = {"index": insert_idx, "tabId": tab_id}
                    insert_requests.append(
                        {"insertText": {"text": plain, "location": loc}}
                    )
                    cells_to_format.append((insert_idx, spans))

        if insert_requests:
            # Reverse so last cell is populated first (stable indices)
            insert_requests.reverse()
            service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": insert_requests},
            ).execute(num_retries=num_retries)

        # Pass 2: Apply bold/italic formatting to cell content
        # Re-read the document to get updated indices
        if not cells_to_format:
            break

        has_formatting = any(
            any(s.bold or s.italic or s.strikethrough or s.url for s in spans)
            for _, spans in cells_to_format
        )
        if not has_formatting:
            break

        doc = (
            service.documents()
            .get(documentId=document_id, includeTabsContent=True)
            .execute(num_retries=num_retries)
        )

        # Re-find tables and build formatting requests
        for tab2 in doc.get("tabs", []):
            props2 = tab2.get("tabProperties", {})
            if props2.get("tabId") != tab_id:
                continue

            body2 = tab2.get("documentTab", {}).get("body", {})
            content2 = body2.get("content", [])
            doc_tables2 = [elem for elem in content2 if "table" in elem]

            fmt_requests = []
            for doc_table, md_rows in zip(doc_tables2, tables_data):
                table = doc_table["table"]
                for r, doc_row in enumerate(table.get("tableRows", [])):
                    if r >= len(md_rows):
                        break
                    md_row = md_rows[r]
                    for c, doc_cell in enumerate(doc_row.get("tableCells", [])):
                        spans = md_row[c] if c < len(md_row) else []
                        if not spans:
                            continue
                        cell_content = doc_cell.get("content", [])
                        if not cell_content:
                            continue
                        para = cell_content[0]
                        cell_start = para.get("startIndex")
                        if cell_start is None:
                            continue

                        offset = cell_start
                        for span in spans:
                            span_end = offset + _utf16_len(span.text)
                            if span.bold:
                                fmt_requests.append(
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
                                fmt_requests.append(
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
                                fmt_requests.append(
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
                                fmt_requests.append(
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

            if fmt_requests:
                service.documents().batchUpdate(
                    documentId=document_id,
                    body={"requests": fmt_requests},
                ).execute(num_retries=num_retries)

            break

        break


def update_tab_content(
    document_id: str, tab_name: str, markdown: str, *, service=None
) -> list:
    """Replace tab content with new markdown.

    Returns list of push warnings.
    """
    from .ir import from_markdown, to_docs_requests

    if service is None:
        service = get_service("docs", "v1")

    # Get tab ID by name
    doc = (
        service.documents()
        .get(documentId=document_id, includeTabsContent=True)
        .execute()
    )

    tab_id = None
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("title") == tab_name:
            tab_id = props.get("tabId")
            break

    if not tab_id:
        raise ValueError(f"Tab not found: {tab_name}")

    # Get current content length to delete
    for tab in doc.get("tabs", []):
        props = tab.get("tabProperties", {})
        if props.get("tabId") == tab_id:
            body = tab.get("documentTab", {}).get("body", {})
            content = body.get("content", [])
            if content:
                # Find end index (last element's endIndex - 1 to preserve final newline)
                end_index = content[-1].get("endIndex", 1) - 1
                if end_index > 1:
                    # Delete existing content
                    service.documents().batchUpdate(
                        documentId=document_id,
                        body={
                            "requests": [
                                {
                                    "deleteContentRange": {
                                        "range": {
                                            "startIndex": 1,
                                            "endIndex": end_index,
                                            "tabId": tab_id,
                                        }
                                    }
                                }
                            ]
                        },
                    ).execute()
            break

    # Insert new content
    blocks = from_markdown(markdown)
    content_requests, tables_data, warnings = to_docs_requests(blocks, tab_id)
    if content_requests:
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": content_requests},
        ).execute()

    # Populate table cells (second pass: read back real indices)
    if tables_data:
        _populate_tables(service, document_id, tab_id, tables_data)

    return warnings


# =============================================================================
# Comments
# =============================================================================


def fetch_comments(document_id: str) -> list[Comment]:
    """Fetch comments from Google Drive API."""
    service = get_service("drive", "v3")

    comments = []
    page_token = None

    with operation("Fetching comments"):
        while True:
            result = (
                service.comments()
                .list(
                    fileId=document_id,
                    fields="comments(id,author,createdTime,quotedFileContent,content,resolved,replies(id,author,createdTime,content)),nextPageToken",
                    pageToken=page_token,
                )
                .execute()
            )

            for c in result.get("comments", []):
                logger.info(f"Processing comment: {c.get('id', 'unknown')}")
                # Parse date
                created = c.get("createdTime", "")
                date = created[:10] if created else ""

                # Author email
                author = c.get("author", {}).get("emailAddress", "")
                if not author:
                    author = c.get("author", {}).get("displayName", "Unknown")

                # Quoted text
                quoted = c.get("quotedFileContent", {}).get("value", "")

                # Replies
                replies = []
                for r in c.get("replies", []):
                    r_created = r.get("createdTime", "")
                    r_date = r_created[:10] if r_created else ""
                    r_author = r.get("author", {}).get("emailAddress", "")
                    if not r_author:
                        r_author = r.get("author", {}).get("displayName", "Unknown")

                    replies.append(
                        CommentReply(
                            reply_id=r.get("id", ""),
                            author=r_author,
                            date=r_date,
                            content=r.get("content", ""),
                        )
                    )

                comments.append(
                    Comment(
                        comment_id=c.get("id", ""),
                        author=author,
                        date=date,
                        quoted_text=quoted,
                        content=c.get("content", ""),
                        resolved=c.get("resolved", False),
                        replies=replies,
                    )
                )

            page_token = result.get("nextPageToken")
            if not page_token:
                break

    return comments


def format_comment(comment: Comment) -> str:
    """Format a single comment as markdown."""
    lines = []

    # Main comment line
    resolved_tag = " [RESOLVED]" if comment.resolved else ""
    lines.append(
        f"* [{comment.comment_id}] {comment.date} - {comment.author}{resolved_tag}"
    )

    # Quoted context
    if comment.quoted_text:
        # Truncate long quotes
        quoted = comment.quoted_text
        if len(quoted) > 80:
            quoted = quoted[:77] + "..."
        lines.append(f'  > "{quoted}"')

    # Comment content
    for line in comment.content.split("\n"):
        lines.append(f"  {line}")

    # Replies
    for reply in comment.replies:
        lines.append(f"  ↳ [{reply.reply_id}] {reply.date} - {reply.author}")
        for line in reply.content.split("\n"):
            lines.append(f"    {line}")

    return "\n".join(lines)


def format_comments_section(
    comments: list[Comment],
    title: str,
    source: str,
    time_str: str,
    section_num: int,
    section_title: str,
) -> DocSection:
    """Format comments as a multipart section."""
    content_lines = []
    with operation("Formatting comments", total=len(comments)) as op:
        for comment in comments:
            logger.info(f"Formatting comment: {comment.comment_id}")
            content_lines.append(format_comment(comment))
            content_lines.append("")
            op.advance()

    return DocSection(
        title=title,
        source=source,
        time=time_str,
        section=section_num,
        section_type="comments",
        section_title=f"{section_title} (Comments)",
        content="\n".join(content_lines).strip(),
    )


def _add_comments_to_sections(
    sections: list[DocSection],
    document_id: str,
) -> list[DocSection]:
    """Fetch comments and interleave comment sections after each content section."""
    comments = fetch_comments(document_id)
    if not comments:
        return sections

    # For now, we don't have per-tab comment mapping from Drive API,
    # so we add all comments after the first section
    # (Google Docs comments are document-wide, not tab-specific)
    result = []
    first_section = sections[0]

    result.append(first_section)
    result.append(
        format_comments_section(
            comments=comments,
            title=first_section.title,
            source=first_section.source,
            time_str=first_section.time,
            section_num=first_section.section,
            section_title=first_section.section_title,
        )
    )

    # Add remaining content sections (if multi-tab)
    with operation("Adding sections", total=len(sections) - 1) as op:
        for section in sections[1:]:
            logger.info(f"Adding section: {section.section_title}")
            result.append(section)
            op.advance()

    return result


# =============================================================================
# Helpers shared by Tab and Doc
# =============================================================================


def _is_tree_file(path: Path) -> bool:
    """Check if a path is a tree YAML file (.doc.gax.yaml or .tab.gax.yaml)."""
    name = path.name.lower()
    return name.endswith(".doc.gax.yaml") or name.endswith(".tab.gax.yaml")


def _parse_tree_file(path: Path) -> dict:
    """Read a .doc.gax.yaml or .tab.gax.yaml file and return parsed dict.

    Returns dict with keys: source, kind, tab, body, appendix.
    """
    content = path.read_text(encoding="utf-8")
    from .tree import validated_parse
    return validated_parse(content)


def _safe_filename(name: str) -> str:
    """Sanitize a string for use as a filename."""
    safe = re.sub(r'[<>:"/\\|?*]', "-", name)
    return re.sub(r"\s+", "_", safe)


def _text_diff(
    remote: str, local: str, fromfile: str = "remote", tofile: str = "local"
) -> str | None:
    """Return a unified diff of two text strings, or None if identical."""
    diff_lines = list(
        difflib.unified_diff(
            remote.splitlines(keepends=True),
            local.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
            lineterm="",
        )
    )
    if not diff_lines:
        return None
    return "\n".join(line.rstrip("\n") for line in diff_lines)


def _parse_tab_file(path: Path) -> DocSection:
    """Read a .doc.gax.md or .tab.gax.md file and return its first section."""
    content = path.read_text(encoding="utf-8")
    sections = parse_multipart(content)
    if not sections:
        raise ValueError(f"No sections found in {path}")
    return sections[0]


def _compute_tab_paths(sections: list[DocSection], folder: Path) -> list[Path]:
    """Compute filesystem paths for sections based on tab nesting.

    A tab with children gets a subdirectory; its content goes inside.
    A leaf tab is a file in its parent's directory.

    Example layout:
        folder/
          Overview.doc.gax.md                    # leaf tab
          Design/Design.doc.gax.md               # parent tab content
          Design/Frontend.doc.gax.md             # child tab (leaf)
          Design/Backend/Backend.doc.gax.md      # nested parent
          Design/Backend/API.doc.gax.md           # grandchild
    """
    ancestor_stack: list[str] = []  # safe names at each depth
    paths = []

    for section in sections:
        if section.section_type == "comments":
            paths.append(Path(""))  # placeholder — skipped later
            continue

        safe = _safe_filename(section.section_title)
        depth = section.tab_depth

        # Trim ancestor stack to current depth
        ancestor_stack = ancestor_stack[:depth]

        if section.tab_has_children:
            # Parent tab: create subdir, content lives inside
            rel = Path(*ancestor_stack, safe) if ancestor_stack else Path(safe)
            file_path = folder / rel / f"{safe}.doc.gax.md"
            ancestor_stack.append(safe)
        else:
            # Leaf tab: file in current ancestor directory
            if ancestor_stack:
                file_path = folder / Path(*ancestor_stack) / f"{safe}.doc.gax.md"
            else:
                file_path = folder / f"{safe}.doc.gax.md"

        paths.append(file_path)

    return paths


def _walk_tab_files(folder: Path) -> list[Path]:
    """Recursively find all .doc.gax.md tab files in a checkout folder."""
    return sorted(folder.rglob("*.doc.gax.md"))


def _write_tab_file(
    section: DocSection,
    path: Path,
    *,
    comments: list[Comment] | None = None,
) -> None:
    """Serialize a DocSection to a .doc.gax.md file.

    If comments are provided, they are appended as a multipart comments section.
    Used by both Tab.clone() and Doc.clone() for consistent serialization.
    """
    if comments:
        sections = [
            section,
            format_comments_section(
                comments=comments,
                title=section.title,
                source=section.source,
                time_str=section.time,
                section_num=section.section,
                section_title=section.section_title,
            ),
        ]
        content = format_multipart(sections)
    else:
        content = format_section(section)
    path.write_text(content, encoding="utf-8")


def _read_checkout_metadata(path: Path) -> dict:
    """Read and validate .gax.yaml metadata from a checkout folder."""
    metadata_path = path / ".gax.yaml"
    if not metadata_path.exists():
        raise ValueError(f"No .gax.yaml found in {path}")

    with open(metadata_path) as f:
        metadata = yaml.safe_load(f)

    if not metadata.get("document_id") or not metadata.get("url"):
        raise ValueError("No document_id or url in .gax.yaml")

    return metadata


def _tab_name_from_filename(path: Path) -> str:
    """Derive a tab name from a filename, stripping gax suffixes."""
    name = path.name
    for suffix in (".tab.gax.md", ".doc.gax.md", ".md"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _known_tab_files(path: Path, metadata: dict) -> list[Path]:
    """Return tab file paths listed in .gax.yaml metadata.

    Falls back to recursive glob if no tabs list is present (legacy checkouts).
    """
    tabs = metadata.get("tabs")
    if not tabs:
        return _walk_tab_files(path)
    return [path / entry["path"] for entry in tabs if (path / entry["path"]).exists()]


# =============================================================================
# Tab(Resource) — single tab, single file
# =============================================================================


class Tab(Resource):
    """A single Google Docs tab (.doc.gax.md or .tab.gax.md file).

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "doc-tab"
    URL_PATTERN = r"docs\.google\.com/document/d/"
    FILE_TYPE = "gax/doc"
    FILE_EXTENSIONS = (".doc.gax.md", ".tab.gax.md", ".doc.gax.yaml", ".tab.gax.yaml")
    SCOPES = ("documents", "drive.readonly")

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Clone a single tab to a .doc.gax.md or .doc.gax.yaml file.

        Keyword args:
            tab_name: specific tab to clone (default: first tab)
            with_comments: include comments section
            quiet: suppress multi-tab hint
            fmt: "md" (default) or "tree" — output format
        """
        tab_name = kw.get("tab_name")
        with_comments = kw.get("with_comments", False)
        fmt = kw.get("fmt", "md")

        document_id = extract_doc_id(self.url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        # Fetch doc (needed for both formats)
        doc = _fetch_doc(document_id)
        doc_title = doc.get("title", "Untitled")
        flat = _flatten_tabs(doc.get("tabs", []))

        if not flat:
            raise ValueError("Document has no tabs")

        if tab_name:
            # Find matching tab
            matched = [(t, i) for t, i in flat if i.title == tab_name]
            if not matched:
                raise ValueError(f"Tab not found: {tab_name}")
            target_tab, target_info = matched[0]
        else:
            target_tab, target_info = flat[0]

        # --- Tree format ---
        if fmt == "tree":
            tree_yaml = _tab_content_to_tree_yaml(doc, target_tab, source_url)

            if output:
                file_path = output
            else:
                safe_name = _safe_filename(doc_title if not tab_name else tab_name)
                suffix = ".tab.gax.yaml" if tab_name else ".doc.gax.yaml"
                file_path = Path(f"{safe_name}{suffix}")

            if file_path.exists():
                raise ValueError(f"File already exists: {file_path}")

            file_path.write_text(tree_yaml, encoding="utf-8")
            return file_path

        # --- Markdown format (default) ---
        if tab_name:
            section = pull_single_tab(document_id, tab_name, source_url)
        else:
            content, baseline_hash = _tab_content_to_markdown(doc, target_tab)
            revision_id = doc.get("revisionId", "")

            time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            section = DocSection(
                title=doc_title,
                source=source_url,
                time=time_str,
                section=1,
                section_title=target_info.title,
                content=content,
                tab_depth=target_info.depth,
                tab_has_children=target_info.has_children,
                tab_id=target_info.id,
                baseline=baseline_hash,
                revision=revision_id,
            )

            # Warn about nested tabs
            has_nested = any(info.depth > 0 for _tab, info in flat)
            if not kw.get("quiet") and has_nested:
                logger.warning(
                    "Document has nested tabs. "
                    "Use 'gax doc checkout' for full structure."
                )

        if output:
            file_path = output
        else:
            safe_name = _safe_filename(section.title if not tab_name else tab_name)
            suffix = ".tab.gax.md" if tab_name else ".doc.gax.md"
            file_path = Path(f"{safe_name}{suffix}")

        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        comments = fetch_comments(document_id) if with_comments else None
        _write_tab_file(section, file_path, comments=comments)
        return file_path

    def checkout(self, output: Path | None = None, **kw) -> Path:
        """Checkout all tabs from this document URL into a folder.

        Delegates to Doc.checkout() — Tab URLs and Doc URLs are interchangeable
        for checkout purposes.
        """
        return Doc(url=self.url).checkout(output=output, **kw)

    def get(self, **kw) -> str:
        """Fetch current remote content for this tab. Read-only.

        Keyword args:
            json: if True, return raw Docs API JSON instead of markdown.
        """
        as_json = kw.get("json", False)

        # URL-constructed instance (no local file): fetch remote directly.
        if self.url and not self.path.is_file():
            document_id = extract_doc_id(self.url)
            doc = _fetch_doc(document_id)
            flat = _flatten_tabs(doc.get("tabs", []))
            if not flat:
                raise ValueError("Document has no tabs")
            tab_name = kw.get("tab")
            if tab_name:
                matched = [(t, i) for t, i in flat if i.title == tab_name]
                if not matched:
                    raise ValueError(f"Tab not found: {tab_name}")
                target_tab, target_info = matched[0]
            else:
                target_tab, target_info = flat[0]
            if as_json:
                return self._get_json(document_id, target_info.title)
            content, _hash = _tab_content_to_markdown(doc, target_tab)
            return content

        if _is_tree_file(self.path):
            return self._get_tree(as_json=as_json)

        section = _parse_tab_file(self.path)
        source_url = section.source
        if not source_url:
            raise ValueError("No source URL found in file")

        document_id = extract_doc_id(source_url)

        if as_json:
            return self._get_json(document_id, section.section_title)

        content = self.path.read_text(encoding="utf-8")
        sections = parse_multipart(content)

        if len(sections) == 1:
            tab_name = section.section_title
            remote = pull_single_tab(document_id, tab_name, source_url)
            return remote.content
        else:
            remote_sections = pull_doc(document_id, source_url)
            return "\n\n".join(s.content for s in remote_sections)

    def _get_tree(self, *, as_json: bool = False) -> str:
        """get() for tree YAML files."""
        tree_doc = _parse_tree_file(self.path)
        source_url = tree_doc.get("source", "")
        tab_name = tree_doc.get("tab", "")
        if not source_url:
            raise ValueError("No source URL found in tree file")

        document_id = extract_doc_id(source_url)

        if as_json:
            return self._get_json(document_id, tab_name)

        # Return tree YAML of current remote
        doc = _fetch_doc(document_id)
        flat = _flatten_tabs(doc.get("tabs", []))
        for tab, info in flat:
            if info.title == tab_name:
                return _tab_content_to_tree_yaml(doc, tab, source_url)

        raise ValueError(f"Tab '{tab_name}' not found in document")

    def _get_json(self, document_id: str, tab_name: str) -> str:
        """Return raw Docs API JSON for a single tab."""
        import json as json_mod

        doc = _fetch_doc(document_id)
        flat = _flatten_tabs(doc.get("tabs", []))

        for tab, info in flat:
            if info.title == tab_name:
                doc_tab = tab.get("documentTab", {})
                return json_mod.dumps(doc_tab, indent=2)

        raise ValueError(f"Tab '{tab_name}' not found in document")

    def pull(self, **kw) -> None:
        """Refresh a tab file from remote."""
        if _is_tree_file(self.path):
            return self._pull_tree(force=kw.get("force", False))

        with_comments = kw.get("with_comments", False)

        section = _parse_tab_file(self.path)
        source_url = section.source
        if not source_url:
            raise ValueError("No source URL found in file")

        document_id = extract_doc_id(source_url)

        # Check if this is a single-tab file or multipart
        content = self.path.read_text(encoding="utf-8")
        sections = parse_multipart(content)

        if len(sections) == 1:
            # Single tab — pull just that tab
            tab_name = section.section_title
            logger.info(f"Pulling tab: {tab_name}")
            new_section = pull_single_tab(document_id, tab_name, source_url)
            if with_comments:
                new_sections = _add_comments_to_sections([new_section], document_id)
                new_content = format_multipart(new_sections)
            else:
                new_content = format_section(new_section)
        else:
            # Multi-section file (legacy multipart) — pull all tabs
            logger.info(f"Pulling document: {document_id}")
            new_sections = pull_doc(document_id, source_url)
            if with_comments:
                new_sections = _add_comments_to_sections(new_sections, document_id)
            new_content = format_multipart(new_sections)

        self.path.write_text(new_content, encoding="utf-8")

    def _pull_tree(self, force: bool = False) -> None:
        """Pull for tree YAML files — re-serialize remote as tree YAML.

        When *force* is True, skip local schema validation and read only the
        ``source`` and ``tab`` keys from the raw YAML.  This is the recovery
        path for corrupt/invalid local files (gax-iuf).
        """
        if force:
            # Bypass validated_parse — read raw YAML to get routing keys only.
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            source_url = raw.get("source", "")
            tab_name = raw.get("tab", "")
        else:
            tree_doc = _parse_tree_file(self.path)
            source_url = tree_doc.get("source", "")
            tab_name = tree_doc.get("tab", "")
        if not source_url:
            raise ValueError("No source URL found in tree file")

        document_id = extract_doc_id(source_url)
        doc = _fetch_doc(document_id)
        flat = _flatten_tabs(doc.get("tabs", []))

        for tab, info in flat:
            if info.title == tab_name:
                new_yaml = _tab_content_to_tree_yaml(doc, tab, source_url)
                self.path.write_text(new_yaml, encoding="utf-8")
                logger.info(f"Pulled tree: {self.path.name}")
                return

        raise ValueError(f"Tab '{tab_name}' not found in document")

    def diff(self, **kw) -> str | None:
        """Preview changes between local tab(s) and remote.

        Handles both single-tab files and multipart files (multiple tabs in
        one .doc.gax.md). Returns unified diff string, or None if no changes.
        Accepts ``body`` kwarg (Path) to use an external file as the local
        content instead of the tracking file's content (single-tab only).
        """
        if _is_tree_file(self.path):
            return self._diff_tree()

        content = self.path.read_text(encoding="utf-8")
        local_sections = parse_multipart(content)
        if not local_sections:
            raise ValueError(f"No sections found in {self.path}")

        source_url = local_sections[0].source
        if not source_url:
            raise ValueError("No source URL found in file")

        document_id = extract_doc_id(source_url)

        if len(local_sections) == 1:
            # Single-tab file
            body: Path | None = kw.get("body", None)
            remote_section = pull_single_tab(document_id, local_sections[0].section_title, source_url)
            local_content = body.read_text(encoding="utf-8") if body else local_sections[0].content
            return _text_diff(
                remote_section.content,
                local_content,
                fromfile=f"remote/{local_sections[0].section_title}",
                tofile=str(self.path),
            )
        else:
            # Multipart file — diff each tab by name
            remote_sections = pull_doc(document_id, source_url)
            remote_by_name = {s.section_title: s for s in remote_sections}
            parts = []
            for local_s in local_sections:
                remote_s = remote_by_name.get(local_s.section_title)
                if remote_s is None:
                    parts.append(f"--- (not present remotely)\n+++ {self.path} [{local_s.section_title}]")
                    continue
                diff = _text_diff(
                    remote_s.content,
                    local_s.content,
                    fromfile=f"remote/{local_s.section_title}",
                    tofile=f"{self.path} [{local_s.section_title}]",
                )
                if diff:
                    parts.append(diff)
            return "\n\n".join(parts) or None

    def _diff_tree(self) -> str | None:
        """Diff for tree YAML files — text diff of tree YAML."""
        tree_doc = _parse_tree_file(self.path)
        source_url = tree_doc.get("source", "")
        tab_name = tree_doc.get("tab", "")
        if not source_url:
            raise ValueError("No source URL found in tree file")

        document_id = extract_doc_id(source_url)
        doc = _fetch_doc(document_id)
        flat = _flatten_tabs(doc.get("tabs", []))

        for tab, info in flat:
            if info.title == tab_name:
                remote_yaml = _tab_content_to_tree_yaml(doc, tab, source_url)
                local_yaml = self.path.read_text(encoding="utf-8")
                return _text_diff(
                    remote_yaml,
                    local_yaml,
                    fromfile=f"remote/{tab_name}",
                    tofile=str(self.path),
                )

        raise ValueError(f"Tab '{tab_name}' not found in document")

    def push(self, **kw) -> None:
        """Push local tab to remote.

        Keyword args:
            patch: use incremental AST-level push (experimental)
            body: Path — push this external file's content instead of the
                  tracking file's content; also updates the tracking file so
                  subsequent pull round-trips are consistent.
            force: for tree files, bypass the revision guard (gax-fuh).
        """
        if _is_tree_file(self.path):
            return self._push_tree(force=kw.get("force", False))

        use_patch = kw.get("patch", False)
        body: Path | None = kw.get("body", None)

        section = _parse_tab_file(self.path)
        source_url = section.source
        tab_name = section.section_title

        if not source_url:
            raise ValueError("No source URL found in file")

        document_id = extract_doc_id(source_url)

        if body is not None:
            raw_content = body.read_text(encoding="utf-8")
            content_to_push = inline_images_from_store(raw_content)
            # Update the tracking file so the tracking file stays in sync
            new_section = DocSection(
                title=section.title,
                source=source_url,
                time=section.time,
                section=section.section,
                section_title=tab_name,
                content=raw_content,
            )
            self.path.write_text(format_section(new_section), encoding="utf-8")
        else:
            content_to_push = inline_images_from_store(section.content)

        if use_patch:
            from .diff_push import diff_push as _diff_push

            logger.info(f"Patching tab '{tab_name}'...")
            _diff_push(document_id, tab_name, content_to_push)
        else:
            logger.info(f"Pushing to tab '{tab_name}'...")
            update_tab_content(document_id, tab_name, content_to_push)

        # Refresh baseline after successful push (ADR 034 §1)
        _refresh_baseline_after_push(self.path, document_id, tab_name)

    def _push_tree(self, *, force: bool = False) -> None:
        """Push for tree YAML files — compute_tree_plan + apply mutations.

        When *force* is True the revision guard is bypassed by clearing the
        stored revision before diffing.  This is the recovery path for corrupt
        remote state (gax-fuh): the plan still diffes remote vs local so only
        necessary mutations are applied, but a stale stored revision no longer
        blocks the push.
        """
        from .diff_push import compute_tree_plan

        if force:
            # Bypass validated_parse for potentially corrupt local files;
            # read only the routing keys from raw YAML (same as _pull_tree).
            import yaml as _yaml
            raw = _yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            source_url = raw.get("source", "")
            tab_name = raw.get("tab", "")
            stored_revision = ""  # bypass revision guard
            local_tree_body = raw.get("body", [])
            local_appendix = raw.get("appendix")
        else:
            tree_doc = _parse_tree_file(self.path)
            source_url = tree_doc.get("source", "")
            tab_name = tree_doc.get("tab", "")
            stored_revision = tree_doc.get("revision", "")
            local_tree_body = tree_doc.get("body", [])
            local_appendix = tree_doc.get("appendix")
        if not source_url:
            raise ValueError("No source URL found in tree file")

        document_id = extract_doc_id(source_url)
        doc = _fetch_doc(document_id)
        flat = _flatten_tabs(doc.get("tabs", []))
        remote_revision = doc.get("revisionId", "")

        matched_tab = None
        tab_id = ""
        for tab, info in flat:
            if info.title == tab_name:
                matched_tab = tab
                tab_id = info.id
                break

        if matched_tab is None:
            raise ValueError(f"Tab '{tab_name}' not found in document")

        doc_tab = matched_tab.get("documentTab", {})
        remote_body = doc_tab.get("body", {}).get("content", [])
        lists = doc_tab.get("lists") or doc.get("lists")

        plan = compute_tree_plan(
            local_tree_body=local_tree_body,
            local_appendix=local_appendix,
            remote_body=remote_body,
            remote_revision=remote_revision,
            stored_revision=stored_revision,
            tab_id=tab_id,
            lists=lists,
        )

        if plan.error:
            raise ValueError(f"Tree push failed: {plan.error}")

        if plan.is_empty:
            logger.info("No differences to push.")
            return

        # Apply mutations
        from ..auth import get_service
        service = get_service("docs", "v1")
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": plan.mutations},
        ).execute()

        logger.info(f"Tree push: {len(plan.mutations)} mutation(s) applied")

        # Post-push refresh: re-serialize from remote to update
        # revision stamp and baseline (like md _refresh_baseline_after_push)
        refreshed_doc = _fetch_doc(document_id)
        refreshed_flat = _flatten_tabs(refreshed_doc.get("tabs", []))
        for rtab, rinfo in refreshed_flat:
            if rinfo.title == tab_name:
                new_yaml = _tab_content_to_tree_yaml(
                    refreshed_doc, rtab, source_url,
                )
                self.path.write_text(new_yaml, encoding="utf-8")
                logger.info("Post-push tree refresh done")
                break


# =============================================================================
# Doc(Resource) — whole document, folder
# =============================================================================


class Doc(Resource):
    """A Google Docs document (.doc.gax.md.d/ folder).

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "doc"
    URL_PATTERN = r"docs\.google\.com/document/d/"
    CHECKOUT_TYPE = "gax/doc-checkout"
    HAS_GENERIC_DISPATCH = False
    SCOPES = ("documents", "drive.readonly")

    def clone(self, output: Path | None = None, **kw) -> Path:
        """Clone all tabs into a folder (supports nested tabs).

        Keyword args:
            with_comments: include document comments
            fmt: "md" (default) or "tree" — output format
        """
        with_comments = kw.get("with_comments", False)
        fmt = kw.get("fmt", "md")
        document_id = extract_doc_id(self.url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        logger.info(f"Fetching: {document_id}")

        # --- Tree format ---
        if fmt == "tree":
            return self._clone_tree(output, document_id, source_url)

        # --- Markdown format (default) ---
        sections = pull_doc(document_id, source_url)

        if not sections:
            raise ValueError("No sections found in document")

        comments = fetch_comments(document_id) if with_comments else None
        title = sections[0].title

        if output:
            folder = output
        else:
            folder = Path(f"{_safe_filename(title)}.doc.gax.md.d")

        folder.mkdir(parents=True, exist_ok=True)

        # Compute filesystem paths based on tab nesting
        tab_paths = _compute_tab_paths(sections, folder)

        # Build tab tree for .gax.yaml metadata
        tab_tree = []
        for section, fpath in zip(sections, tab_paths):
            tab_tree.append(
                {
                    "id": section.tab_id,
                    "title": section.section_title,
                    "path": str(fpath.relative_to(folder)),
                    "depth": section.tab_depth,
                }
            )

        metadata = write_sync_header(
            {
                "type": "gax/doc-checkout",
                "document_id": document_id,
                "url": source_url,
                "title": title,
                "tabs": tab_tree,
            }
        )
        metadata_path = folder / ".gax.yaml"
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        created = 0
        skipped = 0

        for i, (section, file_path) in enumerate(zip(sections, tab_paths)):
            if file_path.exists():
                skipped += 1
                continue

            file_path.parent.mkdir(parents=True, exist_ok=True)
            # Attach comments to the first tab file only
            tab_comments = comments if i == 0 else None
            _write_tab_file(section, file_path, comments=tab_comments)
            logger.info(f"Created: {file_path.relative_to(folder)}")
            created += 1

        logger.info(f"Checked out: {created}, Skipped: {skipped}")
        return folder

    def _clone_tree(
        self,
        output: Path | None,
        document_id: str,
        source_url: str,
    ) -> Path:
        """Clone all tabs as tree YAML into a .doc.gax.yaml.d/ folder."""
        doc = _fetch_doc(document_id)
        doc_title = doc.get("title", "Untitled")
        revision_id = doc.get("revisionId", "")
        flat = _flatten_tabs(doc.get("tabs", []))

        if not flat:
            raise ValueError("Document has no tabs")

        if output:
            folder = output
        else:
            folder = Path(f"{_safe_filename(doc_title)}.doc.gax.yaml.d")

        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata (same as markdown checkout)
        tab_tree = []
        for tab, info in flat:
            safe = _safe_filename(info.title)
            tab_tree.append({
                "id": info.id,
                "title": info.title,
                "path": f"{safe}.doc.gax.yaml",
                "depth": info.depth,
            })

        metadata = write_sync_header({
            "type": "gax/doc-checkout",
            "document_id": document_id,
            "url": source_url,
            "title": doc_title,
            "revision": revision_id,
            "tabs": tab_tree,
        })
        metadata_path = folder / ".gax.yaml"
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata, f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        created = 0
        for tab, info in flat:
            safe = _safe_filename(info.title)
            file_path = folder / f"{safe}.doc.gax.yaml"
            if file_path.exists():
                continue

            tree_yaml = _tab_content_to_tree_yaml(doc, tab, source_url)
            file_path.write_text(tree_yaml, encoding="utf-8")
            logger.info(f"Created: {file_path.name}")
            created += 1

        logger.info(f"Checked out {created} tab(s) as tree YAML")
        return folder

    def checkout(self, output: Path | None = None, **kw) -> Path:
        """Checkout all tabs into a folder."""
        return self.clone(output=output, **kw)

    def get(self, **kw) -> str:
        """Fetch all remote tabs and return content. Read-only."""
        metadata = _read_checkout_metadata(self.path)
        document_id = metadata["document_id"]
        url = metadata["url"]

        sections = pull_doc(document_id, url)

        tab_filter = kw.get("tab")
        if tab_filter:
            matches = [s for s in sections if s.section_title == tab_filter]
            if not matches:
                available = [s.section_title for s in sections]
                raise ValueError(
                    f"Tab '{tab_filter}' not found. Available: {', '.join(available)}"
                )
            sections = matches

        parts = []
        for section in sections:
            if len(sections) > 1:
                parts.append(f"# {section.section_title}\n")
            parts.append(section.content)
        return "\n\n".join(parts)

    def pull(self, **kw) -> None:
        """Pull all tabs in a checkout folder (supports nested tabs).

        With patch=True, broadcasts pull to each individual tab file
        instead of doing a single bulk fetch.
        """
        if kw.get("patch"):
            metadata = _read_checkout_metadata(self.path)
            for tab_file in _known_tab_files(self.path, metadata):
                Tab.from_file(tab_file).pull(**kw)
            return

        metadata = _read_checkout_metadata(self.path)
        metadata_path = self.path / ".gax.yaml"

        document_id = metadata["document_id"]
        url = metadata["url"]

        logger.info(f"Pulling: {document_id}")
        sections = pull_doc(document_id, url)

        # Compute filesystem paths based on tab nesting
        tab_paths = _compute_tab_paths(sections, self.path)

        # Build tab tree for metadata
        tab_tree = []
        for section, fpath in zip(sections, tab_paths):
            if section.section_type == "comments":
                continue
            tab_tree.append(
                {
                    "id": section.tab_id,
                    "title": section.section_title,
                    "path": str(fpath.relative_to(self.path)),
                    "depth": section.tab_depth,
                }
            )

        # Update metadata
        metadata = write_sync_header(metadata)
        metadata["title"] = sections[0].title if sections else metadata.get("title", "")
        metadata["tabs"] = tab_tree
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Write tab files
        remote_files = set()
        for section, file_path in zip(sections, tab_paths):
            if section.section_type == "comments":
                continue

            remote_files.add(file_path)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            content = format_section(section)
            file_path.write_text(content, encoding="utf-8")
            logger.info(f"Updated: {file_path.relative_to(self.path)}")

        # Clean up local files that no longer have a matching remote tab
        for stale in sorted(self.path.rglob("*")):
            if stale.is_dir():
                continue
            if stale.name == ".gax.yaml":
                continue
            if stale in remote_files:
                continue
            logger.warning(f"Removing (no matching remote tab): {stale.relative_to(self.path)}")
            stale.unlink()

        # Remove empty subdirectories left after cleanup
        for d in sorted(self.path.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                logger.warning(f"Removing empty directory: {d.relative_to(self.path)}")
                d.rmdir()

    def diff(self, **kw) -> str | None:
        """Diff all tabs in a checkout folder against remote.

        Also reports stale local files that would be removed on pull.
        """
        metadata = _read_checkout_metadata(self.path)

        all_diffs = []

        for tab_file in _known_tab_files(self.path, metadata):
            tab_diff = Tab.from_file(tab_file).diff()
            if tab_diff:
                all_diffs.append(tab_diff)

        # Check for stale local files
        known = set(_known_tab_files(self.path, metadata))
        stale = []
        for f in sorted(self.path.rglob("*")):
            if f.is_dir() or f.name == ".gax.yaml":
                continue
            if f not in known:
                stale.append(f)
        if stale:
            lines = ["Local-only files (new remote tabs on push; removed on pull):"]
            for f in stale:
                lines.append(f"  - {f.relative_to(self.path)}")
            all_diffs.append("\n".join(lines))

        return "\n\n".join(all_diffs) if all_diffs else None

    def push(self, **kw) -> None:
        """Push all changed tabs in a checkout folder.

        Local files not listed in the checkout metadata are created as
        new remote tabs (mirroring sheet push). The next pull rewrites
        them in canonical tracking-file form.
        """
        metadata = _read_checkout_metadata(self.path)

        for tab_file in _known_tab_files(self.path, metadata):
            t = Tab.from_file(tab_file)
            if t.diff() is not None:
                logger.info(f"Pushing: {tab_file.relative_to(self.path)}")
                t.push(**kw)

        # Create remote tabs for new local files
        document_id = metadata["document_id"]
        known = set(_known_tab_files(self.path, metadata))
        for f in sorted(self.path.rglob("*")):
            if f.is_dir() or f.name == ".gax.yaml" or f in known:
                continue
            content = f.read_text(encoding="utf-8")
            if content.startswith("---"):
                section = _parse_tab_file(f)
                tab_name = section.section_title or _tab_name_from_filename(f)
                body = section.content
            else:
                tab_name = _tab_name_from_filename(f)
                body = content
            logger.info(
                f"Creating new tab '{tab_name}' from {f.relative_to(self.path)}"
            )
            _tab_id, warnings = create_tab_with_content(document_id, tab_name, body)
            for w in warnings:
                logger.info(f"Warning: {w.feature}: {w.detail}")

    # Non-standard operations

    def tab_list(self, out) -> None:
        """Write tab listing to file descriptor."""
        document_id = extract_doc_id(self.url)
        info = get_tabs_list(document_id)

        out.write(f"# {info['title']}\n")
        out.write("index\tid\ttitle\n")
        for t in info["tabs"]:
            indent = "  " * t.depth
            out.write(f"{t.index}\t{t.id}\t{indent}{t.title}\n")

    def tab_import(self, file: Path, output: Path | None = None) -> Path:
        """Import a markdown file as a new tab in a document.

        Returns path to the tracking file created.
        """
        document_id = extract_doc_id(self.url)
        source_url = f"https://docs.google.com/document/d/{document_id}/edit"

        tab_name = file.stem
        content = file.read_text(encoding="utf-8")

        tracking_path = output or file.with_suffix(".tab.gax.md")
        if tracking_path.exists():
            raise ValueError(
                f"Tracking file already exists: {tracking_path}. "
                "Use 'gax doc tab push' to update an existing tab."
            )

        logger.info(f"Creating tab '{tab_name}' in {document_id}...")
        tab_id, warnings = create_tab_with_content(document_id, tab_name, content)
        for w in warnings:
            logger.info(f"Warning: {w.feature}: {w.detail}")
        logger.info(f"Created tab: {tab_id}")

        # Get document title for tracking file
        service = get_service("docs", "v1")
        doc = service.documents().get(documentId=document_id).execute()
        doc_title = doc.get("title", "Untitled")

        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        section = DocSection(
            title=doc_title,
            source=source_url,
            time=time_str,
            section=1,
            section_title=tab_name,
            content=content,
        )

        tracking_content = format_section(section)
        tracking_path.write_text(tracking_content, encoding="utf-8")
        return tracking_path


Resource.register(Tab)
Resource.register(Doc)
