"""Round-trip stability tests for markdown <-> Google Docs conversion.

Tests the projection property: pull(push(pull(push(M)))) == pull(push(M))
(applying the push/pull cycle twice yields the same result as once).

For markdown already in canonical form (the output of a push/pull cycle),
the identity property holds: pull(push(M)) == M.

Required environment variables:
    GAX_TEST_DOC - Google Doc ID for testing

Run with: make test-e2e

Fixtures are organized into tiers to stay within Google Docs API quota
(60 write requests/min). Run individual tiers with:
    pytest -m tier1    # ~26 writes: text basics
    pytest -m tier2    # ~22 writes: lists and code
    pytest -m tier3    # ~50 writes: tables and complex docs
"""

import difflib
import os
import time

import pytest
from googleapiclient.discovery import build

from gax.auth import get_authenticated_credentials, is_authenticated
from gax.gdoc import create_tab_with_content, _populate_tables
from gax.md2docs import extract_tables
from gax.native_md import export_tab_markdown


# =============================================================================
# Infrastructure
# =============================================================================

NUM_RETRIES = 3  # Exponential backoff on 429/5xx


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
    """Delete all tabs except the first one (single batched call)."""
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute(num_retries=NUM_RETRIES)
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


# =============================================================================
# Primitives
# =============================================================================


def _unique(prefix):
    """Unique tab name to avoid collisions."""
    return f"{prefix}_{int(time.time() * 1000) % 100000}"


def push_md(doc_id, tab_name, markdown, docs_service):
    """Push markdown to a new tab, including table population."""
    tab_id = create_tab_with_content(
        doc_id, tab_name, markdown, service=docs_service,
        num_retries=NUM_RETRIES,
    )
    tables_data = extract_tables(markdown)
    if tables_data:
        _populate_tables(docs_service, doc_id, tab_id, tables_data,
                         num_retries=NUM_RETRIES)
    return tab_id


def pull_md(doc_id, tab_name, docs_service, drive_service):
    """Pull a tab back as markdown."""
    return export_tab_markdown(
        doc_id, tab_name,
        docs_service=docs_service,
        drive_service=drive_service,
        num_retries=NUM_RETRIES,
    )


def diff(a, b, name_a="expected", name_b="actual"):
    """Unified diff between two strings. Returns empty string if identical."""
    return "".join(difflib.unified_diff(
        a.splitlines(keepends=True),
        b.splitlines(keepends=True),
        fromfile=name_a,
        tofile=name_b,
    ))


def assert_stable(md, doc_id, services, prefix="rt"):
    """Push -> pull -> push -> pull. Assert M1 == M2 (idempotency).

    Also reports the projection diff (M vs M1) for diagnostics.
    Returns (m1, m2) for further inspection.
    """
    docs = services["docs"]
    drive = services["drive"]

    name1 = _unique(f"{prefix}_c1")
    name2 = _unique(f"{prefix}_c2")

    # Cycle 1: push M, pull back M1
    push_md(doc_id, name1, md, docs)
    m1 = pull_md(doc_id, name1, docs, drive)

    # Report projection diff (informational)
    proj_diff = diff(md, m1, "original", "projected")
    if proj_diff:
        print(f"\n--- Projection diff for '{prefix}' ---")
        print(proj_diff)
        print("--- end ---")

    # Cycle 2: push M1, pull back M2
    push_md(doc_id, name2, m1, docs)
    m2 = pull_md(doc_id, name2, docs, drive)

    d = diff(m1, m2, "cycle1", "cycle2")
    assert m1 == m2, f"Not stable after second cycle:\n{d}"

    return m1, m2


def assert_identity(md, doc_id, services, prefix="id"):
    """Push -> pull. Assert M == M1 (identity on canonical form).

    The input md must already be in canonical form (output of a previous
    push/pull cycle). Returns m1.
    """
    docs = services["docs"]
    drive = services["drive"]

    name = _unique(f"{prefix}")

    push_md(doc_id, name, md, docs)
    m1 = pull_md(doc_id, name, docs, drive)

    d = diff(md, m1, "original", "pulled")
    assert md == m1, f"Not identity:\n{d}"

    return m1


# =============================================================================
# Progressive fixtures, organized by tier
# =============================================================================

# Tier 1: Text basics (~26 writes)
TIER1_FIXTURES = {
    "single_paragraph": "Hello world.\n",

    "two_paragraphs": "First paragraph.\n\nSecond paragraph.\n",

    "heading_and_paragraph": "# Title\n\nSome body text.\n",

    "multiple_headings": (
        "# Heading 1\n\n"
        "Text under h1.\n\n"
        "## Heading 2\n\n"
        "Text under h2.\n\n"
        "### Heading 3\n\n"
        "Text under h3.\n"
    ),

    "bold_and_italic": (
        "This has **bold** text.\n\n"
        "This has *italic* text.\n\n"
        "This has ***both*** styles.\n"
    ),

    "mixed_inline": (
        "A paragraph with **multiple** bold **words** and *italic* too.\n"
    ),
}

# Tier 2: Lists and code (~22 writes)
TIER2_FIXTURES = {
    "unordered_list": (
        "Before the list.\n\n"
        "- First item\n"
        "- Second item\n"
        "- Third item\n\n"
        "After the list.\n"
    ),

    "ordered_list": (
        "Before the list.\n\n"
        "1. First item\n"
        "1. Second item\n"
        "1. Third item\n\n"
        "After the list.\n"
    ),

    "list_with_formatting": (
        "- **Bold item** with text\n"
        "- *Italic item* with text\n"
        "- Plain item\n"
    ),

    "code_block": (
        "Text before.\n\n"
        "```\n"
        "def hello():\n"
        "    print(\"world\")\n"
        "```\n\n"
        "Text after.\n"
    ),  # Note: code fences are projected to "> " prefixed lines (see md2docs.py)

    "heading_list_paragraph": (
        "# Section\n\n"
        "Some intro text.\n\n"
        "- Point A\n"
        "- Point B\n\n"
        "Closing text.\n"
    ),
}

# Tier 3: Tables and complex documents (~50 writes)
TIER3_FIXTURES = {
    "simple_table": (
        "## Data\n\n"
        "| Name | Value |\n"
        "| :---- | :---- |\n"
        "| Alpha | 100 |\n"
        "| Beta | 200 |\n"
    ),

    "table_with_bold": (
        "| Category | Score |\n"
        "| :---- | :---- |\n"
        "| **Setup** | 5 |\n"
        "| **Deploy** | 4 |\n"
    ),

    "mixed_document": (
        "# Report\n\n"
        "**Author:** Test\n\n"
        "## Analysis\n\n"
        "Two components:\n\n"
        "1. **Compute** - standard pricing\n"
        "1. **Service** - additional fees\n\n"
        "## Scores\n\n"
        "- **5 - Seamless:** no friction\n"
        "- **3 - Moderate:** some effort\n\n"
        "| Metric | Value |\n"
        "| :---- | :---- |\n"
        "| **Cost** | $100 |\n"
        "| **Time** | 2 days | \n\n"
        "Final notes. \n"
    ),
}

# Combined for backwards compat
FIXTURES = {**TIER1_FIXTURES, **TIER2_FIXTURES, **TIER3_FIXTURES}


# =============================================================================
# Stability tests by tier
# =============================================================================


@pytest.mark.e2e
@pytest.mark.tier1
class TestStabilityTier1:
    """Text basics: paragraphs, headings, bold/italic."""

    @pytest.mark.parametrize("name", TIER1_FIXTURES.keys())
    def test_fixture(self, name, doc_id, services):
        md = TIER1_FIXTURES[name]
        assert_stable(md, doc_id, services, prefix=name)


@pytest.mark.e2e
@pytest.mark.tier2
class TestStabilityTier2:
    """Lists and code blocks."""

    @pytest.mark.parametrize("name", TIER2_FIXTURES.keys())
    def test_fixture(self, name, doc_id, services):
        md = TIER2_FIXTURES[name]
        assert_stable(md, doc_id, services, prefix=name)


@pytest.mark.e2e
@pytest.mark.tier3
class TestStabilityTier3:
    """Tables and complex mixed documents."""

    @pytest.mark.parametrize("name", TIER3_FIXTURES.keys())
    def test_fixture(self, name, doc_id, services):
        md = TIER3_FIXTURES[name]
        assert_stable(md, doc_id, services, prefix=name)


@pytest.mark.e2e
@pytest.mark.tier3
class TestComplexIdempotency:
    """Test that the full rich formatting fixture stabilizes."""

    def test_rich_fixture(self, doc_id, services):
        from pathlib import Path
        fixture = Path(__file__).parent / "fixtures" / "e2e_rich_formatting.md"
        md = fixture.read_text()
        m1, _ = assert_stable(md, doc_id, services, prefix="rich")
        # Verify key content survived
        assert "Project Evaluation Report" in m1
        assert "Cost Analysis" in m1
        assert "Integration Scores" in m1
