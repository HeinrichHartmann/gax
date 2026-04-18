"""Tests for gax.gdoc.ir — the unified Block/Span IR."""

import json
from pathlib import Path

from gax.gdoc.ir import (
    CodeBlock,
    Heading,
    ListItem,
    Paragraph,
    Span,
    Table,
    _utf16_len,
    check_unsupported,
    from_doc_json,
    from_markdown,
    render_markdown,
    to_docs_requests,
    to_tokens,
)

FIXTURES = Path(__file__).parent / "fixtures"


# =============================================================================
# Helpers
# =============================================================================


class TestUtf16Len:
    def test_ascii(self):
        assert _utf16_len("hello") == 5

    def test_emoji(self):
        # 🟢 is U+1F7E2, outside BMP → 2 UTF-16 code units
        assert _utf16_len("🟢") == 2

    def test_mixed(self):
        assert _utf16_len("a🟢b") == 4


# =============================================================================
# from_markdown (Markdown → IR)
# =============================================================================


class TestFromMarkdown:
    def test_heading(self):
        blocks = from_markdown("## Hello\n")
        assert len(blocks) == 1
        assert isinstance(blocks[0], Heading)
        assert blocks[0].level == 2
        assert blocks[0].text == "Hello"

    def test_paragraph_with_formatting(self):
        blocks = from_markdown("This is **bold** and *italic*.\n")
        assert len(blocks) == 1
        p = blocks[0]
        assert isinstance(p, Paragraph)
        assert len(p.spans) == 5
        assert p.spans[0] == Span("This is ")
        assert p.spans[1] == Span("bold", bold=True)
        assert p.spans[2] == Span(" and ")
        assert p.spans[3] == Span("italic", italic=True)
        assert p.spans[4] == Span(".")

    def test_strikethrough(self):
        blocks = from_markdown("~~struck~~\n")
        assert len(blocks) == 1
        assert blocks[0].spans[0] == Span("struck", strikethrough=True)

    def test_link(self):
        blocks = from_markdown("[click](https://example.com)\n")
        assert len(blocks) == 1
        assert blocks[0].spans[0] == Span("click", url="https://example.com")

    def test_unordered_list(self):
        blocks = from_markdown("- one\n- two\n")
        assert len(blocks) == 2
        assert all(isinstance(b, ListItem) for b in blocks)
        assert not blocks[0].ordered
        assert blocks[0].spans[0].text == "one"

    def test_ordered_list(self):
        blocks = from_markdown("1. first\n2. second\n")
        assert len(blocks) == 2
        assert all(b.ordered for b in blocks)

    def test_code_block(self):
        blocks = from_markdown("```python\nprint('hi')\n```\n")
        assert len(blocks) == 1
        assert isinstance(blocks[0], CodeBlock)
        assert blocks[0].code == "print('hi')"
        assert blocks[0].language == "python"

    def test_table(self):
        md = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
        blocks = from_markdown(md)
        assert len(blocks) == 1
        t = blocks[0]
        assert isinstance(t, Table)
        assert len(t.rows) == 2  # header + 1 data row
        assert t.rows[0][0][0].text == "A"
        assert t.rows[1][0][0].text == "1"

    def test_no_doc_range(self):
        blocks = from_markdown("Hello\n")
        assert blocks[0].doc_range is None


# =============================================================================
# from_doc_json (Google Docs JSON → IR)
# =============================================================================


class TestFromDocJson:
    def test_sample_fixture(self):
        with open(FIXTURES / "sample_doc_response.json") as f:
            doc = json.load(f)
        tab = doc["tabs"][0]
        body = tab["documentTab"]["body"]["content"]
        blocks = from_doc_json(body)

        assert len(blocks) == 3
        assert isinstance(blocks[0], Heading)
        assert blocks[0].text == "Overview"
        assert blocks[0].level == 1
        assert blocks[0].doc_range == (2, 11)

        assert isinstance(blocks[1], Paragraph)
        assert blocks[1].text == "This document describes the project goals."
        assert blocks[1].doc_range == (11, 60)

    def test_inline_formatting(self):
        body = [
            {
                "startIndex": 1,
                "endIndex": 30,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 1,
                            "endIndex": 5,
                            "textRun": {"content": "Hey ", "textStyle": {}},
                        },
                        {
                            "startIndex": 5,
                            "endIndex": 9,
                            "textRun": {"content": "bold", "textStyle": {"bold": True}},
                        },
                        {
                            "startIndex": 9,
                            "endIndex": 10,
                            "textRun": {"content": "\n", "textStyle": {}},
                        },
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                },
            }
        ]
        blocks = from_doc_json(body)
        assert len(blocks) == 1
        p = blocks[0]
        assert isinstance(p, Paragraph)
        assert p.spans[0] == Span("Hey ")
        assert p.spans[1] == Span("bold", bold=True)

    def test_skips_empty_paragraphs(self):
        body = [
            {
                "startIndex": 1,
                "endIndex": 2,
                "paragraph": {
                    "elements": [
                        {"startIndex": 1, "endIndex": 2, "textRun": {"content": "\n"}}
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                },
            },
            {
                "startIndex": 2,
                "endIndex": 10,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 2,
                            "endIndex": 10,
                            "textRun": {"content": "Content\n"},
                        }
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                },
            },
        ]
        blocks = from_doc_json(body)
        assert len(blocks) == 1
        assert blocks[0].text == "Content"

    def test_list_item(self):
        body = [
            {
                "startIndex": 1,
                "endIndex": 10,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 1,
                            "endIndex": 10,
                            "textRun": {"content": "item one\n"},
                        }
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "bullet": {"listId": "list1", "nestingLevel": 0},
                },
            }
        ]
        blocks = from_doc_json(body)
        assert len(blocks) == 1
        assert isinstance(blocks[0], ListItem)
        assert blocks[0].spans[0].text == "item one"

    def test_ordered_list_detection(self):
        lists = {
            "list1": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}
        }
        body = [
            {
                "startIndex": 1,
                "endIndex": 10,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 1,
                            "endIndex": 10,
                            "textRun": {"content": "item one\n"},
                        }
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "bullet": {"listId": "list1", "nestingLevel": 0},
                },
            }
        ]
        blocks = from_doc_json(body, lists=lists)
        assert blocks[0].ordered is True

    def test_table(self):
        body = [
            {
                "startIndex": 1,
                "endIndex": 50,
                "table": {
                    "tableRows": [
                        {
                            "tableCells": [
                                {
                                    "content": [
                                        {
                                            "paragraph": {
                                                "elements": [
                                                    {"textRun": {"content": "A\n"}}
                                                ]
                                            }
                                        }
                                    ]
                                },
                                {
                                    "content": [
                                        {
                                            "paragraph": {
                                                "elements": [
                                                    {"textRun": {"content": "B\n"}}
                                                ]
                                            }
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            "tableCells": [
                                {
                                    "content": [
                                        {
                                            "paragraph": {
                                                "elements": [
                                                    {"textRun": {"content": "1\n"}}
                                                ]
                                            }
                                        }
                                    ]
                                },
                                {
                                    "content": [
                                        {
                                            "paragraph": {
                                                "elements": [
                                                    {"textRun": {"content": "2\n"}}
                                                ]
                                            }
                                        }
                                    ]
                                },
                            ]
                        },
                    ]
                },
            }
        ]
        blocks = from_doc_json(body)
        assert len(blocks) == 1
        t = blocks[0]
        assert isinstance(t, Table)
        assert len(t.rows) == 2
        assert t.rows[0][0][0].text == "A"
        assert t._raw_table is not None

    def test_link_extraction(self):
        body = [
            {
                "startIndex": 1,
                "endIndex": 20,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 1,
                            "endIndex": 20,
                            "textRun": {
                                "content": "click here\n",
                                "textStyle": {"link": {"url": "https://example.com"}},
                            },
                        }
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                },
            }
        ]
        blocks = from_doc_json(body)
        assert blocks[0].spans[0].url == "https://example.com"


# =============================================================================
# Round-trip: from_markdown → render_markdown
# =============================================================================


class TestRoundTrip:
    def test_e2e_fixture_identity(self):
        """Critical test: e2e fixture round-trips without any diff."""
        fixture = (FIXTURES / "e2e_rich_formatting.md").read_text()
        blocks = from_markdown(fixture)
        rendered = render_markdown(blocks)
        assert rendered == fixture

    def test_simple_doc(self):
        md = "## Title\n\nA paragraph.\n\n- item 1\n- item 2\n"
        blocks = from_markdown(md)
        rendered = render_markdown(blocks)
        assert rendered == md

    def test_table_round_trip(self):
        md = "| A | B |\n| :---- | :---- |\n| 1 | 2 |\n"
        blocks = from_markdown(md)
        rendered = render_markdown(blocks)
        assert rendered == md

    def test_code_block_round_trip(self):
        md = "```python\nprint('hello')\n```\n"
        blocks = from_markdown(md)
        rendered = render_markdown(blocks)
        assert rendered == md


# =============================================================================
# to_tokens
# =============================================================================


class TestToTokens:
    def test_heading_token(self):
        blocks = [Heading(level=2, spans=[Span("Title")])]
        tokens = to_tokens(blocks)
        assert len(tokens) == 1
        assert tokens[0]["type"] == "heading"
        assert tokens[0]["attrs"]["level"] == 2

    def test_paragraph_token(self):
        blocks = [Paragraph(spans=[Span("Hello")])]
        tokens = to_tokens(blocks)
        assert tokens[0]["type"] == "paragraph"

    def test_list_grouping(self):
        blocks = [
            ListItem(spans=[Span("a")]),
            ListItem(spans=[Span("b")]),
        ]
        tokens = to_tokens(blocks)
        assert len(tokens) == 1
        assert tokens[0]["type"] == "list"
        assert len(tokens[0]["children"]) == 2

    def test_bold_span_nesting(self):
        blocks = [Paragraph(spans=[Span("x", bold=True, italic=True)])]
        tokens = to_tokens(blocks)
        # Should be: strong > emphasis > text
        child = tokens[0]["children"][0]
        assert child["type"] == "strong"
        assert child["children"][0]["type"] == "emphasis"
        assert child["children"][0]["children"][0]["type"] == "text"


# =============================================================================
# to_docs_requests
# =============================================================================


class TestToDocsRequests:
    def test_heading_request(self):
        blocks = [Heading(level=2, spans=[Span("Title")])]
        requests, tables, warnings = to_docs_requests(blocks, tab_id="t1")
        # Should have: insertText, updateParagraphStyle (HEADING_2)
        assert any("insertText" in r for r in requests)
        heading_reqs = [r for r in requests if "updateParagraphStyle" in r]
        assert len(heading_reqs) == 1
        assert (
            heading_reqs[0]["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
            == "HEADING_2"
        )
        assert heading_reqs[0]["updateParagraphStyle"]["range"]["tabId"] == "t1"

    def test_bold_request(self):
        blocks = [Paragraph(spans=[Span("bold", bold=True)])]
        requests, _, _ = to_docs_requests(blocks)
        bold_reqs = [
            r
            for r in requests
            if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"].get("bold")
        ]
        assert len(bold_reqs) == 1

    def test_table_request(self):
        blocks = [
            Table(
                rows=[
                    [
                        [Span("A")],
                        [Span("B")],
                    ],
                    [
                        [Span("1")],
                        [Span("2")],
                    ],
                ]
            )
        ]
        requests, tables, _ = to_docs_requests(blocks)
        insert_table = [r for r in requests if "insertTable" in r]
        assert len(insert_table) == 1
        assert insert_table[0]["insertTable"]["rows"] == 2
        assert insert_table[0]["insertTable"]["columns"] == 2
        assert len(tables) == 1

    def test_warnings(self):
        blocks = [ListItem(spans=[Span("nested")], depth=1)]
        _, _, warnings = to_docs_requests(blocks)
        assert len(warnings) == 1
        assert warnings[0].feature == "nested lists"


# =============================================================================
# check_unsupported
# =============================================================================


class TestCheckUnsupported:
    def test_clean_doc(self):
        blocks = [
            Heading(level=1, spans=[Span("Title")]),
            Paragraph(spans=[Span("text")]),
        ]
        assert check_unsupported(blocks) == []

    def test_nested_lists_warn(self):
        blocks = [ListItem(spans=[Span("a")], depth=1)]
        warnings = check_unsupported(blocks)
        assert len(warnings) == 1
        assert warnings[0].feature == "nested lists"

    def test_code_block_warns(self):
        blocks = [CodeBlock(code="x=1")]
        warnings = check_unsupported(blocks)
        assert len(warnings) == 1
        assert warnings[0].feature == "code blocks"

    def test_deduplication(self):
        blocks = [CodeBlock(code="a"), CodeBlock(code="b")]
        assert len(check_unsupported(blocks)) == 1
