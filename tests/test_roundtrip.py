"""Round-trip stability tests for markdown <-> Google Docs conversion.

Tests the projection property: pull(push(pull(push(M)))) == pull(push(M))
(applying the push/pull cycle twice yields the same result as once).

For markdown already in canonical form (the output of a push/pull cycle),
the identity property holds: pull(push(M)) == M.

Required environment variables:
    GAX_TEST_DOC - Google Doc ID for testing

Run with: make test-e2e
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


@pytest.fixture(scope="module")
def doc_id():
    return _get_test_doc_id()


# =============================================================================
# Primitives
# =============================================================================


def _unique(prefix):
    """Unique tab name to avoid collisions."""
    return f"{prefix}_{int(time.time() * 1000) % 100000}"


def push_md(doc_id, tab_name, markdown, docs_service):
    """Push markdown to a new tab, including table population."""
    tab_id = create_tab_with_content(
        doc_id, tab_name, markdown, service=docs_service
    )
    tables_data = extract_tables(markdown)
    if tables_data:
        _populate_tables(docs_service, doc_id, tab_id, tables_data)
    return tab_id


def pull_md(doc_id, tab_name, docs_service, drive_service):
    """Pull a tab back as markdown."""
    return export_tab_markdown(
        doc_id, tab_name,
        docs_service=docs_service,
        drive_service=drive_service,
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

    Returns (m1, m2) for further inspection.
    """
    docs = services["docs"]
    drive = services["drive"]

    name1 = _unique(f"{prefix}_c1")
    name2 = _unique(f"{prefix}_c2")

    # Cycle 1: push M, pull back M1
    push_md(doc_id, name1, md, docs)
    m1 = pull_md(doc_id, name1, docs, drive)

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
# Progressive fixtures
# =============================================================================

# Fixtures are markdown documents of increasing complexity.
# We test stability (idempotency) for all of them.
# As we discover the canonical form, we can promote fixtures to identity tests.

FIXTURES = {
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
        "2. Second item\n"
        "3. Third item\n\n"
        "After the list.\n"
    ),

    "list_with_formatting": (
        "- **Bold item** with text\n"
        "- *Italic item* with text\n"
        "- Plain item\n"
    ),

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

    "code_block": (
        "Text before.\n\n"
        "```\n"
        "def hello():\n"
        "    print(\"world\")\n"
        "```\n\n"
        "Text after.\n"
    ),

    "heading_list_paragraph": (
        "# Section\n\n"
        "Some intro text.\n\n"
        "- Point A\n"
        "- Point B\n\n"
        "Closing text.\n"
    ),

    "mixed_document": (
        "# Report\n\n"
        "**Author:** Test\n\n"
        "## Analysis\n\n"
        "Two components:\n\n"
        "1. **Compute** - standard pricing\n"
        "2. **Service** - additional fees\n\n"
        "## Scores\n\n"
        "- **5 - Seamless:** no friction\n"
        "- **3 - Moderate:** some effort\n\n"
        "| Metric | Value |\n"
        "| :---- | :---- |\n"
        "| **Cost** | $100 |\n"
        "| **Time** | 2 days |\n\n"
        "Final notes.\n"
    ),
}


# =============================================================================
# Stability tests (idempotency): M1 == M2
# =============================================================================


@pytest.mark.e2e
class TestStability:
    """Test that push/pull is stable after first projection.

    For each fixture: push(M) -> pull -> M1 -> push(M1) -> pull -> M2.
    Assert M1 == M2.
    """

    @pytest.mark.parametrize("name", FIXTURES.keys())
    def test_fixture(self, name, doc_id, services):
        md = FIXTURES[name]
        assert_stable(md, doc_id, services, prefix=name)


# =============================================================================
# Idempotency of complex documents
# =============================================================================


@pytest.mark.e2e
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


# =============================================================================
# Projection diff (informational): what does the first cycle lose?
# =============================================================================


@pytest.mark.e2e
class TestProjectionDiff:
    """Show what the first push/pull cycle changes.

    These don't assert identity — they just run the first cycle and
    report the diff so we can track progress as we fix bugs.
    """

    @pytest.mark.parametrize("name", FIXTURES.keys())
    def test_fixture_diff(self, name, doc_id, services):
        """Push -> pull, report diff against original."""
        md = FIXTURES[name]
        docs = services["docs"]
        drive = services["drive"]

        tab_name = _unique(f"diff_{name}")
        push_md(doc_id, tab_name, md, docs)
        m1 = pull_md(doc_id, tab_name, docs, drive)

        d = diff(md, m1, "original", "pulled")
        if d:
            # Print diff but don't fail — this is informational
            print(f"\n--- Projection diff for '{name}' ---")
            print(d)
            print("--- end ---")
