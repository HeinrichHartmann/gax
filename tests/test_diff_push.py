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
    _splice_text_requests,
    _span_style_requests,
    ast_diff,
    compute_three_way_plan,
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
    """Build minimal Google Doc table JSON for testing.

    Matches real Google Docs API format: startIndex/endIndex live on the
    structural element wrapper, not inside the "paragraph" dict.
    """
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
                            "startIndex": idx,
                            "endIndex": cell_end,
                            "paragraph": {
                                "elements": [
                                    {
                                        "startIndex": idx,
                                        "endIndex": cell_end,
                                        "textRun": {"content": cell_text + "\n"},
                                    }
                                ],
                            },
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


# =============================================================================
# Three-way plan (ADR 034 §2)
# =============================================================================


class TestThreeWayPlan:
    """Tests for compute_three_way_plan (baseline-aware diff)."""

    def _store_baseline(self, body, lists=None):
        """Helper: store a baseline and return its hash."""
        from gax.store import store_baseline

        baseline_json = {"body": {"content": body}}
        if lists:
            baseline_json["lists"] = lists
        return store_baseline(baseline_json)

    def test_no_edit_produces_empty_plan(self):
        """No edits (local == base rendered) → empty plan."""
        from gax.gdoc.ir import render_markdown

        body = [_make_paragraph(1, "Hello world")]
        baseline_hash = self._store_baseline(body)

        # Render base to get local_markdown (no edits)
        base_blocks = from_doc_json(body)
        local_md = render_markdown(base_blocks)

        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=body,
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t.1",
        )

        assert plan.is_empty
        assert len(plan.mutations) == 0
        assert not plan.revision_changed

    def test_user_edit_produces_mutations(self):
        """User edits → plan has mutations."""
        body = [_make_paragraph(1, "Original text")]
        baseline_hash = self._store_baseline(body)

        # User edits the markdown
        local_md = "Changed text\n"

        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=body,
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t.1",
        )

        assert not plan.is_empty
        assert len(plan.mutations) > 0
        assert plan.error is None

    def test_revision_gate_same_rev(self):
        """Same revision → no conflict flag."""
        from gax.gdoc.ir import render_markdown

        body = [_make_paragraph(1, "Content")]
        baseline_hash = self._store_baseline(body)
        local_md = render_markdown(from_doc_json(body))

        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=body,
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t.1",
        )

        assert not plan.revision_changed

    def test_revision_gate_different_rev_no_conflict(self):
        """Different revision but disjoint changes → proceeds with warning."""
        body = [
            _make_paragraph(1, "First paragraph"),
            _make_paragraph(18, "Second paragraph"),
        ]
        baseline_hash = self._store_baseline(body)

        # User edits first paragraph
        local_md = "Edited first\n\nSecond paragraph\n"

        # Remote is unchanged (same body), but revision moved
        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=body,
            remote_revision="rev2",
            stored_revision="rev1",
            tab_id="t.1",
        )

        assert plan.revision_changed
        # No error because remote content didn't actually change
        # (remote renders same as base)
        assert plan.error is None
        assert not plan.is_empty

    def test_revision_gate_overlapping_conflict(self):
        """Overlapping remote change → error with conflict info."""
        body = [_make_paragraph(1, "Shared paragraph")]
        baseline_hash = self._store_baseline(body)

        # User edits the paragraph
        local_md = "User edited this\n"

        # Remote ALSO changed (different body content)
        remote_body = [_make_paragraph(1, "Collaborator changed this")]

        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=remote_body,
            remote_revision="rev2",
            stored_revision="rev1",
            tab_id="t.1",
        )

        assert plan.revision_changed
        assert plan.error is not None
        assert "Conflicting" in plan.error or "Remote changed" in plan.error
        assert len(plan.mutations) == 0

    def test_missing_baseline_falls_back_to_stateless(self):
        """Missing baseline → degrades to stateless diff."""
        body = [_make_paragraph(1, "Some content")]
        local_md = "Different content\n"

        plan = compute_three_way_plan(
            baseline_hash="sha256-nonexistent",
            local_markdown=local_md,
            remote_body=body,
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t.1",
        )

        # Should still produce a plan (stateless fallback)
        assert not plan.is_empty
        assert "stateless fallback" in plan.summary_lines[0]

    def test_empty_baseline_hash_falls_back(self):
        """Empty baseline hash → stateless fallback."""
        body = [_make_paragraph(1, "Some content")]
        local_md = "Different content\n"

        plan = compute_three_way_plan(
            baseline_hash="",
            local_markdown=local_md,
            remote_body=body,
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t.1",
        )

        # Empty hash → load_baseline returns None → fallback
        assert not plan.is_empty

    def test_drift_insert_above_shifts_range(self):
        """Remote inserted a block above the user's edit → mutation targets shifted range."""
        # Base: two paragraphs
        base_body = [
            _make_paragraph(1, "First paragraph"),
            _make_paragraph(18, "Second paragraph"),
        ]
        baseline_hash = self._store_baseline(base_body)

        # User edited the second paragraph
        local_md = "First paragraph\n\nEdited second\n"

        # Remote inserted a new block ABOVE the user's edit
        remote_body = [
            _make_paragraph(1, "First paragraph"),
            _make_paragraph(18, "Inserted by collaborator"),
            _make_paragraph(44, "Second paragraph"),
        ]

        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=remote_body,
            remote_revision="rev2",
            stored_revision="rev1",
            tab_id="t.1",
        )

        # Should produce mutations targeting the SHIFTED range (index 44+),
        # not the original range (index 18+)
        assert not plan.is_empty
        assert plan.error is None
        assert len(plan.mutations) > 0
        # Verify the mutation targets the correct (shifted) range
        for req in plan.mutations:
            if "deleteContentRange" in req:
                r = req["deleteContentRange"]["range"]
                # Must target the third paragraph's range (44+), not second (18+)
                assert r["startIndex"] >= 44, (
                    f"Mutation at {r['startIndex']} targets wrong range "
                    f"(should be >= 44, the shifted position)"
                )

    def test_drift_delete_above_shifts_range(self):
        """Remote deleted a block above the user's edit → mutation targets collapsed range."""
        # Base: three paragraphs
        base_body = [
            _make_paragraph(1, "First paragraph"),
            _make_paragraph(18, "Middle paragraph"),
            _make_paragraph(36, "Third paragraph"),
        ]
        baseline_hash = self._store_baseline(base_body)

        # User edited the third paragraph
        local_md = "First paragraph\n\nMiddle paragraph\n\nEdited third\n"

        # Remote deleted the middle paragraph — third moves up
        remote_body = [
            _make_paragraph(1, "First paragraph"),
            _make_paragraph(18, "Third paragraph"),
        ]

        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=remote_body,
            remote_revision="rev2",
            stored_revision="rev1",
            tab_id="t.1",
        )

        # Should produce mutations at the collapsed range (18+),
        # not the original (36+)
        assert not plan.is_empty
        assert plan.error is None
        assert len(plan.mutations) > 0
        for req in plan.mutations:
            if "deleteContentRange" in req:
                r = req["deleteContentRange"]["range"]
                assert r["startIndex"] >= 18, (
                    f"Mutation at {r['startIndex']} targets wrong range"
                )

    def test_drift_unmapped_user_edit_aborts(self):
        """User edited a block that is missing from remote → conflict abort.

        Uses same revision to bypass drift detection and exercise the
        alignment-based unmapped-edit guard directly.
        """
        # Base: two paragraphs
        base_body = [
            _make_paragraph(1, "First paragraph"),
            _make_paragraph(18, "Second paragraph"),
        ]
        baseline_hash = self._store_baseline(base_body)

        # User edited the second paragraph
        local_md = "First paragraph\n\nEdited second\n"

        # Remote has the second paragraph removed (structural mismatch)
        remote_body = [
            _make_paragraph(1, "First paragraph"),
        ]

        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=remote_body,
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t.1",
        )

        # Alignment cannot map user-edited block 1 → abort
        assert plan.error is not None
        assert "no longer exist" in plan.error
        assert len(plan.mutations) == 0

    def test_overlap_conflict_via_drift_detection(self):
        """User edited a block that remote also changed → drift conflict."""
        base_body = [
            _make_paragraph(1, "First paragraph"),
            _make_paragraph(18, "Second paragraph"),
        ]
        baseline_hash = self._store_baseline(base_body)

        local_md = "First paragraph\n\nEdited second\n"

        # Remote changed the same block (revision differs)
        remote_body = [
            _make_paragraph(1, "First paragraph"),
            _make_paragraph(18, "Remote edited second"),
        ]

        plan = compute_three_way_plan(
            baseline_hash=baseline_hash,
            local_markdown=local_md,
            remote_body=remote_body,
            remote_revision="rev2",
            stored_revision="rev1",
            tab_id="t.1",
        )

        assert plan.error is not None
        assert "Conflicting blocks" in plan.error
        assert plan.revision_changed


# =============================================================================
# Docs batchUpdate simulator
# =============================================================================


def _utf16_to_char(text: str, utf16_idx: int) -> int:
    """Convert a UTF-16 code-unit offset to a Python character index."""
    pos = 0
    for i, ch in enumerate(text):
        if pos >= utf16_idx:
            return i
        pos += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def _simulate_requests(text: str, requests: list[dict]) -> str:
    """Simulate Google Docs batchUpdate on a text buffer.

    Applies requests sequentially in order. Each request operates on the
    buffer AS IT EXISTS after all previous requests (matching Docs API
    semantics). Handles UTF-16 index conversion for emoji/surrogate-pair
    characters. Only processes deleteContentRange and insertText.
    """
    for req in requests:
        if "deleteContentRange" in req:
            r = req["deleteContentRange"]["range"]
            start = _utf16_to_char(text, r["startIndex"])
            end = _utf16_to_char(text, r["endIndex"])
            text = text[:start] + text[end:]
        elif "insertText" in req:
            loc = req["insertText"]["location"]
            idx = _utf16_to_char(text, loc["index"])
            ins = req["insertText"]["text"]
            text = text[:idx] + ins + text[idx:]
        # Skip style requests (they don't change text)

    return text


# =============================================================================
# Tests: multi-region splice correctness (gax-d75)
# =============================================================================


class TestSpliceMultiRegion:
    """Simulator-based tests for _splice_text_requests reverse ordering."""

    def _run_splice(self, base: str, new: str, block_start: int = 0):
        """Splice base→new, simulate, return result text."""
        # Prepend block_start chars to simulate document offset
        prefix = "X" * block_start
        doc_text = prefix + base

        reqs = _splice_text_requests(base, new, block_start, "t.1")
        result = _simulate_requests(doc_text, reqs)
        return result[block_start:]  # strip prefix

    def test_bead_repro_two_regions(self):
        """Exact repro from gax-d75: quick→slow + jumps→leaps."""
        base = "The quick brown fox jumps over the lazy dog"
        new = "The slow brown fox leaps over the lazy dog"
        assert self._run_splice(base, new) == new

    def test_two_regions_different_lengths(self):
        """Two replacements with different length changes."""
        base = "Hello World, Goodbye World"
        new = "Hi World, Bye World"
        assert self._run_splice(base, new) == new

    def test_three_regions(self):
        """Three changed regions in one paragraph."""
        base = "The big red car drove fast down the long road"
        new = "The small blue car crept slowly down the short road"
        assert self._run_splice(base, new) == new

    def test_emoji_multi_region(self):
        """Multi-region edit with emoji (multi-byte UTF-16)."""
        base = "Hello 🌍 world, goodbye 🌍 moon"
        new = "Howdy 🌎 world, farewell 🌎 moon"
        assert self._run_splice(base, new) == new

    def test_insert_and_delete_regions(self):
        """Mixed insert + delete across regions."""
        base = "AAA BBB CCC"
        new = "AAA DDD"
        assert self._run_splice(base, new) == new

    def test_block_offset_nonzero(self):
        """Splice with non-zero block_start (paragraph not at doc start)."""
        base = "The quick brown fox jumps over the lazy dog"
        new = "The slow brown fox leaps over the lazy dog"
        assert self._run_splice(base, new, block_start=100) == new

    def test_single_region_unchanged(self):
        """Single region edit still works after reverse-order refactor."""
        base = "Hello World"
        new = "Hello Earth"
        assert self._run_splice(base, new) == new

    def test_style_offsets_after_splice(self):
        """Style requests use final-text offsets, valid after all splices."""
        from gax.gdoc.ir import _utf16_len

        base = "The quick brown fox jumps"
        new = "The slow brown fox leaps"
        block_start = 10

        splice_reqs = _splice_text_requests(base, new, block_start, "t.1")
        # Spans for the new text
        spans = [
            Span(text="The "),
            Span(text="slow", bold=True),
            Span(text=" brown fox "),
            Span(text="leaps", italic=True),
        ]
        style_reqs = _span_style_requests(spans, block_start, "t.1")

        # All splice requests should precede (have higher or equal index than)
        # any style request at the same or lower position — verify indices
        # are within the block range
        for req in splice_reqs:
            if "deleteContentRange" in req:
                r = req["deleteContentRange"]["range"]
                assert r["startIndex"] >= block_start
                assert r["endIndex"] <= block_start + _utf16_len(base)
            elif "insertText" in req:
                loc = req["insertText"]["location"]
                assert loc["index"] >= block_start

        for req in style_reqs:
            if "updateTextStyle" in req:
                r = req["updateTextStyle"]["range"]
                assert r["startIndex"] >= block_start
                assert r["endIndex"] <= block_start + _utf16_len(new)
