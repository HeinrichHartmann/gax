"""Tests for diff-based push module."""

from gax.gdoc.md2docs import parse_markdown
from gax.gdoc.diff_push import (
    DocElement,
    AlignedNode,
    align,
    walk_doc_body,
    ast_diff,
    diff_to_mutations,
    _node_type,
)


# =============================================================================
# Alignment tests
# =============================================================================


class TestWalkDocBody:
    def test_classifies_heading(self):
        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "HEADING_2"},
                    "elements": [{"textRun": {"content": "My Heading\n"}}],
                },
                "startIndex": 10,
                "endIndex": 21,
            }
        ]
        elems = walk_doc_body(body)
        assert len(elems) == 1
        assert elems[0].type == "heading"
        assert elems[0].text == "My Heading"
        assert elems[0].details["level"] == 2

    def test_classifies_paragraph(self):
        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"textRun": {"content": "Hello world.\n"}}],
                },
                "startIndex": 1,
                "endIndex": 14,
            }
        ]
        elems = walk_doc_body(body)
        assert len(elems) == 1
        assert elems[0].type == "paragraph"
        assert elems[0].text == "Hello world."

    def test_classifies_empty(self):
        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"textRun": {"content": "\n"}}],
                },
                "startIndex": 14,
                "endIndex": 15,
            }
        ]
        elems = walk_doc_body(body)
        assert len(elems) == 1
        assert elems[0].type == "empty"

    def test_classifies_list_item(self):
        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "bullet": {"listId": "list1", "nestingLevel": 0},
                    "elements": [{"textRun": {"content": "Item one\n"}}],
                },
                "startIndex": 20,
                "endIndex": 29,
            }
        ]
        elems = walk_doc_body(body)
        assert len(elems) == 1
        assert elems[0].type == "list_item"
        assert elems[0].text == "Item one"

    def test_classifies_table(self):
        body = [
            {
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
                    ]
                },
                "startIndex": 100,
                "endIndex": 120,
            }
        ]
        elems = walk_doc_body(body)
        assert len(elems) == 1
        assert elems[0].type == "table"
        assert elems[0].details["num_rows"] == 1
        assert elems[0].details["num_cols"] == 2


class TestAlign:
    def _make_doc_elements(self, items):
        """Helper: create DocElement list from (type, text, start, end) tuples."""
        return [
            DocElement(type=t, text=txt, start_index=s, end_index=e)
            for t, txt, s, e in items
        ]

    def test_exact_match(self):
        doc = self._make_doc_elements(
            [
                ("heading", "Title", 1, 7),
                ("empty", "", 7, 8),
                ("paragraph", "Hello world.", 8, 21),
            ]
        )
        ast = parse_markdown("# Title\n\nHello world.\n")
        result = align(doc, ast)
        assert len(result) == 2
        assert result[0].node.text == "Title"
        assert result[0].start_index == 1
        assert result[1].start_index == 8

    def test_merged_paragraphs(self):
        """Two doc paragraphs merge into one AST paragraph."""
        doc = self._make_doc_elements(
            [
                ("paragraph", "Line one.", 1, 11),
                ("paragraph", "Line two.", 11, 21),
            ]
        )
        # Markdown without blank line between → single paragraph
        md = "Line one.\nLine two.\n"
        ast = parse_markdown(md)
        # mistune may parse this as one paragraph with softbreak
        assert len(ast) == 1
        result = align(doc, ast)
        assert len(result) == 1
        assert len(result[0].doc_elements) == 2
        assert result[0].start_index == 1
        assert result[0].end_index == 21

    def test_skips_empty(self):
        doc = self._make_doc_elements(
            [
                ("heading", "Title", 1, 7),
                ("empty", "", 7, 8),
                ("empty", "", 8, 9),
                ("paragraph", "Text.", 9, 15),
            ]
        )
        ast = parse_markdown("# Title\n\nText.\n")
        result = align(doc, ast)
        assert len(result) == 2

    def test_table_alignment(self):
        doc = self._make_doc_elements(
            [
                ("heading", "Data", 1, 6),
                ("empty", "", 6, 7),
                ("table", "[table 2x2]", 7, 50),
                ("empty", "", 50, 51),
            ]
        )
        ast = parse_markdown("# Data\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
        result = align(doc, ast)
        assert len(result) == 2
        assert _node_type(result[1].node) == "table"


# =============================================================================
# AST diff tests
# =============================================================================


class TestAstDiff:
    def test_no_changes(self):
        md = "# Title\n\nSome text.\n"
        nodes = parse_markdown(md)
        ops = ast_diff(nodes, nodes)
        assert ops == []

    def test_text_update(self):
        base = parse_markdown("# Title\n\nOld text.\n")
        edited = parse_markdown("# Title\n\nNew text.\n")
        ops = ast_diff(base, edited)
        assert len(ops) == 1
        assert ops[0].type == "update"
        assert ops[0].base_idx == 1
        assert ops[0].edit_idx == 1

    def test_heading_update(self):
        base = parse_markdown("# Old Title\n\nText.\n")
        edited = parse_markdown("# New Title\n\nText.\n")
        ops = ast_diff(base, edited)
        assert len(ops) == 1
        assert ops[0].type == "update"
        assert ops[0].base_idx == 0

    def test_insert_detected(self):
        base = parse_markdown("# Title\n\nText.\n")
        edited = parse_markdown("# Title\n\nNew paragraph.\n\nText.\n")
        ops = ast_diff(base, edited)
        inserts = [op for op in ops if op.type == "insert"]
        assert len(inserts) == 1

    def test_delete_detected(self):
        base = parse_markdown("# Title\n\nExtra.\n\nText.\n")
        edited = parse_markdown("# Title\n\nText.\n")
        ops = ast_diff(base, edited)
        deletes = [op for op in ops if op.type == "delete"]
        assert len(deletes) == 1

    def test_multiple_updates(self):
        base = parse_markdown("# Title\n\nPara one.\n\nPara two.\n")
        edited = parse_markdown("# Title\n\nChanged one.\n\nChanged two.\n")
        ops = ast_diff(base, edited)
        updates = [op for op in ops if op.type == "update"]
        assert len(updates) == 2

    def test_formatting_change_detected(self):
        base = parse_markdown("Some **bold** text.\n")
        edited = parse_markdown("Some *italic* text.\n")
        ops = ast_diff(base, edited)
        assert len(ops) == 1
        assert ops[0].type == "update"


# =============================================================================
# Mutation translation tests
# =============================================================================


class TestDiffToMutations:
    def _make_alignment(self, nodes, ranges):
        """Helper: create alignment from nodes and (start, end) tuples."""
        return [
            AlignedNode(
                node=n,
                doc_elements=[
                    DocElement(type="paragraph", text="", start_index=s, end_index=e)
                ],
                start_index=s,
                end_index=e,
            )
            for n, (s, e) in zip(nodes, ranges)
        ]

    def test_update_paragraph_text(self):
        base = parse_markdown("# Title\n\nOld text.\n")
        edited = parse_markdown("# Title\n\nNew text.\n")
        alignment = self._make_alignment(base, [(1, 7), (8, 18)])

        ops = ast_diff(base, edited)
        mutations = diff_to_mutations(ops, alignment, "tab1")

        # Should have: deleteContentRange, insertText (at minimum)
        types = [list(m.keys())[0] for m in mutations]
        assert "deleteContentRange" in types
        assert "insertText" in types

    def test_update_preserves_tab_id(self):
        base = parse_markdown("Old.\n")
        edited = parse_markdown("New.\n")
        alignment = self._make_alignment(base, [(1, 5)])

        ops = ast_diff(base, edited)
        mutations = diff_to_mutations(ops, alignment, "my_tab")

        for m in mutations:
            for key, val in m.items():
                if "range" in val:
                    assert val["range"]["tabId"] == "my_tab"
                elif "location" in val:
                    assert val["location"]["tabId"] == "my_tab"

    def test_insert_paragraph(self):
        base = parse_markdown("Text.\n")
        edited = parse_markdown("Text.\n\nNew paragraph.\n")
        ops = ast_diff(base, edited)

        alignment = self._make_alignment(base, [(1, 7)])
        mutations = diff_to_mutations(ops, alignment, "tab1")

        types = [list(m.keys())[0] for m in mutations]
        assert "insertText" in types
        # The inserted text should contain "New paragraph."
        insert_reqs = [m for m in mutations if "insertText" in m]
        assert any("New paragraph." in m["insertText"]["text"] for m in insert_reqs)

    def test_delete_paragraph(self):
        base = parse_markdown("Para one.\n\nPara two.\n")
        edited = parse_markdown("Para one.\n")
        ops = ast_diff(base, edited)

        alignment = self._make_alignment(base, [(1, 11), (12, 22)])
        mutations = diff_to_mutations(ops, alignment, "tab1")

        types = [list(m.keys())[0] for m in mutations]
        assert "deleteContentRange" in types
        # Should delete the range of "Para two."
        delete_reqs = [m for m in mutations if "deleteContentRange" in m]
        assert any(
            m["deleteContentRange"]["range"]["startIndex"] == 12 for m in delete_reqs
        )

    def test_insert_heading(self):
        base = parse_markdown("# Title\n\nText.\n")
        edited = parse_markdown("# Title\n\n## New Section\n\nText.\n")
        ops = ast_diff(base, edited)

        alignment = self._make_alignment(base, [(1, 7), (8, 14)])
        mutations = diff_to_mutations(ops, alignment, "tab1")

        # Should have insertText + updateParagraphStyle for heading
        types = [list(m.keys())[0] for m in mutations]
        assert "insertText" in types
        assert "updateParagraphStyle" in types

    def test_insert_list_item(self):
        base = parse_markdown("- Alpha\n- Beta\n")
        edited = parse_markdown("- Alpha\n- New item\n- Beta\n")
        ops = ast_diff(base, edited)

        alignment = self._make_alignment(base, [(1, 8), (8, 14)])
        mutations = diff_to_mutations(ops, alignment, "tab1")

        types = [list(m.keys())[0] for m in mutations]
        assert "insertText" in types
        assert "createParagraphBullets" in types

    def test_heading_level_change(self):
        base = parse_markdown("## Subheading\n")
        edited = parse_markdown("### Subheading\n")
        alignment = self._make_alignment(base, [(1, 13)])

        ops = ast_diff(base, edited)
        mutations = diff_to_mutations(ops, alignment, "tab1")

        # Should include updateParagraphStyle for heading level
        style_updates = [m for m in mutations if "updateParagraphStyle" in m]
        assert len(style_updates) >= 1
        ps = style_updates[0]["updateParagraphStyle"]
        assert ps["paragraphStyle"]["namedStyleType"] == "HEADING_3"

    def test_bold_formatting(self):
        base = parse_markdown("Plain text.\n")
        edited = parse_markdown("Plain **bold** text.\n")
        alignment = self._make_alignment(base, [(1, 13)])

        ops = ast_diff(base, edited)
        mutations = diff_to_mutations(ops, alignment, "tab1")

        bold_updates = [
            m
            for m in mutations
            if "updateTextStyle" in m
            and m["updateTextStyle"].get("textStyle", {}).get("bold")
        ]
        assert len(bold_updates) >= 1


# =============================================================================
# Table update tests
# =============================================================================


def _make_doc_table_json(rows_text):
    """Build a minimal Google Doc table JSON from a list of list of strings."""
    idx = 100  # arbitrary start
    table_rows = []
    for row in rows_text:
        cells = []
        for cell_text in row:
            para_start = idx
            para_end = idx + len(cell_text) + 1  # +1 for newline
            cells.append(
                {
                    "content": [
                        {
                            "paragraph": {
                                "elements": [
                                    {"textRun": {"content": cell_text + "\n"}}
                                ],
                                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                            },
                            "startIndex": para_start,
                            "endIndex": para_end,
                        }
                    ]
                }
            )
            idx = para_end + 1
        table_rows.append({"tableCells": cells})
    return {
        "table": {"tableRows": table_rows},
        "startIndex": 100,
        "endIndex": idx,
    }


class TestTableUpdates:
    def _make_table_alignment(self, base_node, doc_table_json):
        doc_elem = DocElement(
            type="table",
            text="[table]",
            start_index=doc_table_json["startIndex"],
            end_index=doc_table_json["endIndex"],
            raw=doc_table_json,
        )
        return [
            AlignedNode(
                node=base_node,
                doc_elements=[doc_elem],
                start_index=doc_elem.start_index,
                end_index=doc_elem.end_index,
            )
        ]

    def test_cell_text_update(self):
        base = parse_markdown("| A | B |\n|---|---|\n| old | keep |\n")
        edited = parse_markdown("| A | B |\n|---|---|\n| new | keep |\n")

        base_table = [n for n in base if _node_type(n) == "table"][0]
        doc_json = _make_doc_table_json([["A", "B"], ["old", "keep"]])
        alignment = self._make_table_alignment(base_table, doc_json)

        ops = ast_diff(base, edited)
        mutations = diff_to_mutations(ops, alignment, "tab1")

        # Should delete old + insert new for the changed cell
        types = [list(m.keys())[0] for m in mutations]
        assert "deleteContentRange" in types
        assert "insertText" in types

        # "keep" cell should NOT be touched
        insert_texts = [m["insertText"]["text"] for m in mutations if "insertText" in m]
        assert "new" in insert_texts
        assert "keep" not in insert_texts

    def test_cell_formatting_update(self):
        base = parse_markdown("| plain |\n|---|\n| text |\n")
        edited = parse_markdown("| plain |\n|---|\n| **text** |\n")

        base_table = [n for n in base if _node_type(n) == "table"][0]
        doc_json = _make_doc_table_json([["plain"], ["text"]])
        alignment = self._make_table_alignment(base_table, doc_json)

        ops = ast_diff(base, edited)
        mutations = diff_to_mutations(ops, alignment, "tab1")

        bold = [
            m
            for m in mutations
            if "updateTextStyle" in m
            and m["updateTextStyle"].get("textStyle", {}).get("bold")
        ]
        assert len(bold) >= 1

    def test_unchanged_table_no_ops(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
        base = parse_markdown(md)
        edited = parse_markdown(md)

        ops = ast_diff(base, edited)
        assert ops == []

    def test_row_count_change_raises(self):
        import pytest

        base = parse_markdown("| A |\n|---|\n| 1 |\n")
        edited = parse_markdown("| A |\n|---|\n| 1 |\n| 2 |\n")

        base_table = [n for n in base if _node_type(n) == "table"][0]
        doc_json = _make_doc_table_json([["A"], ["1"]])
        alignment = self._make_table_alignment(base_table, doc_json)

        ops = ast_diff(base, edited)
        with pytest.raises(ValueError, match="row count changed"):
            diff_to_mutations(ops, alignment, "tab1")

    def test_multi_paragraph_cell_raises(self):
        import pytest

        base = parse_markdown("| A |\n|---|\n| old |\n")
        edited = parse_markdown("| A |\n|---|\n| new |\n")

        base_table = [n for n in base if _node_type(n) == "table"][0]

        # Build a doc JSON where the data cell has 2 paragraphs
        doc_json = _make_doc_table_json([["A"], ["old"]])
        # Manually add a second paragraph to cell[1][0]
        cell = doc_json["table"]["tableRows"][1]["tableCells"][0]
        cell["content"].append(
            {
                "paragraph": {
                    "elements": [{"textRun": {"content": "extra line\n"}}],
                },
                "startIndex": 200,
                "endIndex": 211,
            }
        )

        alignment = self._make_table_alignment(base_table, doc_json)
        ops = ast_diff(base, edited)
        with pytest.raises(ValueError, match="multi-paragraph"):
            diff_to_mutations(ops, alignment, "tab1")
