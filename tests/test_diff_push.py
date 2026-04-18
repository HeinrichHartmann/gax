"""Tests for diff-based push (experimental --patch mode).

Tests the Block/Span IR-based diff pipeline: from_doc_json → ast_diff →
diff_to_mutations. No alignment step — doc_range comes from construction.
"""

import pytest

from gax.gdoc.ir import (
    Block,
    Heading,
    ListItem,
    Paragraph,
    Span,
    Table,
    from_doc_json,
)
from gax.gdoc.diff_push import (
    EditOp,
    ast_diff,
    diff_to_mutations,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_paragraph(start, text, style="NORMAL_TEXT"):
    end = start + len(text) + 1
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


def _make_blocks_with_range(*specs) -> list[Block]:
    """Create Block list with doc_range set.

    specs: tuples of (type, text, start, end) or (type, text, start, end, extra)
    """
    blocks: list[Block] = []
    for spec in specs:
        btype, text, start, end = spec[:4]
        dr = (start, end)
        if btype == "paragraph":
            blocks.append(Paragraph(doc_range=dr, spans=[Span(text)]))
        elif btype == "heading":
            level = spec[4] if len(spec) > 4 else 2
            blocks.append(Heading(doc_range=dr, level=level, spans=[Span(text)]))
        elif btype == "list_item":
            blocks.append(ListItem(doc_range=dr, spans=[Span(text)]))
    return blocks


def _make_doc_table_json(rows_data, start_index=1):
    """Build minimal Google Doc table JSON for testing."""
    table_rows = []
    idx = start_index
    for row in rows_data:
        cells = []
        for cell_text in row:
            cell_end = idx + len(cell_text) + 1
            cells.append(
                {
                    "content": [
                        {
                            "paragraph": {
                                "startIndex": idx,
                                "endIndex": cell_end,
                                "elements": [
                                    {"textRun": {"content": cell_text + "\n"}}
                                ],
                            }
                        }
                    ]
                }
            )
            idx = cell_end
        table_rows.append({"tableCells": cells})

    return {
        "startIndex": start_index,
        "endIndex": idx,
        "table": {"tableRows": table_rows},
    }


# =============================================================================
# from_doc_json (replaces TestWalkDocBody)
# =============================================================================


class TestFromDocJson:
    def test_classifies_heading(self):
        body = [_make_paragraph(1, "Title", style="HEADING_2")]
        blocks = from_doc_json(body)
        assert len(blocks) == 1
        assert isinstance(blocks[0], Heading)
        assert blocks[0].level == 2
        assert blocks[0].text == "Title"
        assert blocks[0].doc_range == (1, 7)

    def test_classifies_paragraph(self):
        body = [_make_paragraph(10, "Hello world")]
        blocks = from_doc_json(body)
        assert len(blocks) == 1
        assert isinstance(blocks[0], Paragraph)
        assert blocks[0].text == "Hello world"

    def test_skips_empty(self):
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
            _make_paragraph(2, "Content"),
        ]
        blocks = from_doc_json(body)
        assert len(blocks) == 1
        assert blocks[0].text == "Content"

    def test_classifies_list_item(self):
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

    def test_classifies_table(self):
        table_json = _make_doc_table_json([["A", "B"], ["1", "2"]])
        blocks = from_doc_json([table_json])
        assert len(blocks) == 1
        assert isinstance(blocks[0], Table)
        assert len(blocks[0].rows) == 2
        assert blocks[0]._raw_table is not None


# =============================================================================
# AST Diff
# =============================================================================


class TestAstDiff:
    def test_no_changes(self):
        blocks = [Paragraph(spans=[Span("hello")])]
        ops = ast_diff(blocks, list(blocks))
        assert len(ops) == 0

    def test_text_update(self):
        base = [Paragraph(spans=[Span("hello")])]
        edited = [Paragraph(spans=[Span("world")])]
        ops = ast_diff(base, edited)
        assert len(ops) == 1
        assert ops[0].type == "update"

    def test_heading_update(self):
        base = [Heading(level=2, spans=[Span("Old")])]
        edited = [Heading(level=2, spans=[Span("New")])]
        ops = ast_diff(base, edited)
        assert len(ops) == 1
        assert ops[0].type == "update"

    def test_insert_detected(self):
        base = [Paragraph(spans=[Span("a")])]
        edited = [Paragraph(spans=[Span("a")]), Paragraph(spans=[Span("b")])]
        ops = ast_diff(base, edited)
        assert any(op.type == "insert" for op in ops)

    def test_delete_detected(self):
        base = [Paragraph(spans=[Span("a")]), Paragraph(spans=[Span("b")])]
        edited = [Paragraph(spans=[Span("a")])]
        ops = ast_diff(base, edited)
        assert any(op.type == "delete" for op in ops)

    def test_multiple_updates(self):
        base = [Paragraph(spans=[Span("a")]), Paragraph(spans=[Span("b")])]
        edited = [Paragraph(spans=[Span("x")]), Paragraph(spans=[Span("y")])]
        ops = ast_diff(base, edited)
        assert len(ops) == 2
        assert all(op.type == "update" for op in ops)

    def test_formatting_change_detected(self):
        base = [Paragraph(spans=[Span("text", bold=True)])]
        edited = [Paragraph(spans=[Span("text", italic=True)])]
        ops = ast_diff(base, edited)
        assert len(ops) == 1
        assert ops[0].type == "update"


# =============================================================================
# diff_to_mutations
# =============================================================================


class TestDiffToMutations:
    def test_update_paragraph_text(self):
        base = _make_blocks_with_range(("paragraph", "old text", 10, 20))
        edited = [Paragraph(spans=[Span("new text")])]
        ops = [EditOp("update", 0, 0, base[0], edited[0])]

        mutations = diff_to_mutations(ops, base, "t1")
        assert any("deleteContentRange" in m for m in mutations)
        assert any("insertText" in m for m in mutations)

    def test_update_preserves_tab_id(self):
        base = _make_blocks_with_range(("paragraph", "text", 5, 15))
        edited = [Paragraph(spans=[Span("new")])]
        ops = [EditOp("update", 0, 0, base[0], edited[0])]

        mutations = diff_to_mutations(ops, base, "tab123")
        for m in mutations:
            for val in m.values():
                if isinstance(val, dict):
                    r = val.get("range") or val.get("location")
                    if r:
                        assert r.get("tabId") == "tab123"

    def test_insert_paragraph(self):
        base = _make_blocks_with_range(("paragraph", "existing", 5, 15))
        new_para = Paragraph(spans=[Span("inserted")])
        ops = [EditOp("insert", None, 0, None, new_para, insert_after=0)]

        mutations = diff_to_mutations(ops, base, "t1")
        insert_reqs = [m for m in mutations if "insertText" in m]
        assert len(insert_reqs) == 1
        assert "inserted" in insert_reqs[0]["insertText"]["text"]

    def test_delete_paragraph(self):
        base = _make_blocks_with_range(("paragraph", "to delete", 10, 25))
        ops = [EditOp("delete", 0, None, base[0], None)]

        mutations = diff_to_mutations(ops, base, "t1")
        delete_reqs = [m for m in mutations if "deleteContentRange" in m]
        assert len(delete_reqs) == 1
        r = delete_reqs[0]["deleteContentRange"]["range"]
        assert r["startIndex"] == 10
        assert r["endIndex"] == 25

    def test_insert_heading(self):
        base = _make_blocks_with_range(("paragraph", "text", 5, 15))
        new_heading = Heading(level=2, spans=[Span("New Section")])
        ops = [EditOp("insert", None, 0, None, new_heading, insert_after=0)]

        mutations = diff_to_mutations(ops, base, "t1")
        style_reqs = [m for m in mutations if "updateParagraphStyle" in m]
        assert len(style_reqs) == 1
        assert (
            style_reqs[0]["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
            == "HEADING_2"
        )

    def test_insert_list_item(self):
        base = _make_blocks_with_range(("paragraph", "text", 5, 15))
        new_item = ListItem(spans=[Span("bullet")])
        ops = [EditOp("insert", None, 0, None, new_item, insert_after=0)]

        mutations = diff_to_mutations(ops, base, "t1")
        bullet_reqs = [m for m in mutations if "createParagraphBullets" in m]
        assert len(bullet_reqs) == 1

    def test_heading_level_change(self):
        base = _make_blocks_with_range(("heading", "Title", 5, 15, 2))
        edited = [Heading(level=3, spans=[Span("Title")])]
        ops = [EditOp("update", 0, 0, base[0], edited[0])]

        mutations = diff_to_mutations(ops, base, "t1")
        style_reqs = [m for m in mutations if "updateParagraphStyle" in m]
        assert any(
            r["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"] == "HEADING_3"
            for r in style_reqs
        )

    def test_bold_formatting(self):
        base = _make_blocks_with_range(("paragraph", "text", 5, 15))
        edited = [Paragraph(spans=[Span("text", bold=True)])]
        ops = [EditOp("update", 0, 0, base[0], edited[0])]

        mutations = diff_to_mutations(ops, base, "t1")
        bold_reqs = [
            m
            for m in mutations
            if "updateTextStyle" in m and m["updateTextStyle"]["textStyle"].get("bold")
        ]
        assert len(bold_reqs) == 1

    def test_heading_demotion_resets_style(self):
        """Issue #1: Heading → Paragraph must reset namedStyleType to NORMAL_TEXT."""
        base = _make_blocks_with_range(("heading", "Title", 5, 15, 2))
        edited = [Paragraph(spans=[Span("Title")])]
        ops = [EditOp("update", 0, 0, base[0], edited[0])]

        mutations = diff_to_mutations(ops, base, "t1")
        style_reqs = [m for m in mutations if "updateParagraphStyle" in m]
        assert len(style_reqs) == 1
        assert (
            style_reqs[0]["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
            == "NORMAL_TEXT"
        )


# =============================================================================
# Table updates
# =============================================================================


class TestTableUpdates:
    def _make_table_pair(self, base_data, edit_data):
        """Create base Table (with doc_range and _raw_table) and edited Table."""
        raw = _make_doc_table_json(base_data)
        base_blocks = from_doc_json([raw])
        assert len(base_blocks) == 1
        base_table = base_blocks[0]
        assert isinstance(base_table, Table)

        edit_rows = []
        for row in edit_data:
            edit_rows.append([[Span(cell)] for cell in row])
        edit_table = Table(rows=edit_rows)

        return base_table, edit_table

    def test_cell_text_update(self):
        base, edit = self._make_table_pair(
            [["A", "B"], ["1", "2"]],
            [["A", "B"], ["X", "2"]],
        )
        ops = [EditOp("update", 0, 0, base, edit)]
        mutations = diff_to_mutations(ops, [base], "t1")

        delete_reqs = [m for m in mutations if "deleteContentRange" in m]
        insert_reqs = [m for m in mutations if "insertText" in m]
        assert len(delete_reqs) >= 1
        assert any("X" in m["insertText"]["text"] for m in insert_reqs)

    def test_cell_formatting_update(self):
        base, _ = self._make_table_pair(
            [["A", "B"], ["text", "2"]],
            [["A", "B"], ["text", "2"]],
        )
        edit_rows = [
            [[Span("A")], [Span("B")]],
            [[Span("text", bold=True)], [Span("2")]],
        ]
        edit = Table(rows=edit_rows)
        ops = [EditOp("update", 0, 0, base, edit)]
        mutations = diff_to_mutations(ops, [base], "t1")

        bold_reqs = [
            m
            for m in mutations
            if "updateTextStyle" in m and m["updateTextStyle"]["textStyle"].get("bold")
        ]
        assert len(bold_reqs) == 1

    def test_unchanged_table_no_ops(self):
        base, edit = self._make_table_pair(
            [["A", "B"], ["1", "2"]],
            [["A", "B"], ["1", "2"]],
        )
        ops = ast_diff([base], [edit])
        assert len(ops) == 0

    def test_row_count_change_raises(self):
        base, edit = self._make_table_pair(
            [["A", "B"], ["1", "2"]],
            [["A", "B"], ["1", "2"], ["3", "4"]],
        )
        ops = [EditOp("update", 0, 0, base, edit)]
        with pytest.raises(ValueError, match="row count"):
            diff_to_mutations(ops, [base], "t1")

    def test_multi_paragraph_cell_raises(self):
        """Table cells with multiple paragraphs raise ValueError."""
        raw = {
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
                                            "startIndex": 1,
                                            "endIndex": 10,
                                            "elements": [
                                                {"textRun": {"content": "line 1\n"}}
                                            ],
                                        }
                                    },
                                    {
                                        "paragraph": {
                                            "startIndex": 10,
                                            "endIndex": 20,
                                            "elements": [
                                                {"textRun": {"content": "line 2\n"}}
                                            ],
                                        }
                                    },
                                ]
                            }
                        ]
                    }
                ]
            },
        }
        base_blocks = from_doc_json([raw])
        base_table = base_blocks[0]
        assert isinstance(base_table, Table)

        edit = Table(rows=[[[Span("changed")]]])
        ops = [EditOp("update", 0, 0, base_table, edit)]
        with pytest.raises(ValueError, match="multi-paragraph"):
            diff_to_mutations(ops, [base_table], "t1")
