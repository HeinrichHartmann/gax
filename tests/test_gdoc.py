"""Tests for Google Docs sync functionality.

Uses mock service objects to test without hitting real Google APIs.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from gax.gdoc import (
    pull_doc,
    format_multipart,
    format_section,
    pull_single_tab,
    compute_tab_paths,
    DocSection,
)
from gax.gdoc.native_md import get_doc_tabs, split_doc_by_tabs, TabInfo


# Load fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Load a fixture file as string."""
    return (FIXTURES_DIR / name).read_text()


def make_mock_service(doc_response: dict):
    """Create a mock Docs service that returns the given document."""
    service = MagicMock()
    service.documents().get().execute.return_value = doc_response
    return service


class TestPullDoc:
    """Tests for pull_doc function."""

    @patch("gax.gdoc.native_md")
    def test_multi_tab_document(self, mock_native_md):
        """Test pulling a document with multiple tabs."""
        # Mock native_md functions
        mock_native_md.get_doc_tabs.return_value = (
            "Project Plan",
            [
                TabInfo(id="t.1", title="Overview", index=0),
                TabInfo(id="t.2", title="Timeline", index=1),
            ],
        )
        mock_native_md.export_doc_markdown.return_value = (
            "# Overview\n\nThese are the project goals.\n\n"
            "# Timeline\n\n## Key Milestones\n\nMilestone 1\n"
        )
        mock_native_md.split_doc_by_tabs.return_value = {
            "Overview": "These are the project goals.",
            "Timeline": "## Key Milestones\n\nMilestone 1",
        }

        # Mock docs service for title
        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Project Plan"}

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=docs_service,
        )

        # Should have 2 sections (one per tab)
        assert len(sections) == 2

        # Check first section (Overview)
        assert sections[0].title == "Project Plan"
        assert sections[0].section == 1
        assert sections[0].section_title == "Overview"
        assert "project goals" in sections[0].content

        # Check second section (Timeline)
        assert sections[1].title == "Project Plan"
        assert sections[1].section == 2
        assert sections[1].section_title == "Timeline"
        assert "Key Milestones" in sections[1].content

    @patch("gax.gdoc.native_md")
    def test_single_tab_document(self, mock_native_md):
        """Test pulling a document with a single tab."""
        mock_native_md.get_doc_tabs.return_value = (
            "Simple Doc",
            [TabInfo(id="t1", title="Simple Doc", index=0)],
        )
        mock_native_md.export_doc_markdown.return_value = (
            "# Simple Doc\n\nHello World\n"
        )
        mock_native_md.split_doc_by_tabs.return_value = {"Simple Doc": "Hello World"}

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Simple Doc"}

        sections = pull_doc(
            "single-doc",
            "https://docs.google.com/document/d/single-doc/edit",
            docs_service=docs_service,
        )

        assert len(sections) == 1
        assert sections[0].title == "Simple Doc"
        assert "Hello World" in sections[0].content

    @patch("gax.gdoc.native_md")
    def test_document_without_tabs(self, mock_native_md):
        """Test pulling a legacy document without tabs array."""
        # No tabs returned means fallback to default
        mock_native_md.get_doc_tabs.return_value = ("Legacy Doc", [])
        mock_native_md.export_doc_markdown.return_value = "Legacy content\n"
        mock_native_md.split_doc_by_tabs.return_value = {"Document": "Legacy content"}

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Legacy Doc"}

        sections = pull_doc(
            "legacy-doc",
            "https://docs.google.com/document/d/legacy-doc/edit",
            docs_service=docs_service,
        )

        assert len(sections) == 1
        assert sections[0].title == "Legacy Doc"
        # When no tabs, uses fallback title "Document"
        assert sections[0].section_title == "Document"


class TestFormatMultipart:
    """Tests for multipart format output."""

    @patch("gax.gdoc.native_md")
    def test_format_multi_tab_to_file(self, mock_native_md, tmp_path):
        """Test formatting a multi-tab document and writing to file."""
        mock_native_md.get_doc_tabs.return_value = (
            "Project Plan",
            [
                TabInfo(id="t.1", title="Overview", index=0),
                TabInfo(id="t.2", title="Timeline", index=1),
            ],
        )
        mock_native_md.export_doc_markdown.return_value = (
            "# Overview\n\nProject goals.\n\n"
            "# Timeline\n\n## Key Milestones\n\nMilestone 1\n"
        )
        mock_native_md.split_doc_by_tabs.return_value = {
            "Overview": "Project goals.",
            "Timeline": "## Key Milestones\n\nMilestone 1",
        }

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Project Plan"}

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=docs_service,
        )

        content = format_multipart(sections)

        # Write to temp file
        output_file = tmp_path / "Project_Plan.doc.gax.md"
        output_file.write_text(content)

        # Verify file contents
        written = output_file.read_text()

        # Should have two sections with YAML headers
        assert written.count("---\n") >= 4  # At least 2 sections x 2 delimiters
        assert "title: Project Plan" in written
        assert "tab: Overview" in written
        assert "tab: Timeline" in written

        # Content should be present
        assert "Key Milestones" in written

    @patch("gax.gdoc.native_md")
    def test_sections_are_self_contained(self, mock_native_md, tmp_path):
        """Test that each section can be extracted as a standalone file."""
        mock_native_md.get_doc_tabs.return_value = (
            "Project Plan",
            [
                TabInfo(id="t.1", title="Overview", index=0),
                TabInfo(id="t.2", title="Timeline", index=1),
            ],
        )
        mock_native_md.export_doc_markdown.return_value = "# Overview\n\n# Timeline\n"
        mock_native_md.split_doc_by_tabs.return_value = {
            "Overview": "Overview content",
            "Timeline": "Timeline content",
        }

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Project Plan"}

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=docs_service,
        )

        # Each section should have full metadata
        for section in sections:
            assert section.title == "Project Plan"
            assert "docs.google.com" in section.source
            assert section.time  # Should have timestamp

        # Format each section individually and write as standalone file
        for i, section in enumerate(sections):
            single = format_section(section)

            output_file = tmp_path / f"section_{i + 1}.doc.gax.md"
            output_file.write_text(single)

            # Verify it's valid
            written = output_file.read_text()
            assert written.startswith("---\n")
            assert "title: Project Plan" in written


class TestHeadingConversion:
    """Tests for heading style conversion (native API handles this)."""

    @patch("gax.gdoc.native_md")
    def test_heading_levels(self, mock_native_md):
        """Test that headings from native export are preserved."""
        # Native API returns markdown directly with headings
        mock_native_md.get_doc_tabs.return_value = (
            "Headings Test",
            [TabInfo(id="t1", title="Headings", index=0)],
        )
        mock_native_md.export_doc_markdown.return_value = (
            "# Headings\n\n# Heading 1\n\n## Heading 2\n\n### Heading 3\n\n"
            "#### Heading 4\n\nNormal text\n"
        )
        mock_native_md.split_doc_by_tabs.return_value = {
            "Headings": (
                "# Heading 1\n\n## Heading 2\n\n### Heading 3\n\n"
                "#### Heading 4\n\nNormal text"
            )
        }

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Headings Test"}

        sections = pull_doc("headings-doc", "https://...", docs_service=docs_service)

        content = sections[0].content
        assert "# Heading 1" in content
        assert "## Heading 2" in content
        assert "### Heading 3" in content
        assert "#### Heading 4" in content
        assert "Normal text" in content


class TestGetDocTabs:
    """Tests for get_doc_tabs function."""

    def test_multi_tab_document(self):
        """Test getting tabs from a multi-tab document."""
        doc_response = json.loads(load_fixture("sample_doc_response.json"))
        service = make_mock_service(doc_response)

        title, tabs = get_doc_tabs("test-doc-123", docs_service=service)

        assert title == "Project Plan"
        assert len(tabs) == 2
        assert tabs[0].title == "Overview"
        assert tabs[0].index == 0
        assert tabs[1].title == "Timeline"
        assert tabs[1].index == 1

    def test_single_tab_document(self):
        """Test getting tabs from a single-tab document."""
        doc_response = {
            "documentId": "single-doc",
            "title": "Simple Doc",
            "tabs": [
                {
                    "tabProperties": {"tabId": "t.123", "title": "Simple Doc"},
                    "documentTab": {"body": {"content": []}},
                }
            ],
        }
        service = make_mock_service(doc_response)

        title, tabs = get_doc_tabs("single-doc", docs_service=service)

        assert title == "Simple Doc"
        assert len(tabs) == 1
        assert tabs[0].title == "Simple Doc"
        assert tabs[0].id == "t.123"

    def test_legacy_document_no_tabs(self):
        """Test getting tabs from a legacy document without tabs array."""
        doc_response = {
            "documentId": "legacy-doc",
            "title": "Legacy Doc",
            "body": {"content": []},
        }
        service = make_mock_service(doc_response)

        title, tabs = get_doc_tabs("legacy-doc", docs_service=service)

        assert title == "Legacy Doc"
        assert len(tabs) == 0

    def test_nested_tabs(self):
        """Test getting tabs from a document with nested (child) tabs."""
        doc_response = {
            "documentId": "nested-doc",
            "title": "Nested Doc",
            "tabs": [
                {
                    "tabProperties": {"tabId": "t.1", "title": "Overview"},
                    "documentTab": {"body": {"content": []}},
                },
                {
                    "tabProperties": {"tabId": "t.2", "title": "Design"},
                    "documentTab": {"body": {"content": []}},
                    "childTabs": [
                        {
                            "tabProperties": {"tabId": "t.3", "title": "Frontend"},
                            "documentTab": {"body": {"content": []}},
                        },
                        {
                            "tabProperties": {"tabId": "t.4", "title": "Backend"},
                            "documentTab": {"body": {"content": []}},
                            "childTabs": [
                                {
                                    "tabProperties": {"tabId": "t.5", "title": "API"},
                                    "documentTab": {"body": {"content": []}},
                                },
                            ],
                        },
                    ],
                },
            ],
        }
        service = make_mock_service(doc_response)

        title, tabs = get_doc_tabs("nested-doc", docs_service=service)

        assert title == "Nested Doc"
        assert len(tabs) == 5
        assert [(t.title, t.depth, t.has_children) for t in tabs] == [
            ("Overview", 0, False),
            ("Design", 0, True),
            ("Frontend", 1, False),
            ("Backend", 1, True),
            ("API", 2, False),
        ]
        assert [t.index for t in tabs] == [0, 1, 2, 3, 4]


class TestPullSingleTab:
    """Tests for pull_single_tab function."""

    @patch("gax.gdoc.native_md")
    def test_pull_specific_tab(self, mock_native_md):
        """Test pulling a specific tab by name."""
        mock_native_md.export_tab_markdown.return_value = (
            "## Key Milestones\n\nMilestone 1"
        )

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Project Plan"}

        section = pull_single_tab(
            "test-doc-123",
            "Timeline",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=docs_service,
        )

        assert section.title == "Project Plan"
        assert section.section_title == "Timeline"
        assert "Key Milestones" in section.content

    @patch("gax.gdoc.native_md")
    def test_pull_first_tab(self, mock_native_md):
        """Test pulling the first tab."""
        mock_native_md.export_tab_markdown.return_value = "Overview content here"

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Project Plan"}

        section = pull_single_tab(
            "test-doc-123",
            "Overview",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=docs_service,
        )

        assert section.section_title == "Overview"
        assert "Overview content" in section.content

    @patch("gax.gdoc.native_md")
    def test_pull_tab_not_found(self, mock_native_md):
        """Test pulling a non-existent tab raises error."""
        mock_native_md.export_tab_markdown.side_effect = ValueError(
            "Tab not found: NonExistent"
        )

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Project Plan"}

        import pytest

        with pytest.raises(ValueError, match="Tab not found"):
            pull_single_tab(
                "test-doc-123",
                "NonExistent",
                "https://docs.google.com/document/d/test-doc-123/edit",
                docs_service=docs_service,
            )

    @patch("gax.gdoc.native_md")
    def test_pull_legacy_document(self, mock_native_md):
        """Test pulling from a legacy document."""
        mock_native_md.export_tab_markdown.return_value = "Legacy content"

        docs_service = MagicMock()
        docs_service.documents().get().execute.return_value = {"title": "Legacy Doc"}

        section = pull_single_tab(
            "legacy-doc",
            "Legacy Doc",
            "https://docs.google.com/document/d/legacy-doc/edit",
            docs_service=docs_service,
        )

        assert section.section_title == "Legacy Doc"
        assert "Legacy content" in section.content


def _section(title, depth=0, has_children=False, tab_id="t.x", section_type=None):
    """Helper to create a DocSection for path computation tests."""
    return DocSection(
        title="Doc",
        source="https://docs.google.com/document/d/abc/edit",
        time="2026-01-01T00:00:00Z",
        section=1,
        section_title=title,
        content="",
        section_type=section_type,
        tab_depth=depth,
        tab_has_children=has_children,
        tab_id=tab_id,
    )


class TestComputeTabPaths:
    """Tests for compute_tab_paths function."""

    def test_flat_tabs(self, tmp_path):
        """Flat tabs (no nesting) produce files in root folder."""
        sections = [
            _section("Overview"),
            _section("Timeline"),
        ]
        paths = compute_tab_paths(sections, tmp_path)
        assert [p.relative_to(tmp_path) for p in paths] == [
            Path("Overview.tab.gax.md"),
            Path("Timeline.tab.gax.md"),
        ]

    def test_nested_tabs(self, tmp_path):
        """Nested tabs produce subdirectories."""
        sections = [
            _section("Overview"),
            _section("Design", has_children=True),
            _section("Frontend", depth=1),
            _section("Backend", depth=1, has_children=True),
            _section("API", depth=2),
        ]
        paths = compute_tab_paths(sections, tmp_path)
        rel = [p.relative_to(tmp_path) for p in paths]
        assert rel == [
            Path("Overview.tab.gax.md"),
            Path("Design/Design.tab.gax.md"),
            Path("Design/Frontend.tab.gax.md"),
            Path("Design/Backend/Backend.tab.gax.md"),
            Path("Design/Backend/API.tab.gax.md"),
        ]

    def test_sibling_parent_tabs(self, tmp_path):
        """Two top-level parent tabs produce separate subdirectories."""
        sections = [
            _section("Design", has_children=True),
            _section("Frontend", depth=1),
            _section("Testing", has_children=True),
            _section("Unit", depth=1),
        ]
        paths = compute_tab_paths(sections, tmp_path)
        rel = [p.relative_to(tmp_path) for p in paths]
        assert rel == [
            Path("Design/Design.tab.gax.md"),
            Path("Design/Frontend.tab.gax.md"),
            Path("Testing/Testing.tab.gax.md"),
            Path("Testing/Unit.tab.gax.md"),
        ]

    def test_comment_sections_skipped(self, tmp_path):
        """Comment sections get empty Path placeholders."""
        sections = [
            _section("Overview"),
            _section("Overview (Comments)", section_type="comments"),
        ]
        paths = compute_tab_paths(sections, tmp_path)
        assert paths[0] == tmp_path / "Overview.tab.gax.md"
        assert paths[1] == Path("")

    def test_special_characters_sanitized(self, tmp_path):
        """Tab names with special characters are sanitized."""
        sections = [_section("Q&A: Design/Notes")]
        paths = compute_tab_paths(sections, tmp_path)
        assert paths[0] == tmp_path / "Q&A-_Design-Notes.tab.gax.md"


class TestSplitDocByTabsNested:
    """Tests for split_doc_by_tabs with nested tabs.

    The Drive API exports ALL tabs as H1 headers regardless of nesting depth.
    """

    def test_flat_h1_tabs(self):
        """Standard H1-separated tabs."""
        md = "# Overview\n\nGoals.\n\n# Timeline\n\nMilestones.\n"
        result = split_doc_by_tabs(md, ["Overview", "Timeline"])
        assert "Goals." in result["Overview"]
        assert "Milestones." in result["Timeline"]

    def test_nested_all_h1(self):
        """Nested tabs are all exported as H1 by the Drive API."""
        md = (
            "# Overview\n\nGoals.\n\n"
            "# Design\n\nIntro.\n\n"
            "# Frontend\n\nReact stuff.\n\n"
            "# Backend\n\nPython stuff.\n"
        )
        result = split_doc_by_tabs(
            md,
            ["Overview", "Design", "Frontend", "Backend"],
            [0, 0, 1, 1],
        )
        assert "Goals." in result["Overview"]
        assert "Intro." in result["Design"]
        assert "React stuff." in result["Frontend"]
        assert "Python stuff." in result["Backend"]

    def test_h2_not_matched_as_tab_when_not_in_list(self):
        """H2 headers that aren't tab titles should be kept as content."""
        md = "# Overview\n\n## Subsection\n\nBody.\n"
        result = split_doc_by_tabs(md, ["Overview"], [0])
        assert "## Subsection" in result["Overview"]
        assert "Body." in result["Overview"]
