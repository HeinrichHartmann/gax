"""Round-trip tests for markdown <-> Google Docs conversion.

Two test classes:
1. TestPushVerify - push the fixture, inspect the Google Doc via API to
   verify styling (headings, bold, italic, links, bullets, tables).
2. TestIdentityRoundTrip - push the fixture, pull it back, assert M == M1
   (identity on canonical form).

Required environment variables:
    GAX_TEST_DOC - Google Doc ID for testing

Run with:
    pytest tests/test_roundtrip.py -v
"""

import difflib
import os
import time
from pathlib import Path

import pytest
from googleapiclient.discovery import build

from gax.auth import get_authenticated_credentials, is_authenticated
from gax.gdoc.doc import create_tab_with_content, pull_single_tab, update_tab_content


# =============================================================================
# Infrastructure
# =============================================================================

NUM_RETRIES = 3
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "e2e_rich_formatting.md"


def _get_test_doc_id() -> str:
    doc_id = os.environ.get("GAX_TEST_DOC")
    if not doc_id:
        pytest.skip("GAX_TEST_DOC not set")
    return doc_id


@pytest.fixture(scope="module")
def services():
    """Authenticated Google API services."""
    if not is_authenticated():
        pytest.skip("Not authenticated. Run 'gax auth login' first.")
    creds = get_authenticated_credentials()
    return {
        "docs": build("docs", "v1", credentials=creds),
        "drive": build("drive", "v3", credentials=creds),
    }


def _clear_doc_tabs(doc_id, docs_service):
    """Delete all tabs except the first one."""
    doc = (
        docs_service.documents()
        .get(documentId=doc_id, includeTabsContent=True)
        .execute(num_retries=NUM_RETRIES)
    )
    tabs = doc.get("tabs", [])
    requests = []
    for tab in tabs[1:]:
        tab_id = tab.get("tabProperties", {}).get("tabId", "")
        if tab_id:
            requests.append({"deleteTab": {"tabId": tab_id}})
    if requests:
        try:
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": requests},
            ).execute(num_retries=NUM_RETRIES)
        except Exception:
            pass


@pytest.fixture(scope="module")
def doc_id(services):
    """Get test doc ID; clean up all tabs before and after test module."""
    did = _get_test_doc_id()
    _clear_doc_tabs(did, services["docs"])
    yield did
    _clear_doc_tabs(did, services["docs"])


def _unique(prefix):
    return f"{prefix}_{int(time.time() * 1000) % 100000}"


# =============================================================================
# Shared: push fixture once, reuse across both test classes
# =============================================================================


@pytest.fixture(scope="module")
def pushed_fixture(doc_id, services):
    """Push the fixture to a new tab. Returns (tab_name, tab_id, fixture_md)."""
    md = FIXTURE_PATH.read_text()
    tab_name = _unique("rt_verify")
    tab_id, _warnings = create_tab_with_content(
        doc_id,
        tab_name,
        md,
        service=services["docs"],
        num_retries=NUM_RETRIES,
    )
    return tab_name, tab_id, md


# =============================================================================
# Test 1: Verify Google Doc styling via API
# =============================================================================


def _get_tab_body(doc, tab_id):
    """Extract body content elements for a tab."""
    for tab in doc.get("tabs", []):
        if tab["tabProperties"]["tabId"] == tab_id:
            return tab["documentTab"]["body"]["content"]
    raise ValueError(f"Tab {tab_id} not found")


def _collect_paragraphs(body_content):
    """Extract all paragraphs with their text and style info."""
    paragraphs = []
    for elem in body_content:
        if "paragraph" not in elem:
            continue
        para = elem["paragraph"]
        style = para.get("paragraphStyle", {})
        named_style = style.get("namedStyleType", "NORMAL_TEXT")
        bullet = para.get("bullet")

        runs = []
        for e in para.get("elements", []):
            if "textRun" not in e:
                continue
            tr = e["textRun"]
            ts = tr.get("textStyle", {})
            runs.append(
                {
                    "text": tr["content"],
                    "bold": ts.get("bold", False),
                    "italic": ts.get("italic", False),
                    "link": ts.get("link", {}).get("url"),
                }
            )

        full_text = "".join(r["text"] for r in runs).strip()
        paragraphs.append(
            {
                "text": full_text,
                "named_style": named_style,
                "bullet": bullet,
                "runs": runs,
            }
        )
    return paragraphs


def _collect_tables(body_content):
    """Extract all tables with their cell text."""
    tables = []
    for elem in body_content:
        if "table" not in elem:
            continue
        table = elem["table"]
        rows = []
        for row in table.get("tableRows", []):
            cells = []
            for cell in row.get("tableCells", []):
                cell_text = ""
                cell_runs = []
                for content_elem in cell.get("content", []):
                    if "paragraph" in content_elem:
                        for e in content_elem["paragraph"].get("elements", []):
                            if "textRun" in e:
                                tr = e["textRun"]
                                ts = tr.get("textStyle", {})
                                cell_text += tr["content"]
                                cell_runs.append(
                                    {
                                        "text": tr["content"],
                                        "bold": ts.get("bold", False),
                                        "italic": ts.get("italic", False),
                                    }
                                )
                cells.append({"text": cell_text.strip(), "runs": cell_runs})
            rows.append(cells)
        tables.append(
            {
                "rows": rows,
                "num_rows": len(rows),
                "num_cols": len(rows[0]) if rows else 0,
            }
        )
    return tables


@pytest.mark.e2e
class TestPushVerify:
    """Push the fixture and verify styling in the Google Doc via API."""

    @pytest.fixture(scope="class")
    def doc_structure(self, doc_id, services, pushed_fixture):
        """Read the pushed document structure once for all assertions."""
        _, tab_id, _ = pushed_fixture
        doc = (
            services["docs"]
            .documents()
            .get(documentId=doc_id, includeTabsContent=True)
            .execute(num_retries=NUM_RETRIES)
        )
        body = _get_tab_body(doc, tab_id)
        return {
            "paragraphs": _collect_paragraphs(body),
            "tables": _collect_tables(body),
        }

    def _find_para(self, doc_structure, text_substring):
        """Find a paragraph containing the given text."""
        for p in doc_structure["paragraphs"]:
            if text_substring in p["text"]:
                return p
        raise AssertionError(f"Paragraph containing {text_substring!r} not found")

    def _find_run(self, para, text_substring):
        """Find a text run containing the given text."""
        for r in para["runs"]:
            if text_substring in r["text"]:
                return r
        raise AssertionError(
            f"Run containing {text_substring!r} not found in {para['text']!r}"
        )

    # --- Headings ---

    def test_h1_heading(self, doc_structure):
        p = self._find_para(doc_structure, "Markdown Round-Trip Test Fixture")
        assert p["named_style"] == "HEADING_1"

    def test_h2_heading(self, doc_structure):
        p = self._find_para(doc_structure, "Headings")
        # There are multiple H2s; just check the first "Headings" section header
        assert p["named_style"] == "HEADING_2"

    def test_h3_heading(self, doc_structure):
        p = self._find_para(doc_structure, "H3 Heading")
        assert p["named_style"] == "HEADING_3"

    def test_h4_heading(self, doc_structure):
        p = self._find_para(doc_structure, "H4 Heading")
        assert p["named_style"] == "HEADING_4"

    def test_h5_heading(self, doc_structure):
        p = self._find_para(doc_structure, "H5 Heading")
        assert p["named_style"] == "HEADING_5"

    def test_h6_heading(self, doc_structure):
        p = self._find_para(doc_structure, "H6 Heading")
        assert p["named_style"] == "HEADING_6"

    # --- Bold / Italic ---

    def test_bold_text(self, doc_structure):
        p = self._find_para(doc_structure, "This has bold text")
        r = self._find_run(p, "bold")
        assert r["bold"] is True

    def test_italic_text(self, doc_structure):
        p = self._find_para(doc_structure, "This has italic text")
        r = self._find_run(p, "italic")
        assert r["italic"] is True

    def test_bold_italic_text(self, doc_structure):
        p = self._find_para(doc_structure, "bold italic")
        r = self._find_run(p, "bold italic")
        assert r["bold"] is True
        assert r["italic"] is True

    # --- Links ---

    def test_hyperlink(self, doc_structure):
        p = self._find_para(doc_structure, "Visit")
        r = self._find_run(p, "Google")
        assert r["link"] == "https://www.google.com"

    def test_multiple_links(self, doc_structure):
        p = self._find_para(doc_structure, "links inline")
        r1 = self._find_run(p, "multiple")
        assert r1["link"] == "https://example.com"
        r2 = self._find_run(p, "inline")
        assert r2["link"] == "https://example.org"

    # --- Lists ---

    def test_unordered_list_has_bullets(self, doc_structure):
        # Filter to the one that's a bullet (not ordered)
        items = [
            p
            for p in doc_structure["paragraphs"]
            if "First item" in p["text"] and p["bullet"] is not None
        ]
        assert len(items) >= 1, "No bulleted 'First item' found"

    def test_ordered_list_has_bullets(self, doc_structure):
        # Ordered lists also get bullet property in Google Docs
        items = [
            p for p in doc_structure["paragraphs"] if "Ordered after table" in p["text"]
        ]
        assert len(items) == 1
        assert items[0]["bullet"] is not None

    def test_list_with_bold_formatting(self, doc_structure):
        p = self._find_para(doc_structure, "Bold item")
        r = self._find_run(p, "Bold item")
        assert r["bold"] is True

    # --- Tables ---

    def test_simple_table_structure(self, doc_structure):
        tables = doc_structure["tables"]
        # Find the simple 2-col table with Alpha/Beta
        simple = [
            t
            for t in tables
            if any(any(c["text"] == "Alpha" for c in row) for row in t["rows"])
        ]
        assert len(simple) == 1
        t = simple[0]
        assert t["num_rows"] == 3  # header + 2 data rows
        assert t["num_cols"] == 2

    def test_table_with_bold_cells(self, doc_structure):
        tables = doc_structure["tables"]
        bold_table = [
            t
            for t in tables
            if any(any(c["text"] == "Setup" for c in row) for row in t["rows"])
        ]
        assert len(bold_table) >= 1
        t = bold_table[0]
        # Find the "Setup" cell and check it has bold
        for row in t["rows"]:
            for cell in row:
                if cell["text"] == "Setup":
                    assert any(r["bold"] for r in cell["runs"]), (
                        "Setup cell should be bold"
                    )

    def test_wide_table(self, doc_structure):
        tables = doc_structure["tables"]
        wide = [t for t in tables if t["num_cols"] == 8]
        assert len(wide) == 1

    def test_table_with_emoji(self, doc_structure):
        tables = doc_structure["tables"]
        emoji_table = [
            t
            for t in tables
            if any(any("🟢" in c["text"] for c in row) for row in t["rows"])
        ]
        assert len(emoji_table) == 1

    def test_table_count(self, doc_structure):
        # Simple, Bold, Minimal, Wide, Emoji, Empty Cells, plus Mixed (X/Y)
        assert len(doc_structure["tables"]) == 7


# =============================================================================
# Test 2: Identity round-trip
# =============================================================================


@pytest.mark.e2e
class TestIdentityRoundTrip:
    """Push the fixture, pull it back, assert M == M1 (identity)."""

    def test_identity(self, doc_id, services, pushed_fixture):
        tab_name, _, md = pushed_fixture

        section = pull_single_tab(
            doc_id,
            tab_name,
            f"https://docs.google.com/document/d/{doc_id}/edit",
            docs_service=services["docs"],
            num_retries=NUM_RETRIES,
        )
        m1 = section.content

        if md != m1:
            d = "".join(
                difflib.unified_diff(
                    md.splitlines(keepends=True),
                    m1.splitlines(keepends=True),
                    fromfile="fixture",
                    tofile="pulled",
                )
            )
            assert False, f"Identity failed (M != M1):\n{d}"


# =============================================================================
# Test 3: Push edits and verify they are reflected
# =============================================================================


def _apply_fixture_edits(md: str) -> str:
    """Apply three complex edits to the fixture markdown.

    Edit 1 – Simple Table: rename Alpha→**Gamma** (adds bold), change value
              100→999, append new row Delta|300.
    Edit 2 – Table With Bold: append row with ***Testing***|3 (bold+italic cell).
    Edit 3 – Mixed Structures paragraph: rewrite with inline **bold**.
    """
    # Edit 1: Simple Table – content, formatting, and structure change
    md = md.replace(
        "| Alpha | 100 |\n| Beta | 200 |",
        "| **Gamma** | 999 |\n| Beta | 200 |\n| Delta | 300 |",
    )
    # Edit 2: Table With Bold – add bold+italic row
    md = md.replace(
        "| **Deploy** | 4 |",
        "| **Deploy** | 4 |\n| ***Testing*** | 3 |",
    )
    # Edit 3: paragraph near tables – rewrite with bold formatting
    md = md.replace(
        "Text before a list.",
        "Edited paragraph with **bold** before a list.",
    )
    return md


@pytest.mark.e2e
class TestPushEdits:
    """Push fixture, apply complex edits, push again, verify via API + round-trip.

    All edits are pushed in a single update_tab_content call to minimise
    API usage.  Assertions are split into small test methods that share
    a single API read (class-scoped doc_structure fixture).
    """

    @pytest.fixture(scope="class")
    def edited_tab(self, doc_id, services):
        """Create tab with fixture, push edited version, return state."""
        md = FIXTURE_PATH.read_text()
        tab_name = _unique("rt_edit")

        # Step 1: create tab with original fixture
        tab_id, _ = create_tab_with_content(
            doc_id,
            tab_name,
            md,
            service=services["docs"],
            num_retries=NUM_RETRIES,
        )

        # Step 2: apply edits and push
        edited_md = _apply_fixture_edits(md)
        update_tab_content(
            doc_id,
            tab_name,
            edited_md,
            service=services["docs"],
        )

        return tab_name, tab_id, edited_md

    @pytest.fixture(scope="class")
    def doc_structure(self, doc_id, services, edited_tab):
        """Read pushed document structure once for all assertions."""
        _, tab_id, _ = edited_tab
        doc = (
            services["docs"]
            .documents()
            .get(documentId=doc_id, includeTabsContent=True)
            .execute(num_retries=NUM_RETRIES)
        )
        body = _get_tab_body(doc, tab_id)
        return {
            "paragraphs": _collect_paragraphs(body),
            "tables": _collect_tables(body),
        }

    # --- Edit 1: Simple Table ---

    def test_simple_table_row_added(self, doc_structure):
        """Simple table grew from 3 to 4 rows (header + Alpha→Gamma, Beta, Delta)."""
        tables = doc_structure["tables"]
        t = _find_table(tables, "Name")
        assert t["num_rows"] == 4
        assert t["num_cols"] == 2

    def test_simple_table_gamma_bold(self, doc_structure):
        """Alpha was replaced by Gamma with bold formatting."""
        tables = doc_structure["tables"]
        t = _find_table(tables, "Name")
        cell = _find_cell(t, "Gamma")
        assert any(
            r["bold"] for r in cell["runs"]
        ), "Gamma should be bold"

    def test_simple_table_new_values(self, doc_structure):
        """Value column has 999 (changed) and 300 (new row)."""
        tables = doc_structure["tables"]
        t = _find_table(tables, "Name")
        all_text = {c["text"] for row in t["rows"] for c in row}
        assert "999" in all_text, "Value 999 missing"
        assert "300" in all_text, "Value 300 (Delta row) missing"
        assert "100" not in all_text, "Old value 100 should be gone"

    # --- Edit 2: Table With Bold ---

    def test_bold_table_row_added(self, doc_structure):
        """Bold table grew from 3 to 4 rows."""
        tables = doc_structure["tables"]
        t = _find_table(tables, "Category")
        assert t["num_rows"] == 4

    def test_bold_table_testing_bold_italic(self, doc_structure):
        """New Testing cell has both bold and italic."""
        tables = doc_structure["tables"]
        t = _find_table(tables, "Category")
        cell = _find_cell(t, "Testing")
        assert any(
            r["bold"] and r["italic"] for r in cell["runs"]
        ), "Testing cell should be bold+italic"

    # --- Edit 3: Paragraph near tables ---

    def test_paragraph_text_changed(self, doc_structure):
        """Paragraph rewritten with new text and bold span."""
        paras = doc_structure["paragraphs"]
        edited = [p for p in paras if "Edited paragraph" in p["text"]]
        assert len(edited) == 1, "Edited paragraph not found"
        bold_runs = [r for r in edited[0]["runs"] if r.get("bold")]
        assert any("bold" in r["text"] for r in bold_runs), (
            "Bold formatting missing in edited paragraph"
        )

    def test_original_text_gone(self, doc_structure):
        """Original paragraph text should not appear."""
        paras = doc_structure["paragraphs"]
        assert not any(
            "Text before a list." == p["text"] for p in paras
        ), "Original paragraph should have been replaced"

    # --- Round-trip identity ---

    def test_roundtrip_identity(self, doc_id, services, edited_tab):
        """Edited markdown survives push→pull round-trip unchanged."""
        tab_name, _, edited_md = edited_tab

        section = pull_single_tab(
            doc_id,
            tab_name,
            f"https://docs.google.com/document/d/{doc_id}/edit",
            docs_service=services["docs"],
            num_retries=NUM_RETRIES,
        )
        pulled = section.content

        if edited_md != pulled:
            d = "".join(
                difflib.unified_diff(
                    edited_md.splitlines(keepends=True),
                    pulled.splitlines(keepends=True),
                    fromfile="edited",
                    tofile="pulled",
                )
            )
            assert False, f"Push-edit round-trip failed:\n{d}"


# =============================================================================
# Helpers
# =============================================================================


def _find_table(tables, header_text):
    """Find a table whose first row contains a cell matching *header_text*."""
    for t in tables:
        if t["rows"] and any(c["text"] == header_text for c in t["rows"][0]):
            return t
    raise AssertionError(
        f"Table with header {header_text!r} not found among "
        f"{len(tables)} tables"
    )


def _find_cell(table, cell_text):
    """Find a cell by its stripped text content."""
    for row in table["rows"]:
        for cell in row:
            if cell["text"] == cell_text:
                return cell
    raise AssertionError(
        f"Cell {cell_text!r} not found in table"
    )


# =============================================================================
# Test 4: Inline images and unsupported elements
# =============================================================================

# A small 1x1 red PNG for testing (publicly accessible)
TEST_IMAGE_URL = "https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png"


@pytest.mark.e2e
class TestInlineElements:
    """Insert inline images via API, pull, verify they appear in markdown."""

    @pytest.fixture(scope="class")
    def tab_with_image(self, doc_id, services):
        """Create a tab, insert text + inline image via API."""
        tab_name = _unique("rt_img")
        docs = services["docs"]

        # Step 1: Create tab with initial text
        tab_id, _ = create_tab_with_content(
            doc_id,
            tab_name,
            "# Image Test\n\nBefore image.\n\nAfter image.\n",
            service=docs,
            num_retries=NUM_RETRIES,
        )

        # Step 2: Re-read to find insertion point (after "Before image.\n")
        doc = docs.documents().get(
            documentId=doc_id, includeTabsContent=True
        ).execute(num_retries=NUM_RETRIES)

        body = _get_tab_body(doc, tab_id)
        insert_index = None
        for elem in body:
            if "paragraph" in elem:
                for e in elem["paragraph"].get("elements", []):
                    tr = e.get("textRun", {})
                    if "Before image." in tr.get("content", ""):
                        insert_index = e["endIndex"]

        assert insert_index is not None, "Could not find insertion point"

        # Step 3: Insert inline image
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {
                        "insertInlineImage": {
                            "uri": TEST_IMAGE_URL,
                            "location": {
                                "index": insert_index,
                                "tabId": tab_id,
                            },
                            "objectSize": {
                                "width": {"magnitude": 100, "unit": "PT"},
                                "height": {"magnitude": 34, "unit": "PT"},
                            },
                        }
                    }
                ]
            },
        ).execute(num_retries=NUM_RETRIES)

        return tab_name, tab_id

    def test_pull_contains_image_or_warning(self, doc_id, tab_with_image):
        """Pull tab with inline image — should either render it or warn."""
        import logging

        tab_name, tab_id = tab_with_image

        # Capture warnings from the pull
        warnings = []
        handler = logging.Handler()
        handler.emit = lambda record: warnings.append(record.getMessage())
        handler.setLevel(logging.WARNING)
        ir_logger = logging.getLogger("gax.gdoc.ir")
        ir_logger.addHandler(handler)

        try:
            source_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            section = pull_single_tab(doc_id, tab_name, source_url)
        finally:
            ir_logger.removeHandler(handler)

        md = section.content

        # Image should appear as ![...](url) in markdown
        assert "![" in md, (
            f"Expected inline image in markdown.\n"
            f"Markdown:\n{md}\n"
            f"Warnings: {warnings}"
        )

        # No inlineObjectElement should be skipped
        assert not any("inlineObjectElement" in w for w in warnings), (
            f"inlineObjectElement should not be skipped: {warnings}"
        )

        # Verify the rest of the content is intact
        assert "Image Test" in md
        assert "Before image." in md
        assert "After image." in md
