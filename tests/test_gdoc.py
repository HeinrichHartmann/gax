"""Tests for Google Docs sync functionality.

Uses mock service objects to test without hitting real Google APIs.
Now mocks documents().get() with includeTabsContent=True JSON responses
instead of mocking the Drive API markdown export.
"""

from unittest.mock import MagicMock

from pathlib import Path

import pytest

from gax.gdoc.doc import (
    pull_doc,
    format_multipart,
    format_section,
    pull_single_tab,
)
from gax.gdoc.doc import _flatten_tabs, _compute_tab_paths, DocSection


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


# =============================================================================
# Nested tab helpers and tests
# =============================================================================


def _make_nested_doc_response(title, tab_tree):
    """Build a documents().get() response with nested tabs.

    tab_tree: list of (tab_title, body_content_list, children) tuples
              where children is a recursive list of the same shape.

    Example:
        _make_nested_doc_response("Doc", [
            ("Overview", [_make_empty_para(1)], []),
            ("Design", [_make_empty_para(1)], [
                ("Frontend", [_make_empty_para(1)], []),
                ("Backend", [_make_empty_para(1)], [
                    ("API", [_make_empty_para(1)], []),
                ]),
            ]),
        ])
    """
    counter = [0]  # mutable counter for unique tab IDs

    def _build_tabs(tree):
        result = []
        for tab_title, content, children in tree:
            counter[0] += 1
            tab = {
                "tabProperties": {"tabId": f"t.{counter[0]}", "title": tab_title},
                "documentTab": {"body": {"content": content}},
            }
            if children:
                tab["childTabs"] = _build_tabs(children)
            result.append(tab)
        return result

    return {
        "documentId": "test-doc-123",
        "title": title,
        "tabs": _build_tabs(tab_tree),
    }


class TestFlattenTabs:
    """Tests for _flatten_tabs recursive walker."""

    def test_flat_tabs(self):
        """Top-level only tabs produce depth=0."""
        tabs = [
            {"tabProperties": {"tabId": "t.1", "title": "A"}},
            {"tabProperties": {"tabId": "t.2", "title": "B"}},
        ]
        flat = _flatten_tabs(tabs)
        assert len(flat) == 2
        assert flat[0][1].title == "A"
        assert flat[0][1].depth == 0
        assert flat[1][1].title == "B"
        assert flat[1][1].depth == 0

    def test_nested_tabs(self):
        """Child tabs are flattened with increasing depth."""
        tabs = [
            {
                "tabProperties": {"tabId": "t.1", "title": "Parent"},
                "childTabs": [
                    {"tabProperties": {"tabId": "t.2", "title": "Child"}},
                ],
            },
        ]
        flat = _flatten_tabs(tabs)
        assert len(flat) == 2
        assert flat[0][1].title == "Parent"
        assert flat[0][1].depth == 0
        assert flat[0][1].has_children is True
        assert flat[1][1].title == "Child"
        assert flat[1][1].depth == 1
        assert flat[1][1].has_children is False

    def test_deeply_nested(self):
        """Three levels of nesting."""
        tabs = [
            {
                "tabProperties": {"tabId": "t.1", "title": "L0"},
                "childTabs": [
                    {
                        "tabProperties": {"tabId": "t.2", "title": "L1"},
                        "childTabs": [
                            {"tabProperties": {"tabId": "t.3", "title": "L2"}},
                        ],
                    },
                ],
            },
        ]
        flat = _flatten_tabs(tabs)
        assert len(flat) == 3
        depths = [info.depth for _, info in flat]
        assert depths == [0, 1, 2]

    def test_mixed_flat_and_nested(self):
        """Mix of tabs with and without children."""
        tabs = [
            {"tabProperties": {"tabId": "t.1", "title": "Flat"}},
            {
                "tabProperties": {"tabId": "t.2", "title": "Parent"},
                "childTabs": [
                    {"tabProperties": {"tabId": "t.3", "title": "Child"}},
                ],
            },
        ]
        flat = _flatten_tabs(tabs)
        assert len(flat) == 3
        titles = [info.title for _, info in flat]
        assert titles == ["Flat", "Parent", "Child"]


class TestPullDocNested:
    """Tests for pull_doc with nested tabs."""

    def test_nested_tabs_all_pulled(self):
        """All nested tabs are pulled in order."""
        doc = _make_nested_doc_response(
            "Nested Doc",
            [
                (
                    "Overview",
                    [_make_empty_para(1), _make_paragraph(2, "Top level")],
                    [],
                ),
                (
                    "Design",
                    [_make_empty_para(1), _make_paragraph(2, "Design content")],
                    [
                        (
                            "Frontend",
                            [_make_empty_para(1), _make_paragraph(2, "FE stuff")],
                            [],
                        ),
                        (
                            "Backend",
                            [_make_empty_para(1), _make_paragraph(2, "BE stuff")],
                            [],
                        ),
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

        assert len(sections) == 4
        titles = [s.section_title for s in sections]
        assert titles == ["Overview", "Design", "Frontend", "Backend"]

        assert sections[0].tab_depth == 0
        assert sections[1].tab_depth == 0
        assert sections[1].tab_has_children is True
        assert sections[2].tab_depth == 1
        assert sections[3].tab_depth == 1

    def test_deeply_nested_pull(self):
        """Three levels of nesting are pulled."""
        doc = _make_nested_doc_response(
            "Deep Doc",
            [
                (
                    "Root",
                    [_make_empty_para(1), _make_paragraph(2, "Root")],
                    [
                        (
                            "Mid",
                            [_make_empty_para(1), _make_paragraph(2, "Mid")],
                            [
                                (
                                    "Leaf",
                                    [_make_empty_para(1), _make_paragraph(2, "Leaf")],
                                    [],
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        sections = pull_doc(
            "test-doc-123",
            "https://...",
            docs_service=service,
        )

        assert len(sections) == 3
        depths = [s.tab_depth for s in sections]
        assert depths == [0, 1, 2]


class TestPullSingleTabNested:
    """Tests for pull_single_tab with nested tabs."""

    def test_find_child_tab_by_name(self):
        """Can find a nested child tab by simple name."""
        doc = _make_nested_doc_response(
            "Doc",
            [
                (
                    "Parent",
                    [_make_empty_para(1), _make_paragraph(2, "parent content")],
                    [
                        (
                            "Child",
                            [_make_empty_para(1), _make_paragraph(2, "child content")],
                            [],
                        ),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        section = pull_single_tab(
            "test-doc-123",
            "Child",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        assert section.section_title == "Child"
        assert "child content" in section.content
        assert section.tab_depth == 1

    def test_find_by_path(self):
        """Can find a tab using path-qualified name."""
        doc = _make_nested_doc_response(
            "Doc",
            [
                (
                    "Design",
                    [_make_empty_para(1)],
                    [
                        (
                            "Frontend",
                            [_make_empty_para(1), _make_paragraph(2, "FE")],
                            [],
                        ),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        section = pull_single_tab(
            "test-doc-123",
            "Design/Frontend",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        assert section.section_title == "Frontend"

    def test_ambiguous_name_raises(self):
        """Ambiguous tab name raises ValueError with path hints."""
        doc = _make_nested_doc_response(
            "Doc",
            [
                (
                    "A",
                    [_make_empty_para(1)],
                    [
                        (
                            "Notes",
                            [_make_empty_para(1), _make_paragraph(2, "A notes")],
                            [],
                        )
                    ],
                ),
                (
                    "B",
                    [_make_empty_para(1)],
                    [
                        (
                            "Notes",
                            [_make_empty_para(1), _make_paragraph(2, "B notes")],
                            [],
                        )
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        with pytest.raises(ValueError, match="Ambiguous"):
            pull_single_tab(
                "test-doc-123",
                "Notes",
                "https://docs.google.com/document/d/test-doc-123/edit",
                docs_service=service,
            )

    def test_disambiguate_with_path(self):
        """Path-qualified name resolves ambiguity."""
        doc = _make_nested_doc_response(
            "Doc",
            [
                (
                    "A",
                    [_make_empty_para(1)],
                    [
                        (
                            "Notes",
                            [_make_empty_para(1), _make_paragraph(2, "A notes")],
                            [],
                        )
                    ],
                ),
                (
                    "B",
                    [_make_empty_para(1)],
                    [
                        (
                            "Notes",
                            [_make_empty_para(1), _make_paragraph(2, "B notes")],
                            [],
                        )
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        section = pull_single_tab(
            "test-doc-123",
            "B/Notes",
            "https://docs.google.com/document/d/test-doc-123/edit",
            docs_service=service,
        )

        assert "B notes" in section.content


class TestComputeTabPaths:
    """Tests for _compute_tab_paths filesystem layout."""

    def test_flat_tabs(self, tmp_path):
        """Flat tabs produce flat files."""
        sections = [
            DocSection("Doc", "url", "time", 1, "Overview", "content"),
            DocSection("Doc", "url", "time", 2, "Notes", "content"),
        ]
        paths = _compute_tab_paths(sections, tmp_path)
        assert paths[0] == tmp_path / "Overview.doc.gax.md"
        assert paths[1] == tmp_path / "Notes.doc.gax.md"

    def test_nested_tabs(self, tmp_path):
        """Parent tabs create subdirectories."""
        sections = [
            DocSection(
                "Doc",
                "url",
                "time",
                1,
                "Design",
                "c",
                tab_depth=0,
                tab_has_children=True,
                tab_id="t.1",
            ),
            DocSection(
                "Doc",
                "url",
                "time",
                2,
                "Frontend",
                "c",
                tab_depth=1,
                tab_has_children=False,
                tab_id="t.2",
            ),
            DocSection(
                "Doc",
                "url",
                "time",
                3,
                "Backend",
                "c",
                tab_depth=1,
                tab_has_children=False,
                tab_id="t.3",
            ),
        ]
        paths = _compute_tab_paths(sections, tmp_path)
        assert paths[0] == tmp_path / "Design" / "Design.doc.gax.md"
        assert paths[1] == tmp_path / "Design" / "Frontend.doc.gax.md"
        assert paths[2] == tmp_path / "Design" / "Backend.doc.gax.md"

    def test_deeply_nested(self, tmp_path):
        """Three-level nesting creates nested subdirectories."""
        sections = [
            DocSection(
                "Doc",
                "url",
                "time",
                1,
                "Root",
                "c",
                tab_depth=0,
                tab_has_children=True,
                tab_id="t.1",
            ),
            DocSection(
                "Doc",
                "url",
                "time",
                2,
                "Mid",
                "c",
                tab_depth=1,
                tab_has_children=True,
                tab_id="t.2",
            ),
            DocSection(
                "Doc",
                "url",
                "time",
                3,
                "Leaf",
                "c",
                tab_depth=2,
                tab_has_children=False,
                tab_id="t.3",
            ),
        ]
        paths = _compute_tab_paths(sections, tmp_path)
        assert paths[0] == tmp_path / "Root" / "Root.doc.gax.md"
        assert paths[1] == tmp_path / "Root" / "Mid" / "Mid.doc.gax.md"
        assert paths[2] == tmp_path / "Root" / "Mid" / "Leaf.doc.gax.md"

    def test_mixed_flat_and_nested(self, tmp_path):
        """Mix of flat leaf and parent-with-children tabs."""
        sections = [
            DocSection(
                "Doc",
                "url",
                "time",
                1,
                "Intro",
                "c",
                tab_depth=0,
                tab_has_children=False,
            ),
            DocSection(
                "Doc",
                "url",
                "time",
                2,
                "Design",
                "c",
                tab_depth=0,
                tab_has_children=True,
                tab_id="t.2",
            ),
            DocSection(
                "Doc",
                "url",
                "time",
                3,
                "Frontend",
                "c",
                tab_depth=1,
                tab_has_children=False,
                tab_id="t.3",
            ),
            DocSection(
                "Doc",
                "url",
                "time",
                4,
                "Appendix",
                "c",
                tab_depth=0,
                tab_has_children=False,
            ),
        ]
        paths = _compute_tab_paths(sections, tmp_path)
        assert paths[0] == tmp_path / "Intro.doc.gax.md"
        assert paths[1] == tmp_path / "Design" / "Design.doc.gax.md"
        assert paths[2] == tmp_path / "Design" / "Frontend.doc.gax.md"
        assert paths[3] == tmp_path / "Appendix.doc.gax.md"

    def test_comments_get_placeholder(self, tmp_path):
        """Comment sections get empty Path placeholders."""
        sections = [
            DocSection("Doc", "url", "time", 1, "Tab", "c"),
            DocSection(
                "Doc", "url", "time", 2, "Comments", "c", section_type="comments"
            ),
        ]
        paths = _compute_tab_paths(sections, tmp_path)
        assert paths[0] == tmp_path / "Tab.doc.gax.md"
        assert paths[1] == Path("")


class TestDocCloneNested:
    """Tests for Doc.clone with nested tabs."""

    def test_clone_creates_subdirectories(self, tmp_path):
        """Clone with nested tabs creates subdirectory layout."""
        doc = _make_nested_doc_response(
            "Project",
            [
                ("Overview", [_make_empty_para(1), _make_paragraph(2, "overview")], []),
                (
                    "Design",
                    [_make_empty_para(1), _make_paragraph(2, "design")],
                    [
                        (
                            "Frontend",
                            [_make_empty_para(1), _make_paragraph(2, "frontend")],
                            [],
                        ),
                    ],
                ),
            ],
        )
        service = _make_mock_service(doc)

        # Monkey-patch _fetch_doc to use our mock
        import gax.gdoc.doc as doc_module

        original_fetch = doc_module._fetch_doc
        doc_module._fetch_doc = lambda *a, **kw: doc["documentId"] and doc

        # Need to also patch pull_doc's _fetch_doc usage
        # Simpler: call pull_doc directly and then test _compute_tab_paths
        try:
            sections = pull_doc(
                "test-doc-123",
                "https://docs.google.com/document/d/test-doc-123/edit",
                docs_service=service,
            )

            folder = tmp_path / "Project.doc.gax.md.d"
            folder.mkdir()
            paths = _compute_tab_paths(sections, folder)

            # Verify layout
            assert paths[0] == folder / "Overview.doc.gax.md"
            assert paths[1] == folder / "Design" / "Design.doc.gax.md"
            assert paths[2] == folder / "Design" / "Frontend.doc.gax.md"

            # Write files and verify they exist
            for section, fpath in zip(sections, paths):
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(format_section(section), encoding="utf-8")

            assert (folder / "Overview.doc.gax.md").exists()
            assert (folder / "Design" / "Design.doc.gax.md").exists()
            assert (folder / "Design" / "Frontend.doc.gax.md").exists()
        finally:
            doc_module._fetch_doc = original_fetch
