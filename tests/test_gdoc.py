"""Tests for Google Docs sync functionality.

Uses mock service objects to test without hitting real Google APIs.
Now mocks documents().get() with includeTabsContent=True JSON responses
instead of mocking the Drive API markdown export.
"""

from unittest.mock import MagicMock

from gax.gdoc import (
    pull_doc,
    format_multipart,
    format_section,
    pull_single_tab,
)


def _make_paragraph(start, text, style="NORMAL_TEXT"):
    """Build a Google Docs paragraph element."""
    end = start + len(text) + 1  # +1 for trailing \n
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": text + "\n"},
                }
            ],
            "paragraphStyle": {"namedStyleType": style},
        },
    }


def _make_empty_para(start):
    """Build an empty paragraph (just a newline)."""
    return {
        "startIndex": start,
        "endIndex": start + 1,
        "paragraph": {
            "elements": [
                {
                    "startIndex": start,
                    "endIndex": start + 1,
                    "textRun": {"content": "\n"},
                }
            ],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        },
    }


def _make_doc_response(title, tabs):
    """Build a full documents().get() response.

    tabs: list of (tab_title, body_content_list) tuples
    """
    doc_tabs = []
    for i, (tab_title, content) in enumerate(tabs):
        doc_tabs.append(
            {
                "tabProperties": {"tabId": f"t.{i + 1}", "title": tab_title},
                "documentTab": {"body": {"content": content}},
            }
        )
    return {"documentId": "test-doc-123", "title": title, "tabs": doc_tabs}


def _make_mock_service(doc_response):
    """Create a mock Docs service that returns the given document."""
    service = MagicMock()
    service.documents().get().execute.return_value = doc_response
    return service


class TestPullDoc:
    """Tests for pull_doc function."""

    def test_multi_tab_document(self):
        """Test pulling a document with multiple tabs."""
        doc = _make_doc_response(
            "Project Plan",
            [
                (
                    "Overview",
                    [
                        _make_empty_para(1),
                        _make_paragraph(2, "These are the project goals"),
                    ],
                ),
                (
                    "Timeline",
                    [
                        _make_empty_para(1),
                        _make_paragraph(2, "Key Milestones", style="HEADING_2"),
                        _make_paragraph(17, "Milestone 1"),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        assert len(sections) == 2
        assert sections[0].title == "Project Plan"
        assert sections[0].section == 1
        assert sections[0].section_title == "Overview"
        assert "project goals" in sections[0].content
        assert sections[1].section == 2
        assert sections[1].section_title == "Timeline"
        assert "Key Milestones" in sections[1].content

    def test_single_tab_document(self):
        """Test pulling a document with a single tab."""
        doc = _make_doc_response(
            "Simple Doc",
            [
                (
                    "Simple Doc",
                    [
                        _make_empty_para(1),
                        _make_paragraph(2, "Hello World"),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        sections = pull_doc(
            "single-doc",
            "https://docs.google.com/document/d/single-doc/edit",
            docs_service=service,
        )

        assert len(sections) == 1
        assert sections[0].title == "Simple Doc"
        assert "Hello World" in sections[0].content

    def test_document_without_tabs(self):
        """Test pulling a document with no tabs returns empty."""
        doc = {"documentId": "legacy-doc", "title": "Legacy Doc", "tabs": []}
        service = _make_mock_service(doc)

        sections = pull_doc(
            "legacy-doc",
            "https://docs.google.com/document/d/legacy-doc/edit",
            docs_service=service,
        )

        assert len(sections) == 0


class TestFormatMultipart:
    """Tests for multipart format output."""

    def test_format_multi_tab_to_file(self, tmp_path):
        """Test formatting a multi-tab document and writing to file."""
        doc = _make_doc_response(
            "Project Plan",
            [
                (
                    "Overview",
                    [
                        _make_empty_para(1),
                        _make_paragraph(2, "Project goals"),
                    ],
                ),
                (
                    "Timeline",
                    [
                        _make_empty_para(1),
                        _make_paragraph(2, "Key Milestones", style="HEADING_2"),
                        _make_paragraph(17, "Milestone 1"),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        content = format_multipart(sections)

        output_file = tmp_path / "Project_Plan.doc.gax.md"
        output_file.write_text(content)

        written = output_file.read_text()
        assert written.count("---\n") >= 4
        assert "title: Project Plan" in written
        assert "tab: Overview" in written
        assert "tab: Timeline" in written
        assert "Key Milestones" in written

    def test_sections_are_self_contained(self, tmp_path):
        """Test that each section can be extracted as a standalone file."""
        doc = _make_doc_response(
            "Project Plan",
            [
                (
                    "Overview",
                    [_make_empty_para(1), _make_paragraph(2, "Overview content")],
                ),
                (
                    "Timeline",
                    [_make_empty_para(1), _make_paragraph(2, "Timeline content")],
                ),
            ],
        )
        service = _make_mock_service(doc)

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        for section in sections:
            assert section.title == "Project Plan"
            assert "docs.google.com" in section.source
            assert section.time

        for i, section in enumerate(sections):
            single = format_section(section)
            output_file = tmp_path / f"section_{i + 1}.doc.gax.md"
            output_file.write_text(single)
            written = output_file.read_text()
            assert written.startswith("---\n")
            assert "title: Project Plan" in written


class TestHeadingConversion:
    """Tests for heading style conversion."""

    def test_heading_levels(self):
        """Test that headings from doc JSON are preserved."""
        doc = _make_doc_response(
            "Headings Test",
            [
                (
                    "Headings",
                    [
                        _make_empty_para(1),
                        _make_paragraph(2, "Heading 1", style="HEADING_1"),
                        _make_paragraph(12, "Heading 2", style="HEADING_2"),
                        _make_paragraph(22, "Heading 3", style="HEADING_3"),
                        _make_paragraph(32, "Heading 4", style="HEADING_4"),
                        _make_paragraph(42, "Normal text"),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        sections = pull_doc("headings-doc", "https://...", docs_service=service)

        content = sections[0].content
        assert "# Heading 1" in content
        assert "## Heading 2" in content
        assert "### Heading 3" in content
        assert "#### Heading 4" in content
        assert "Normal text" in content


class TestPullSingleTab:
    """Tests for pull_single_tab function."""

    def test_pull_specific_tab(self):
        """Test pulling a specific tab by name."""
        doc = _make_doc_response(
            "Project Plan",
            [
                (
                    "Overview",
                    [_make_empty_para(1), _make_paragraph(2, "Overview content")],
                ),
                (
                    "Timeline",
                    [
                        _make_empty_para(1),
                        _make_paragraph(2, "Key Milestones", style="HEADING_2"),
                        _make_paragraph(17, "Milestone 1"),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        section = pull_single_tab(
            "test-doc-123",
            "Timeline",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        assert section.title == "Project Plan"
        assert section.section_title == "Timeline"
        assert "Key Milestones" in section.content

    def test_pull_first_tab(self):
        """Test pulling the first tab."""
        doc = _make_doc_response(
            "Project Plan",
            [
                (
                    "Overview",
                    [_make_empty_para(1), _make_paragraph(2, "Overview content here")],
                ),
            ],
        )
        service = _make_mock_service(doc)

        section = pull_single_tab(
            "test-doc-123",
            "Overview",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        assert section.section_title == "Overview"
        assert "Overview content" in section.content

    def test_pull_tab_not_found(self):
        """Test pulling a non-existent tab raises error."""
        doc = _make_doc_response(
            "Project Plan",
            [
                ("Overview", [_make_empty_para(1), _make_paragraph(2, "content")]),
            ],
        )
        service = _make_mock_service(doc)

        import pytest

        with pytest.raises(ValueError, match="Tab not found"):
            pull_single_tab(
                "test-doc-123",
                "NonExistent",
                "https://docs.google.com/document/d/test-doc-123/edit",
                docs_service=service,
            )

    def test_pull_with_formatting(self):
        """Test pulling a tab with inline formatting."""
        body = [
            _make_empty_para(1),
            {
                "startIndex": 2,
                "endIndex": 20,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 2,
                            "endIndex": 7,
                            "textRun": {"content": "Some ", "textStyle": {}},
                        },
                        {
                            "startIndex": 7,
                            "endIndex": 11,
                            "textRun": {"content": "bold", "textStyle": {"bold": True}},
                        },
                        {
                            "startIndex": 11,
                            "endIndex": 17,
                            "textRun": {"content": " text\n", "textStyle": {}},
                        },
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                },
            },
        ]
        doc = _make_doc_response("Formatted Doc", [("Main", body)])
        service = _make_mock_service(doc)

        section = pull_single_tab(
            "test-doc-123",
            "Main",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        assert "**bold**" in section.content
