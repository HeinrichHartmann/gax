"""Tests for Google Docs sync functionality.

Uses mock service objects to test without hitting real Google APIs.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from gax.gdoc import (
    pull_doc,
    format_multipart,
    format_section,
    get_tabs_list,
    pull_single_tab,
)


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

    def test_multi_tab_document(self):
        """Test pulling a document with multiple tabs."""
        doc_response = json.loads(load_fixture("sample_doc_response.json"))
        service = make_mock_service(doc_response)

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            service=service,
        )

        # Should have 2 sections (one per tab)
        assert len(sections) == 2

        # Check first section (Overview)
        assert sections[0].title == "Project Plan"
        assert sections[0].section == 1
        assert sections[0].section_title == "Overview"
        assert "# Overview" in sections[0].content
        assert "project goals" in sections[0].content

        # Check second section (Timeline)
        assert sections[1].title == "Project Plan"
        assert sections[1].section == 2
        assert sections[1].section_title == "Timeline"
        assert "# Timeline" in sections[1].content
        assert "## Key Milestones" in sections[1].content

    def test_single_tab_document(self):
        """Test pulling a document with a single tab."""
        doc_response = {
            "documentId": "single-doc",
            "title": "Simple Doc",
            "tabs": [
                {
                    "tabProperties": {"tabId": "t1", "title": "Simple Doc"},
                    "documentTab": {
                        "body": {
                            "content": [
                                {
                                    "paragraph": {
                                        "elements": [
                                            {"textRun": {"content": "Hello World\n"}}
                                        ],
                                        "paragraphStyle": {
                                            "namedStyleType": "NORMAL_TEXT"
                                        },
                                    }
                                }
                            ]
                        }
                    },
                }
            ],
        }
        service = make_mock_service(doc_response)

        sections = pull_doc(
            "single-doc",
            "https://docs.google.com/document/d/single-doc/edit",
            service=service,
        )

        assert len(sections) == 1
        assert sections[0].title == "Simple Doc"
        assert "Hello World" in sections[0].content

    def test_document_without_tabs(self):
        """Test pulling a legacy document without tabs array."""
        doc_response = {
            "documentId": "legacy-doc",
            "title": "Legacy Doc",
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [{"textRun": {"content": "Legacy content\n"}}],
                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        }
                    }
                ]
            },
        }
        service = make_mock_service(doc_response)

        sections = pull_doc(
            "legacy-doc",
            "https://docs.google.com/document/d/legacy-doc/edit",
            service=service,
        )

        assert len(sections) == 1
        assert sections[0].title == "Legacy Doc"
        assert sections[0].section_title == "Legacy Doc"
        assert "Legacy content" in sections[0].content


class TestFormatMultipart:
    """Tests for multipart format output."""

    def test_format_multi_tab_to_file(self, tmp_path):
        """Test formatting a multi-tab document and writing to file."""
        doc_response = json.loads(load_fixture("sample_doc_response.json"))
        service = make_mock_service(doc_response)

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            service=service,
        )

        content = format_multipart(sections)

        # Write to temp file
        output_file = tmp_path / "Project_Plan.doc.gax"
        output_file.write_text(content)

        # Verify file contents
        written = output_file.read_text()

        # Should have two sections with YAML headers
        assert written.count("---\n") >= 4  # At least 2 sections x 2 delimiters
        assert "title: Project Plan" in written
        assert "section: 1" in written
        assert "section: 2" in written
        assert "section_title: Overview" in written
        assert "section_title: Timeline" in written

        # Content should be present
        assert "# Overview" in written
        assert "# Timeline" in written
        assert "## Key Milestones" in written

    def test_sections_are_self_contained(self, tmp_path):
        """Test that each section can be extracted as a standalone file."""
        doc_response = json.loads(load_fixture("sample_doc_response.json"))
        service = make_mock_service(doc_response)

        sections = pull_doc(
            "test-doc-123",
            "https://docs.google.com/document/d/test-doc-123/edit",
            service=service,
        )

        # Each section should have full metadata
        for section in sections:
            assert section.title == "Project Plan"
            assert "docs.google.com" in section.source
            assert section.time  # Should have timestamp

        # Format each section individually and write as standalone file
        for i, section in enumerate(sections):
            single = format_section(section)

            output_file = tmp_path / f"section_{i + 1}.doc.gax"
            output_file.write_text(single)

            # Verify it's valid
            written = output_file.read_text()
            assert written.startswith("---\n")
            assert "title: Project Plan" in written
            assert f"section: {i + 1}" in written


class TestHeadingConversion:
    """Tests for heading style conversion."""

    def test_heading_levels(self):
        """Test that heading styles are converted correctly."""
        doc_response = {
            "documentId": "headings-doc",
            "title": "Headings Test",
            "tabs": [
                {
                    "tabProperties": {"tabId": "t1", "title": "Headings"},
                    "documentTab": {
                        "body": {
                            "content": [
                                {
                                    "paragraph": {
                                        "elements": [
                                            {"textRun": {"content": "Heading 1\n"}}
                                        ],
                                        "paragraphStyle": {
                                            "namedStyleType": "HEADING_1"
                                        },
                                    }
                                },
                                {
                                    "paragraph": {
                                        "elements": [
                                            {"textRun": {"content": "Heading 2\n"}}
                                        ],
                                        "paragraphStyle": {
                                            "namedStyleType": "HEADING_2"
                                        },
                                    }
                                },
                                {
                                    "paragraph": {
                                        "elements": [
                                            {"textRun": {"content": "Heading 3\n"}}
                                        ],
                                        "paragraphStyle": {
                                            "namedStyleType": "HEADING_3"
                                        },
                                    }
                                },
                                {
                                    "paragraph": {
                                        "elements": [
                                            {"textRun": {"content": "Heading 4\n"}}
                                        ],
                                        "paragraphStyle": {
                                            "namedStyleType": "HEADING_4"
                                        },
                                    }
                                },
                                {
                                    "paragraph": {
                                        "elements": [
                                            {"textRun": {"content": "Normal text\n"}}
                                        ],
                                        "paragraphStyle": {
                                            "namedStyleType": "NORMAL_TEXT"
                                        },
                                    }
                                },
                            ]
                        }
                    },
                }
            ],
        }

        service = make_mock_service(doc_response)
        sections = pull_doc("headings-doc", "https://...", service=service)

        content = sections[0].content
        assert "# Heading 1" in content
        assert "## Heading 2" in content
        assert "### Heading 3" in content
        assert "#### Heading 4" in content
        assert "Normal text" in content
        # Normal text should NOT have # prefix
        assert "\n# Normal text" not in content


class TestGetTabsList:
    """Tests for get_tabs_list function."""

    def test_multi_tab_document(self):
        """Test getting tabs from a multi-tab document."""
        doc_response = json.loads(load_fixture("sample_doc_response.json"))
        service = make_mock_service(doc_response)

        info = get_tabs_list("test-doc-123", service=service)

        assert info["title"] == "Project Plan"
        assert len(info["tabs"]) == 2
        assert info["tabs"][0]["title"] == "Overview"
        assert info["tabs"][0]["index"] == 0
        assert info["tabs"][1]["title"] == "Timeline"
        assert info["tabs"][1]["index"] == 1

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

        info = get_tabs_list("single-doc", service=service)

        assert info["title"] == "Simple Doc"
        assert len(info["tabs"]) == 1
        assert info["tabs"][0]["title"] == "Simple Doc"
        assert info["tabs"][0]["id"] == "t.123"

    def test_legacy_document_no_tabs(self):
        """Test getting tabs from a legacy document without tabs array."""
        doc_response = {
            "documentId": "legacy-doc",
            "title": "Legacy Doc",
            "body": {"content": []},
        }
        service = make_mock_service(doc_response)

        info = get_tabs_list("legacy-doc", service=service)

        assert info["title"] == "Legacy Doc"
        assert len(info["tabs"]) == 1
        assert info["tabs"][0]["title"] == "Legacy Doc"
        assert info["tabs"][0]["id"] == ""


class TestPullSingleTab:
    """Tests for pull_single_tab function."""

    def test_pull_specific_tab(self):
        """Test pulling a specific tab by name."""
        doc_response = json.loads(load_fixture("sample_doc_response.json"))
        service = make_mock_service(doc_response)

        section = pull_single_tab(
            "test-doc-123",
            "Timeline",
            "https://docs.google.com/document/d/test-doc-123/edit",
            service=service,
        )

        assert section.title == "Project Plan"
        assert section.section_title == "Timeline"
        assert "# Timeline" in section.content
        assert "## Key Milestones" in section.content

    def test_pull_first_tab(self):
        """Test pulling the first tab."""
        doc_response = json.loads(load_fixture("sample_doc_response.json"))
        service = make_mock_service(doc_response)

        section = pull_single_tab(
            "test-doc-123",
            "Overview",
            "https://docs.google.com/document/d/test-doc-123/edit",
            service=service,
        )

        assert section.section_title == "Overview"
        assert "# Overview" in section.content

    def test_pull_tab_not_found(self):
        """Test pulling a non-existent tab raises error."""
        doc_response = json.loads(load_fixture("sample_doc_response.json"))
        service = make_mock_service(doc_response)

        import pytest

        with pytest.raises(ValueError, match="Tab not found"):
            pull_single_tab(
                "test-doc-123",
                "NonExistent",
                "https://docs.google.com/document/d/test-doc-123/edit",
                service=service,
            )

    def test_pull_legacy_document(self):
        """Test pulling from a legacy document matches document title."""
        doc_response = {
            "documentId": "legacy-doc",
            "title": "Legacy Doc",
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [{"textRun": {"content": "Legacy content\n"}}],
                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        }
                    }
                ]
            },
        }
        service = make_mock_service(doc_response)

        section = pull_single_tab(
            "legacy-doc",
            "Legacy Doc",
            "https://docs.google.com/document/d/legacy-doc/edit",
            service=service,
        )

        assert section.section_title == "Legacy Doc"
        assert "Legacy content" in section.content
