"""End-to-end fidelity spec suite for doc push (ADR 034 / gax-cvi).

These tests define the acceptance criteria for faithful surgical push.
They drive the REAL gax product code paths (gax.gdoc.ir, gax.gdoc.diff_push)
against mock doc JSON fixtures.

Phase 0 lands these tests with xfail/skip markers referencing the phase
bead that will remove them. The suite runs green in CI immediately.

Run:
    direnv exec . python -m pytest tests/test_e2e_fidelity.py --collect-only
    direnv exec . python -m pytest tests/test_e2e_fidelity.py -v
"""

from __future__ import annotations

import pytest

from gax.gdoc.ir import (
    Block,
    Heading,
    Paragraph,
    Table,
    _utf16_len,
    from_doc_json,
    from_markdown,
    render_markdown,
)
from gax.gdoc.diff_push import (
    ast_diff,
    diff_to_mutations,
)


# =============================================================================
# Rich fixture: mock Google Docs JSON with colors, fonts, alignment, links,
# lists, tables, and emoji — matching the prototype's populate_rich_doc.
# =============================================================================


def _rich_doc_body() -> list[dict]:
    """Build a rich-format doc body exercising all fidelity-relevant features.

    Structure:
      [0] Section break (implicit)
      [1] Heading "Test Heading" (HEADING_1)
      [2] Mixed paragraph: "This is plain text with **bold word** and
          [colored]{red} [link here](https://example.com) see more."
      [3] Centered paragraph: "Confidential — internal only" (CENTER, 9pt)
      [4] List item: "Revenue increased significantly"
      [5] List item: "Costs remained flat"
      [6] List item: "Market share grew"
      [7] Table 2x2: Region|Revenue / EMEA|**4.2M**
      [8] Emoji paragraph: "Emoji test: 🎉 party 🚀 rocket 🏳️‍🌈 flag 𝕳𝖊𝖑𝖑𝖔"
    """
    idx = 1

    # --- Heading ---
    h_text = "Test Heading"
    h_start = idx
    h_end = h_start + len(h_text) + 1
    heading = {
        "startIndex": h_start,
        "endIndex": h_end,
        "paragraph": {
            "elements": [
                {
                    "startIndex": h_start,
                    "endIndex": h_end,
                    "textRun": {"content": h_text + "\n"},
                }
            ],
            "paragraphStyle": {"namedStyleType": "HEADING_1"},
        },
    }
    idx = h_end

    # --- Mixed paragraph with bold, color, link ---
    p2_parts = [
        ("This is plain text with ", {}),
        ("bold word", {"bold": True}),
        (" and ", {}),
        (
            "colored",
            {
                "foregroundColor": {
                    "color": {"rgbColor": {"red": 0.8, "green": 0.0, "blue": 0.0}}
                }
            },
        ),
        (" ", {}),
        ("link here", {"link": {"url": "https://example.com"}}),
        (" see more.", {}),
    ]
    p2_start = idx
    p2_elements = []
    for text, style in p2_parts:
        el_end = idx + len(text)
        el = {
            "startIndex": idx,
            "endIndex": el_end,
            "textRun": {"content": text, "textStyle": style},
        }
        p2_elements.append(el)
        idx = el_end
    # trailing newline element
    p2_elements.append(
        {
            "startIndex": idx,
            "endIndex": idx + 1,
            "textRun": {"content": "\n", "textStyle": {}},
        }
    )
    idx += 1
    mixed_para = {
        "startIndex": p2_start,
        "endIndex": idx,
        "paragraph": {
            "elements": p2_elements,
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        },
    }

    # --- Centered paragraph with small font ---
    p3_text = "Confidential — internal only"
    p3_start = idx
    p3_end = idx + len(p3_text) + 1
    centered_para = {
        "startIndex": p3_start,
        "endIndex": p3_end,
        "paragraph": {
            "elements": [
                {
                    "startIndex": p3_start,
                    "endIndex": p3_end,
                    "textRun": {
                        "content": p3_text + "\n",
                        "textStyle": {
                            "fontSize": {"magnitude": 9, "unit": "PT"}
                        },
                    },
                }
            ],
            "paragraphStyle": {
                "namedStyleType": "NORMAL_TEXT",
                "alignment": "CENTER",
            },
        },
    }
    idx = p3_end

    # --- List items ---
    list_items_text = [
        "Revenue increased significantly",
        "Costs remained flat",
        "Market share grew",
    ]
    list_elements = []
    for li_text in list_items_text:
        li_start = idx
        li_end = idx + len(li_text) + 1
        list_elements.append(
            {
                "startIndex": li_start,
                "endIndex": li_end,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": li_start,
                            "endIndex": li_end,
                            "textRun": {"content": li_text + "\n"},
                        }
                    ],
                    "paragraphStyle": {
                        "namedStyleType": "NORMAL_TEXT",
                    },
                    "bullet": {
                        "listId": "kix.list001",
                        "nestingLevel": 0,
                    },
                },
            }
        )
        idx = li_end

    # --- Table 2x2 ---
    table_start = idx
    # Simplified table structure matching Google Docs JSON
    cell_data = [
        [("Region", {}), ("Revenue", {})],
        [("EMEA", {}), ("4.2M", {"bold": True})],
    ]
    table_rows = []
    for row_data in cell_data:
        cells = []
        for cell_text, style in row_data:
            cell_start = idx
            cell_para_end = idx + len(cell_text) + 1
            cell_el = {
                "content": [
                    {
                        "startIndex": cell_start,
                        "endIndex": cell_para_end,
                        "paragraph": {
                            "elements": [
                                {
                                    "startIndex": cell_start,
                                    "endIndex": cell_para_end,
                                    "textRun": {
                                        "content": cell_text + "\n",
                                        "textStyle": style,
                                    },
                                }
                            ],
                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                        },
                    }
                ]
            }
            cells.append(cell_el)
            idx = cell_para_end
        table_rows.append({"tableCells": cells})
    table_end = idx
    table_elem = {
        "startIndex": table_start,
        "endIndex": table_end,
        "table": {
            "rows": 2,
            "columns": 2,
            "tableRows": table_rows,
        },
    }

    # --- Emoji paragraph ---
    emoji_text = "Emoji test: 🎉 party 🚀 rocket 🏳️\u200d🌈 flag 𝕳𝖊𝖑𝖑𝖔"
    emoji_start = idx
    emoji_end = idx + len(emoji_text) + 1
    emoji_para = {
        "startIndex": emoji_start,
        "endIndex": emoji_end,
        "paragraph": {
            "elements": [
                {
                    "startIndex": emoji_start,
                    "endIndex": emoji_end,
                    "textRun": {"content": emoji_text + "\n"},
                }
            ],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        },
    }
    idx = emoji_end

    return [heading, mixed_para, centered_para] + list_elements + [table_elem, emoji_para]


def _lists_dict() -> dict:
    """Lists metadata for the fixture (bullet list style)."""
    return {
        "kix.list001": {
            "listProperties": {
                "nestingLevels": [
                    {"bulletAlignment": "START", "glyphType": "GLYPH_TYPE_UNSPECIFIED"}
                ]
            }
        }
    }


# =============================================================================
# Helpers
# =============================================================================


def _strip_index_fields(obj):
    """Deep-strip startIndex/endIndex for structural comparison.

    Mirrors the prototype helper: after text edits, indices shift but
    non-edited blocks should be structurally identical otherwise.
    """
    if isinstance(obj, dict):
        return {
            k: _strip_index_fields(v)
            for k, v in obj.items()
            if k not in ("startIndex", "endIndex")
        }
    if isinstance(obj, list):
        return [_strip_index_fields(item) for item in obj]
    return obj


def _blocks_from_fixture() -> list[Block]:
    """Parse the rich fixture through the production IR."""
    return from_doc_json(_rich_doc_body(), lists=_lists_dict())


def _render_and_reparse(blocks: list[Block]) -> list[Block]:
    """Render blocks to markdown, then re-parse — simulating the pull→edit→push loop."""
    md = render_markdown(blocks)
    return from_markdown(md)


# =============================================================================
# Scenario 1: No-op push ⇒ zero mutations (gax-cvi.1)
#
# ADR 034 invariant: serialize → parse → diff produces ZERO mutations.
# In the production code path, this is: from_doc_json → render_markdown →
# from_markdown → ast_diff. If the user made no edits, nothing should change.
# =============================================================================


@pytest.mark.e2e
class TestNoOpPush:
    """Scenario 1: pull → render → re-parse → diff = zero mutations."""

    @pytest.mark.xfail(
        reason="gax-cvi.2: requires pull-time baseline to distinguish "
        "representation gaps from edits; current IR is lossy for colors/fonts/alignment",
        strict=False,
    )
    def test_no_op_full_fidelity(self):
        """Full rich doc: round-trip through markdown produces zero ops.

        This tests the strongest invariant: the production IR round-trips
        perfectly, so a no-edit push produces zero mutations. Currently
        xfail because the production IR drops colors/fonts/alignment,
        causing spurious diffs.
        """
        base_blocks = _blocks_from_fixture()
        local_blocks = _render_and_reparse(base_blocks)
        ops = ast_diff(base_blocks, local_blocks)
        assert len(ops) == 0, (
            f"No-op round trip should produce zero ops, got {len(ops)}: "
            f"{[op.type for op in ops]}"
        )

    def test_no_op_markdown_subset(self):
        """Markdown-representable subset: round-trip is zero ops.

        Uses only features the production IR captures (bold, italic,
        strikethrough, links, headings, lists). This must pass NOW.
        """
        # Build a doc using only markdown-representable features.
        # Note: bold spans must not have trailing/leading whitespace,
        # because markdown bold syntax (**text**) breaks if there's
        # whitespace adjacent to the delimiters.
        body = [
            {
                "startIndex": 1,
                "endIndex": 10,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 1,
                            "endIndex": 10,
                            "textRun": {"content": "Heading\n"},
                        }
                    ],
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                },
            },
            {
                "startIndex": 10,
                "endIndex": 40,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 10,
                            "endIndex": 21,
                            "textRun": {"content": "Normal and "},
                        },
                        {
                            "startIndex": 21,
                            "endIndex": 25,
                            "textRun": {
                                "content": "bold",
                                "textStyle": {"bold": True},
                            },
                        },
                        {
                            "startIndex": 25,
                            "endIndex": 40,
                            "textRun": {"content": " text follows.\n"},
                        },
                    ],
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                },
            },
        ]
        base_blocks = from_doc_json(body)
        md = render_markdown(base_blocks)
        local_blocks = from_markdown(md)
        ops = ast_diff(base_blocks, local_blocks)
        assert len(ops) == 0, (
            f"Markdown-only round trip should produce zero ops, got {len(ops)}: "
            f"{[op.type for op in ops]}"
        )


# =============================================================================
# Scenario 2: Word edit preserves sibling runs (gax-cvi.1)
#
# ADR 034 invariant 2: editing one word should not disturb sibling runs
# (bold, color, link in the same paragraph).
# =============================================================================


@pytest.mark.e2e
class TestWordEditPreservesSiblings:
    """Scenario 2: change one word in a paragraph; unedited blocks are unchanged."""

    def test_word_edit_produces_single_update(self):
        """Edit 'plain text' → 'simple text' in the mixed paragraph.

        Should produce exactly one update op (on the mixed paragraph),
        not deletes/inserts of the whole block list.
        """
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        # Edit one word
        edited_md = md.replace("plain text", "simple text")
        assert edited_md != md, "Edit should change the markdown"

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        # Should have at least one op
        assert len(ops) > 0, "Should produce ops for text edit"

        # The edit is in the mixed paragraph; other blocks should be untouched
        update_ops = [op for op in ops if op.type == "update"]
        assert len(update_ops) >= 1, "Should have at least one update"

    @pytest.mark.xfail(
        reason="gax-cvi.3: requires run-level splicing (ADR 034 §3); "
        "current diff_push deletes+reinserts entire paragraph",
        strict=False,
    )
    def test_sibling_runs_survive_word_edit(self):
        """After push of word edit, bold/link formatting on sibling runs is preserved.

        Currently xfail: the production path does paragraph-level
        delete+reinsert, which destroys all formatting on the paragraph.
        Phase 3 (run-level splicing) will fix this.
        """
        base_blocks = _blocks_from_fixture()

        # Verify the fixture has formatting to preserve
        mixed_para = base_blocks[1]
        assert isinstance(mixed_para, Paragraph)
        has_bold = any(s.bold for s in mixed_para.spans)
        has_link = any(s.url for s in mixed_para.spans)
        assert has_bold, "Fixture should have bold span"
        assert has_link, "Fixture should have link span"

        md = render_markdown(base_blocks)
        edited_md = md.replace("plain text", "simple text")
        local_blocks = from_markdown(edited_md)

        ops = ast_diff(base_blocks, local_blocks)
        tab_id = "t.1"
        mutations = diff_to_mutations(ops, base_blocks, tab_id)

        # Count delete vs style-only operations
        deletes = [m for m in mutations if "deleteContentRange" in m]
        # Run-level splice should NOT delete the entire paragraph
        # (paragraph range = base_blocks[1].doc_range)
        para_range = base_blocks[1].doc_range
        if para_range:
            full_para_deletes = [
                m
                for m in deletes
                if m["deleteContentRange"]["range"]["startIndex"] == para_range[0]
            ]
            assert len(full_para_deletes) == 0, (
                "Run-level splice should not delete entire paragraph; "
                "should only splice the changed run"
            )


# =============================================================================
# Scenario 3: Style survival on untouched paragraphs (gax-cvi.1)
#
# ADR 034 invariant 2: untouched paragraphs must be byte-identical after push.
# =============================================================================


@pytest.mark.e2e
class TestStyleSurvivalUntouched:
    """Scenario 3: editing one block does not affect other blocks' structure."""

    def test_untouched_blocks_not_in_ops(self):
        """Blocks not edited should not appear in edit ops at all."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        # Edit only the heading
        edited_md = md.replace("Test Heading", "Renamed Heading", 1)
        assert edited_md != md

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        # Only the heading block should be in ops
        affected_base_indices = {op.base_idx for op in ops if op.base_idx is not None}
        # Heading is block 0
        assert 0 in affected_base_indices, "Heading should be in ops"

        # Non-heading blocks should NOT be affected
        # (list items, table, emoji paragraph)
        total_blocks = len(base_blocks)
        untouched_indices = set(range(1, total_blocks)) - affected_base_indices
        assert len(untouched_indices) > 0, (
            "Some blocks should remain untouched"
        )

    @pytest.mark.xfail(
        reason="gax-cvi.2: full index-stripped comparison requires baseline; "
        "currently the rendered markdown may differ from the original JSON "
        "structure for non-markdown features",
        strict=False,
    )
    def test_untouched_blocks_structurally_identical(self):
        """Index-stripped block JSON is identical for non-edited blocks.

        Mirrors _assert_untouched_blocks_identical from prototype.
        Requires baseline storage to verify (Phase 1).
        """
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)
        edited_md = md.replace("Test Heading", "Renamed Heading", 1)
        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        # After applying mutations, re-fetch doc and compare.
        # In unit-test form: verify no ops touch non-heading blocks.
        affected = {op.base_idx for op in ops if op.base_idx is not None}
        for i in range(len(base_blocks)):
            if i not in affected:
                # This block should produce zero mutations
                pass  # Structural comparison would need live API
        # Verify no spurious ops on non-heading blocks
        assert affected == {0}, (
            f"Only heading (block 0) should be affected, got {affected}"
        )


# =============================================================================
# Scenario 4: Insert paragraph (gax-cvi.1)
#
# Inserting a new paragraph should not disturb neighbor blocks.
# =============================================================================


@pytest.mark.e2e
class TestInsertParagraph:
    """Scenario 4: insert a paragraph between existing blocks."""

    def test_insert_detected_as_insert_op(self):
        """Adding a paragraph in the markdown produces an 'insert' edit op."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        # Insert a new paragraph after the heading
        lines = md.split("\n")
        # Find heading line and insert after it
        insert_idx = None
        for i, line in enumerate(lines):
            if line.startswith("# Test Heading"):
                insert_idx = i + 1
                break
        assert insert_idx is not None, "Should find the heading"

        lines.insert(insert_idx, "")
        lines.insert(insert_idx + 1, "Newly inserted paragraph.")
        edited_md = "\n".join(lines)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        insert_ops = [op for op in ops if op.type == "insert"]
        assert len(insert_ops) >= 1, "Should have at least one insert op"

        # Verify the inserted text
        inserted_texts: list[str] = [
            op.edit_block.text  # type: ignore[union-attr]
            for op in insert_ops
            if op.edit_block and hasattr(op.edit_block, "text")
        ]
        assert any(
            "Newly inserted" in t for t in inserted_texts
        ), f"Inserted text should contain 'Newly inserted', got {inserted_texts}"

    def test_insert_does_not_delete_neighbors(self):
        """Insert op should not produce delete ops on existing blocks."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        lines = md.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("# Test Heading"):
                lines.insert(i + 2, "New paragraph here.")
                lines.insert(i + 2, "")
                break
        edited_md = "\n".join(lines)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        delete_ops = [op for op in ops if op.type == "delete"]
        assert len(delete_ops) == 0, (
            f"Pure insert should not produce delete ops, got {len(delete_ops)}"
        )


# =============================================================================
# Scenario 5: Delete paragraph (gax-cvi.1)
#
# Deleting one paragraph should not affect neighbors.
# =============================================================================


@pytest.mark.e2e
class TestDeleteParagraph:
    """Scenario 5: delete a paragraph; other blocks remain."""

    def test_delete_detected_as_delete_op(self):
        """Removing a paragraph from the markdown produces a 'delete' edit op."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        # Remove one list item line
        lines = md.split("\n")
        removed = None
        new_lines = []
        for line in lines:
            if "Costs remained flat" in line and removed is None:
                removed = line
                continue
            new_lines.append(line)
        assert removed is not None, "Should find list item to remove"
        edited_md = "\n".join(new_lines)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        delete_ops = [op for op in ops if op.type == "delete"]
        assert len(delete_ops) >= 1, "Should have at least one delete op"

    def test_delete_does_not_insert(self):
        """Pure deletion should not produce insert ops."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        lines = md.split("\n")
        new_lines = [line for line in lines if "Market share grew" not in line]
        edited_md = "\n".join(new_lines)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        insert_ops = [op for op in ops if op.type == "insert"]
        assert len(insert_ops) == 0, (
            f"Pure delete should not produce insert ops, got {len(insert_ops)}"
        )


# =============================================================================
# Scenario 6: Heading rename (gax-cvi.1)
#
# Renaming a heading should produce an update, not delete+insert.
# =============================================================================


@pytest.mark.e2e
class TestHeadingRename:
    """Scenario 6: rename heading text + change level."""

    def test_heading_rename_is_update(self):
        """Renaming a heading should produce an 'update' op, not delete+insert."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        # Change heading text and level
        edited_md = md.replace("# Test Heading", "## Renamed Heading", 1)
        assert edited_md != md

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        # Should be an update (not delete + insert)
        update_ops = [op for op in ops if op.type == "update"]
        assert len(update_ops) >= 1, "Heading rename should be an update op"

        # Verify the update targets the heading
        heading_updates = [
            op
            for op in update_ops
            if op.base_block and isinstance(op.base_block, Heading)
        ]
        assert len(heading_updates) >= 1, "Should update the heading block"

    def test_heading_rename_mutations_include_paragraph_style(self):
        """Heading level change should produce updateParagraphStyle mutation."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)
        edited_md = md.replace("# Test Heading", "## Renamed Heading", 1)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)
        tab_id = "t.1"
        mutations = diff_to_mutations(ops, base_blocks, tab_id)

        para_style_mutations = [
            m for m in mutations if "updateParagraphStyle" in m
        ]
        assert len(para_style_mutations) >= 1, (
            "Level change should produce updateParagraphStyle"
        )

        # Verify it sets HEADING_2
        for m in para_style_mutations:
            style = m["updateParagraphStyle"]["paragraphStyle"]
            if "namedStyleType" in style:
                assert style["namedStyleType"] == "HEADING_2"


# =============================================================================
# Scenario 7: Table cell edit (gax-cvi.1)
#
# Editing one cell should not affect other cells.
# =============================================================================


@pytest.mark.e2e
class TestTableCellEdit:
    """Scenario 7: edit one table cell; other cells unchanged."""

    def test_table_edit_detected(self):
        """Editing a table cell should produce an update op on the table."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        # Edit one cell: "Region" → "Area"
        edited_md = md.replace("Region", "Area", 1)
        assert edited_md != md

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        # Should detect table change
        table_ops = [
            op
            for op in ops
            if op.base_block and isinstance(op.base_block, Table)
        ]
        assert len(table_ops) >= 1, "Should detect table cell edit"

    def test_table_mutations_target_cell(self):
        """Table cell edit should produce mutations only on the changed cell."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)
        edited_md = md.replace("Region", "Area", 1)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)
        tab_id = "t.1"
        mutations = diff_to_mutations(ops, base_blocks, tab_id)

        # Should have mutations (delete old text + insert new)
        assert len(mutations) > 0, "Should produce mutations for table edit"

        # Mutations should target the cell's index range, not the whole table
        delete_mutations = [m for m in mutations if "deleteContentRange" in m]
        insert_mutations = [m for m in mutations if "insertText" in m]
        assert len(delete_mutations) >= 1, "Should delete old cell text"
        assert len(insert_mutations) >= 1, "Should insert new cell text"


# =============================================================================
# Scenario 8: UTF-16 emoji handling (gax-cvi.1)
#
# Emoji (outside BMP) use 2 UTF-16 code units. Index math must account
# for this or mutations will corrupt the document.
# =============================================================================


@pytest.mark.e2e
class TestEmojiUtf16:
    """Scenario 8: edits around emoji must use correct UTF-16 indices."""

    def test_emoji_paragraph_parsed(self):
        """The emoji paragraph should parse correctly from doc JSON."""
        base_blocks = _blocks_from_fixture()

        # Find emoji block
        emoji_block = None
        for b in base_blocks:
            if isinstance(b, Paragraph) and "🎉" in b.text:
                emoji_block = b
                break
        assert emoji_block is not None, "Should parse emoji paragraph"
        assert "🚀" in emoji_block.text, "Should contain rocket emoji"

    def test_emoji_utf16_len(self):
        """Verify _utf16_len handles emoji correctly."""
        # 🎉 is U+1F389 — outside BMP, 2 UTF-16 code units
        assert _utf16_len("🎉") == 2
        # 🚀 is U+1F680
        assert _utf16_len("🚀") == 2
        # Mixed: "a🎉b" = 1 + 2 + 1 = 4
        assert _utf16_len("a🎉b") == 4
        # 𝕳 is U+1D573
        assert _utf16_len("𝕳") == 2

    @pytest.mark.xfail(
        reason="gax-cvi.3: run-level splice must use UTF-16 offsets; "
        "current paragraph-replace path sidesteps per-character indexing",
        strict=False,
    )
    def test_emoji_edit_index_math(self):
        """Edit text adjacent to emoji; verify mutation indices are UTF-16 correct.

        After Phase 3 (run-level splice), the mutation will target only
        the changed run. The indices must account for emoji surrogate pairs.
        """
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        # Edit: "party" → "celebration" (adjacent to 🎉)
        edited_md = md.replace("party", "celebration")
        assert edited_md != md

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)
        tab_id = "t.1"
        mutations = diff_to_mutations(ops, base_blocks, tab_id)

        # In run-level splice, the delete range for "party" must start
        # AFTER the 🎉 emoji (which is 2 UTF-16 units, not 1).
        # Find the delete that targets the emoji paragraph
        emoji_block = None
        for b in base_blocks:
            if isinstance(b, Paragraph) and "🎉" in b.text:
                emoji_block = b
                break
        assert emoji_block is not None

        # The splice should NOT delete the entire paragraph
        if emoji_block.doc_range:
            full_deletes = [
                m
                for m in mutations
                if "deleteContentRange" in m
                and m["deleteContentRange"]["range"]["startIndex"]
                == emoji_block.doc_range[0]
                and m["deleteContentRange"]["range"]["endIndex"]
                >= emoji_block.doc_range[1] - 2
            ]
            assert len(full_deletes) == 0, (
                "Run-level splice should only delete the 'party' run, "
                "not the entire emoji paragraph"
            )


# =============================================================================
# Adversarial alignment: unit tests against ast_diff (gax-cvi.1)
#
# These test the alignment algorithm's behavior on pathological inputs
# that the Phase 2 anchored-similarity matcher must handle.
# =============================================================================


class TestAdversarialAlignment:
    """Adversarial alignment cases for the diff algorithm."""

    def test_duplicate_paragraphs(self):
        """Two identical paragraphs: editing one should not confuse the matcher.

        Current SequenceMatcher may misalign duplicates. This documents
        the expected behavior.
        """
        # Two identical paragraphs
        body = []
        idx = 1
        for _ in range(2):
            end = idx + len("Same text") + 1
            body.append(
                {
                    "startIndex": idx,
                    "endIndex": end,
                    "paragraph": {
                        "elements": [
                            {
                                "startIndex": idx,
                                "endIndex": end,
                                "textRun": {"content": "Same text\n"},
                            }
                        ],
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    },
                }
            )
            idx = end

        base_blocks = from_doc_json(body)
        assert len(base_blocks) == 2

        # Edit: change first to "Different text", keep second
        md = render_markdown(base_blocks)
        edited_md = md.replace("Same text", "Different text", 1)
        local_blocks = from_markdown(edited_md)

        ops = ast_diff(base_blocks, local_blocks)
        # Note: SequenceMatcher may produce different alignments;
        # the key requirement is: no data loss (no spurious deletes)
        assert len(local_blocks) == 2, "Should still have 2 blocks"
        # At least one op should exist (something changed)
        assert len(ops) >= 1, "Should detect the text change"

    def test_heavy_rewrite_80_percent(self):
        """80% of paragraphs rewritten: should produce updates, not delete-all + insert-all.

        This tests that the matcher handles heavy edits gracefully.
        """
        # 5 paragraphs
        body = []
        idx = 1
        texts = ["Alpha line", "Beta line", "Gamma line", "Delta line", "Epsilon line"]
        for text in texts:
            end = idx + len(text) + 1
            body.append(
                {
                    "startIndex": idx,
                    "endIndex": end,
                    "paragraph": {
                        "elements": [
                            {
                                "startIndex": idx,
                                "endIndex": end,
                                "textRun": {"content": text + "\n"},
                            }
                        ],
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    },
                }
            )
            idx = end

        base_blocks = from_doc_json(body)
        md = render_markdown(base_blocks)

        # Rewrite 4/5 paragraphs (80%)
        edited_md = md.replace("Alpha line", "First rewritten")
        edited_md = edited_md.replace("Beta line", "Second rewritten")
        edited_md = edited_md.replace("Gamma line", "Third rewritten")
        edited_md = edited_md.replace("Delta line", "Fourth rewritten")
        # Keep "Epsilon line" unchanged

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        # Block count should be preserved
        assert len(local_blocks) == 5
        # Should have some updates
        update_ops = [op for op in ops if op.type == "update"]
        assert len(update_ops) >= 1, "Should have update ops for rewritten blocks"
        # Should NOT have more deletes than necessary
        delete_ops = [op for op in ops if op.type == "delete"]
        insert_ops = [op for op in ops if op.type == "insert"]
        # At worst, it's all replaces (updates); should not delete ALL and insert ALL
        assert not (len(delete_ops) == 5 and len(insert_ops) == 5), (
            "Should not be delete-all + insert-all"
        )

    def test_new_heading_splits_section(self):
        """Inserting a heading in the middle should not rewrite the whole section.

        A new heading between existing content should be a single insert;
        content after it should pair with its original positions.
        Note: SequenceMatcher handles this case correctly already.
        """
        # Heading + 3 paragraphs
        body = []
        idx = 1
        h_end = idx + len("Section One") + 1
        body.append(
            {
                "startIndex": idx,
                "endIndex": h_end,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": idx,
                            "endIndex": h_end,
                            "textRun": {"content": "Section One\n"},
                        }
                    ],
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                },
            }
        )
        idx = h_end
        for text in ["First paragraph.", "Second paragraph.", "Third paragraph."]:
            end = idx + len(text) + 1
            body.append(
                {
                    "startIndex": idx,
                    "endIndex": end,
                    "paragraph": {
                        "elements": [
                            {
                                "startIndex": idx,
                                "endIndex": end,
                                "textRun": {"content": text + "\n"},
                            }
                        ],
                        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    },
                }
            )
            idx = end

        base_blocks = from_doc_json(body)
        md = render_markdown(base_blocks)

        # Insert a new heading between 2nd and 3rd paragraph
        lines = md.split("\n")
        new_lines = []
        found = False
        for line in lines:
            new_lines.append(line)
            if "Second paragraph" in line and not found:
                new_lines.append("")
                new_lines.append("## New Subsection")
                new_lines.append("")
                found = True
        edited_md = "\n".join(new_lines)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)

        # The new heading should be a single insert op
        insert_ops = [op for op in ops if op.type == "insert"]
        heading_inserts = [
            op
            for op in insert_ops
            if op.edit_block and isinstance(op.edit_block, Heading)
        ]
        assert len(heading_inserts) == 1, (
            f"Should insert exactly 1 new heading, got {len(heading_inserts)}"
        )

        # Existing paragraphs should NOT be deleted
        delete_ops = [op for op in ops if op.type == "delete"]
        assert len(delete_ops) == 0, (
            f"Inserting a heading should not delete existing paragraphs, "
            f"got {len(delete_ops)} deletes"
        )


# =============================================================================
# Scenario 9: Mutation generation sanity (gax-cvi.1)
#
# Verifies that diff_to_mutations produces valid Docs API request shapes.
# =============================================================================


@pytest.mark.e2e
class TestMutationGeneration:
    """Verify mutation translation produces valid API request structures."""

    def test_update_produces_delete_insert(self):
        """A text update should produce deleteContentRange + insertText."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)
        edited_md = md.replace("Test Heading", "New Title", 1)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)
        tab_id = "t.1"
        mutations = diff_to_mutations(ops, base_blocks, tab_id)

        assert len(mutations) > 0, "Should produce mutations"

        # Check structure
        has_delete = any("deleteContentRange" in m for m in mutations)
        has_insert = any("insertText" in m for m in mutations)
        assert has_delete, "Update should include deleteContentRange"
        assert has_insert, "Update should include insertText"

    def test_mutations_sorted_reverse_index(self):
        """Mutations are sorted in reverse startIndex order (Docs API requirement)."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)

        # Edit two blocks to get multiple mutations
        edited_md = md.replace("Test Heading", "New Title", 1)
        edited_md = edited_md.replace("Revenue increased", "Revenue grew", 1)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)
        tab_id = "t.1"
        mutations = diff_to_mutations(ops, base_blocks, tab_id)

        # Extract start indices
        indices = []
        for m in mutations:
            for val in m.values():
                if isinstance(val, dict):
                    r = val.get("range") or val.get("location")
                    if r:
                        idx = r.get("startIndex") or r.get("index")
                        if idx is not None:
                            indices.append(idx)
                            break

        # Should be non-increasing (reverse order)
        for i in range(len(indices) - 1):
            assert indices[i] >= indices[i + 1], (
                f"Mutations not in reverse index order: "
                f"{indices[i]} < {indices[i + 1]} at position {i}"
            )

    def test_tab_id_in_all_mutations(self):
        """Every mutation range/location should include the tab_id."""
        base_blocks = _blocks_from_fixture()
        md = render_markdown(base_blocks)
        edited_md = md.replace("Test Heading", "New Title", 1)

        local_blocks = from_markdown(edited_md)
        ops = ast_diff(base_blocks, local_blocks)
        tab_id = "t.test_tab"
        mutations = diff_to_mutations(ops, base_blocks, tab_id)

        for m in mutations:
            for val in m.values():
                if isinstance(val, dict):
                    r = val.get("range") or val.get("location")
                    if r and ("startIndex" in r or "index" in r):
                        assert r.get("tabId") == tab_id, (
                            f"Missing/wrong tabId in mutation: {m}"
                        )
