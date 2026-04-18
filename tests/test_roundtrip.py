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
from gax.gdoc import create_tab_with_content
from gax.gdoc.native_md import export_tab_markdown


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

        m1 = export_tab_markdown(
            doc_id,
            tab_name,
            docs_service=services["docs"],
            drive_service=services["drive"],
            num_retries=NUM_RETRIES,
        )

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
