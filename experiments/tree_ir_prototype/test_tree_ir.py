"""Integration tests for Tree IR prototype.

Validates ADR 034 + 035 concepts against the live Google Docs API.

Run with:
    direnv exec . python -m pytest experiments/tree_ir_prototype/ -m e2e -v

Each test scenario exercises the full loop:
    scratch doc → documents().get() → enriched IR → YAML → edit YAML →
    parse YAML → three-way diff vs baseline → batchUpdate → re-fetch → assert
"""

from __future__ import annotations

import copy
import json
import time

import pytest

from .enriched_ir import (
    Block,
    Heading,
    ListItem,
    Paragraph,
    Span,
    Table,
    TextStyle,
    ParagraphStyle,
    from_doc_json,
    _utf16_len,
)
from .yaml_serializer import serialize_tree, parse_tree
from .tree_diff import compute_plan, plan_to_requests, _normalize_spans
from .conftest import populate_rich_doc, E2E_PREFIX


# =============================================================================
# Helpers
# =============================================================================


def _fetch_doc(docs_service, doc_id: str) -> dict:
    """Fetch full document JSON."""
    return docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()


def _get_body_content(doc: dict) -> list[dict]:
    """Get body content from first tab."""
    tab = doc.get("tabs", [{}])[0]
    return tab.get("documentTab", {}).get("body", {}).get("content", [])


def _get_tab_id(doc: dict) -> str:
    """Get the tabId of the first tab."""
    tab = doc.get("tabs", [{}])[0]
    return tab.get("tabProperties", {}).get("tabId", "")


def _get_lists(doc: dict) -> dict:
    """Get lists dict from first tab."""
    tab = doc.get("tabs", [{}])[0]
    return tab.get("documentTab", {}).get("lists", {})


def _body_json_snapshot(doc: dict) -> str:
    """Deterministic JSON snapshot of body content for comparison."""
    return json.dumps(_get_body_content(doc), sort_keys=True, ensure_ascii=False)


def _block_json_at(doc: dict, block_idx: int) -> str:
    """Get JSON of a specific body element for byte-identical comparison."""
    content = _get_body_content(doc)
    # Skip the first element (section break)
    structural = [e for e in content if "paragraph" in e or "table" in e]
    if block_idx < len(structural):
        return json.dumps(structural[block_idx], sort_keys=True, ensure_ascii=False)
    return ""


def _apply_plan(docs_service, doc_id: str, plan, tab_id: str):
    """Apply a plan's mutations to the document."""
    requests = plan_to_requests(plan, tab_id)
    if requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests},
        ).execute()
    return requests


def _strip_index_fields(obj):
    """Deep-strip startIndex/endIndex from a JSON-like structure for comparison.

    The Docs API shifts all indices when text is inserted/deleted, so we
    cannot compare raw indices across edits. Stripping them lets us assert
    that non-edited blocks are structurally identical.
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


def _get_structural_blocks(doc: dict) -> list[dict]:
    """Get structural elements (paragraphs + tables) from body, excluding section breaks."""
    content = _get_body_content(doc)
    return [e for e in content if "paragraph" in e or "table" in e]


def _assert_untouched_blocks_identical(
    doc_before: dict,
    doc_after: dict,
    edited_indices: set[int],
    scenario: str = "",
):
    """Assert all non-edited blocks are byte-identical (index-stripped).

    ADR 034 invariant 2: untouched content is untouched.

    Args:
        doc_before: Full doc JSON before edit
        doc_after: Full doc JSON after edit
        edited_indices: Set of structural block indices that were edited
        scenario: Label for assertion message
    """
    blocks_before = _get_structural_blocks(doc_before)
    blocks_after = _get_structural_blocks(doc_after)

    # After insert/delete the block count may differ; only compare
    # blocks that exist in both and are not in the edited set.
    # For insert: new blocks appear; for delete: blocks disappear.
    # We compare the minimum of before/after, skipping edited indices.
    min_len = min(len(blocks_before), len(blocks_after))

    for i in range(min_len):
        if i in edited_indices:
            continue
        before_stripped = _strip_index_fields(blocks_before[i])
        after_stripped = _strip_index_fields(blocks_after[i])
        assert before_stripped == after_stripped, (
            f"[{scenario}] Block {i} was not edited but differs after push.\n"
            f"  Before: {json.dumps(before_stripped, sort_keys=True)[:300]}\n"
            f"  After:  {json.dumps(after_stripped, sort_keys=True)[:300]}"
        )


# =============================================================================
# Scenario 1: No-op (serialize → parse → diff ⇒ ZERO mutations)
# =============================================================================


@pytest.mark.e2e
class TestNoOp:
    """Scenario 1: serialize → parse → diff produces zero mutations."""

    def test_no_op_round_trip(self, docs_service, scratch_doc):
        """Full rich doc: serialize → parse → diff = zero mutations."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        # IR from doc JSON (baseline)
        base_blocks = from_doc_json(body_content, lists=lists)

        # Serialize to YAML
        yaml_str = serialize_tree(base_blocks)
        assert len(yaml_str) > 0, "YAML serialization should not be empty"

        # Parse back
        local_blocks = parse_tree(yaml_str)
        assert len(local_blocks) > 0, "Parsed blocks should not be empty"

        # Compute plan
        plan = compute_plan(base_blocks, local_blocks, tab_id)

        # ASSERT: zero mutations
        assert plan.is_empty, (
            f"No-op round trip should produce zero mutations, got {len(plan.mutations)}: "
            f"{plan.summary}"
        )


# =============================================================================
# Scenario 2: Run-boundary noise → ZERO mutations
# =============================================================================


@pytest.mark.e2e
class TestRunBoundaryNoise:
    """Scenario 2: different run splits of same text+style → zero mutations."""

    def test_run_resplit_produces_zero_mutations(self, docs_service, scratch_doc):
        """Re-splitting runs without changing text/style = zero mutations."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)

        # Serialize
        yaml_str = serialize_tree(base_blocks)

        # Parse back
        local_blocks = parse_tree(yaml_str)

        # Now artificially split a run: take the first paragraph with multiple chars
        # and split its first span into two spans with the same style
        for block in local_blocks:
            if isinstance(block, Paragraph) and block.spans:
                first_span = block.spans[0]
                if len(first_span.text) > 2:
                    # Split it
                    mid = len(first_span.text) // 2
                    part1 = Span(text=first_span.text[:mid], style=copy.deepcopy(first_span.style))
                    part2 = Span(text=first_span.text[mid:], style=copy.deepcopy(first_span.style))
                    block.spans = [part1, part2] + block.spans[1:]
                    break

        # Compute plan
        plan = compute_plan(base_blocks, local_blocks, tab_id)

        # ASSERT: zero mutations (run boundaries are non-semantic)
        assert plan.is_empty, (
            f"Run boundary noise should produce zero mutations, got {len(plan.mutations)}: "
            f"{plan.summary}"
        )


# =============================================================================
# Scenario 3: Word edit mid-paragraph (surgical splice)
# =============================================================================


@pytest.mark.e2e
class TestWordEdit:
    """Scenario 3: change one word, sibling runs untouched."""

    def test_word_edit_preserves_siblings(self, docs_service, scratch_doc):
        """Edit one word in a mixed-format paragraph; other runs survive."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)

        # Take a snapshot of the body for comparison
        pre_snapshot = _body_json_snapshot(doc)

        # Serialize to YAML, edit one word, parse back
        yaml_str = serialize_tree(base_blocks)
        # Edit: change "plain text" to "simple text" in the mixed paragraph
        edited_yaml = yaml_str.replace("plain text", "simple text")
        assert edited_yaml != yaml_str, "Edit should change the YAML"

        local_blocks = parse_tree(edited_yaml)

        # Compute plan
        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty, "Should produce mutations for text edit"

        # Verify plan preview is available
        assert len(plan.summary) > 0, "Plan should have a summary"

        # Apply the plan
        requests = _apply_plan(docs_service, doc_id, plan, tab_id)
        mutation_count = len(requests)

        # Re-fetch and verify
        doc_after = _fetch_doc(docs_service, doc_id)
        body_after = _get_body_content(doc_after)
        blocks_after = from_doc_json(body_after, lists=_get_lists(doc_after))

        # Find the edited paragraph
        edited_para = None
        for b in blocks_after:
            if isinstance(b, Paragraph) and "simple text" in b.text:
                edited_para = b
                break
        assert edited_para is not None, "Edited text should appear in re-fetched doc"

        # Check that the bold, colored, and linked spans are still present
        has_bold = any(s.style.bold for s in edited_para.spans)
        has_color = any(s.style.foreground_color for s in edited_para.spans)
        has_link = any(s.style.url for s in edited_para.spans)
        assert has_bold, "Bold formatting should survive word edit"
        assert has_color, "Color formatting should survive word edit"
        assert has_link, "Link should survive word edit"

        # INVARIANT 2: untouched blocks are byte-identical (index-stripped)
        # The edited paragraph is block 1 (after heading at 0)
        _assert_untouched_blocks_identical(doc, doc_after, {1}, "Scenario 3")

        # Record mutation count
        print(f"\n  Scenario 3 mutations: {mutation_count}")


# =============================================================================
# Scenario 4: Style-only edit (no text change)
# =============================================================================


@pytest.mark.e2e
class TestStyleOnlyEdit:
    """Scenario 4: change color of one word → updateTextStyle only."""

    def test_color_change_no_text_delete(self, docs_service, scratch_doc):
        """Change color of one word: only updateTextStyle, no delete/insert."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)

        # Serialize and parse
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Find the paragraph with "bold word" and change its color
        for block in local_blocks:
            if isinstance(block, Paragraph):
                for span in block.spans:
                    if "bold" in span.text and span.style.bold:
                        # Change color of bold word
                        span.style.foreground_color = "#0000ff"
                        break

        # Compute plan
        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty, "Should produce mutations for style change"

        # Verify NO delete/insert mutations (style-only)
        requests = plan_to_requests(plan, tab_id)
        for req in requests:
            assert "deleteContentRange" not in req, (
                "Style-only edit should NOT produce deleteContentRange"
            )
            assert "insertText" not in req, (
                "Style-only edit should NOT produce insertText"
            )
            assert "updateTextStyle" in req, (
                "Style-only edit should produce updateTextStyle"
            )

        # Apply
        _apply_plan(docs_service, doc_id, plan, tab_id)

        # Re-fetch and verify color applied
        doc_after = _fetch_doc(docs_service, doc_id)
        body_after = _get_body_content(doc_after)
        blocks_after = from_doc_json(body_after, lists=_get_lists(doc_after))

        # Find the bold word and check its color
        for b in blocks_after:
            if isinstance(b, Paragraph):
                for s in b.spans:
                    if s.style.bold and "bold" in s.text:
                        assert s.style.foreground_color == "#0000ff", (
                            f"Color should be blue, got {s.style.foreground_color}"
                        )
                        break

        print(f"\n  Scenario 4 mutations: {len(requests)}")


# =============================================================================
# Scenario 5: Paragraph-style edit (alignment change)
# =============================================================================


@pytest.mark.e2e
class TestParagraphStyleEdit:
    """Scenario 5: change alignment → single updateParagraphStyle."""

    def test_alignment_change(self, docs_service, scratch_doc):
        """Change alignment: single updateParagraphStyle, text untouched."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Find the centered paragraph and change it to END alignment
        found = False
        for block in local_blocks:
            if isinstance(block, Paragraph) and block.para_style.alignment == "CENTER":
                block.para_style.alignment = "END"
                found = True
                break
        assert found, "Should find a centered paragraph"

        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty, "Should produce mutations"

        requests = plan_to_requests(plan, tab_id)
        # Should be paragraph style only
        for req in requests:
            assert "deleteContentRange" not in req
            assert "insertText" not in req
        has_para_style = any("updateParagraphStyle" in req for req in requests)
        assert has_para_style, "Should have updateParagraphStyle"

        _apply_plan(docs_service, doc_id, plan, tab_id)

        # Verify
        doc_after = _fetch_doc(docs_service, doc_id)
        blocks_after = from_doc_json(
            _get_body_content(doc_after), lists=_get_lists(doc_after)
        )
        found_end = any(
            isinstance(b, Paragraph) and b.para_style.alignment == "END"
            for b in blocks_after
        )
        assert found_end, "Alignment should be END after push"
        print(f"\n  Scenario 5 mutations: {len(requests)}")


# =============================================================================
# Scenario 6: Formatting beyond markdown (font size + underline)
# =============================================================================


@pytest.mark.e2e
class TestBeyondMarkdown:
    """Scenario 6: font size + underline on a span."""

    def test_font_size_underline(self, docs_service, scratch_doc):
        """Set font size + underline: proves tree surface exceeds md vocabulary."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Find a plain span and add underline + font size
        target_block = None
        target_span_idx = None
        for block in local_blocks:
            if isinstance(block, Paragraph) and "plain" in block.text:
                # Use the first plain span
                for idx, span in enumerate(block.spans):
                    if not span.style.bold and not span.style.url and "plain" in span.text:
                        span.style.underline = True
                        span.style.font_size = 14.0
                        target_block = block
                        target_span_idx = idx
                        break
                if target_block:
                    break

        assert target_block is not None, "Should find a target span"

        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty

        requests = plan_to_requests(plan, tab_id)
        # Should be style-only
        for req in requests:
            assert "deleteContentRange" not in req
            assert "insertText" not in req

        _apply_plan(docs_service, doc_id, plan, tab_id)

        # Verify
        doc_after = _fetch_doc(docs_service, doc_id)
        blocks_after = from_doc_json(
            _get_body_content(doc_after), lists=_get_lists(doc_after)
        )
        found_underline = False
        for b in blocks_after:
            if isinstance(b, Paragraph):
                for s in b.spans:
                    if s.style.underline and s.style.font_size:
                        found_underline = True
                        break
        assert found_underline, "Underline + font size should be applied"
        print(f"\n  Scenario 6 mutations: {len(requests)}")


# =============================================================================
# Scenario 7: Insert paragraph between two styled paragraphs
# =============================================================================


@pytest.mark.e2e
class TestInsertParagraph:
    """Scenario 7: insert between styled paragraphs → neighbors unchanged."""

    def test_insert_preserves_neighbors(self, docs_service, scratch_doc):
        """Insert a new paragraph; neighbors are byte-identical."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)

        # Snapshot neighbor blocks (heading and mixed paragraph)
        structural = [e for e in body_content if "paragraph" in e or "table" in e]
        neighbor_before_json = json.dumps(structural[0], sort_keys=True) if structural else ""
        neighbor_after_json = json.dumps(structural[1], sort_keys=True) if len(structural) > 1 else ""

        # Insert a new paragraph after the heading
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Insert after first block (heading)
        new_para = Paragraph(spans=[Span(text="Newly inserted paragraph", style=TextStyle())])
        local_blocks.insert(1, new_para)

        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty

        requests = _apply_plan(docs_service, doc_id, plan, tab_id)

        # Re-fetch
        doc_after = _fetch_doc(docs_service, doc_id)
        body_after = _get_body_content(doc_after)
        structural_after = [e for e in body_after if "paragraph" in e or "table" in e]

        # The heading (first structural) should be byte-identical
        # Note: startIndex/endIndex will shift, so we compare content only
        def _content_only(elem: dict) -> str:
            """Strip index fields for content comparison."""
            e = copy.deepcopy(elem)
            for key in ("startIndex", "endIndex"):
                e.pop(key, None)
            if "paragraph" in e:
                for el in e["paragraph"].get("elements", []):
                    el.pop("startIndex", None)
                    el.pop("endIndex", None)
            return json.dumps(e, sort_keys=True)

        heading_after = _content_only(structural_after[0])
        heading_before = _content_only(structural[0])
        assert heading_after == heading_before, "Heading should be unchanged after insert"

        # Verify the new paragraph exists
        all_text = " ".join(
            "".join(
                el.get("textRun", {}).get("content", "")
                for el in e.get("paragraph", {}).get("elements", [])
            )
            for e in body_after if "paragraph" in e
        )
        assert "Newly inserted paragraph" in all_text

        # INVARIANT 2: all pre-existing blocks are unchanged (index-stripped).
        # After insert at position 1, original blocks 0 stays at 0,
        # and original blocks 1..N shift to 2..N+1 in the after doc.
        blocks_before = _get_structural_blocks(doc)
        blocks_after_list = _get_structural_blocks(doc_after)
        # Check block 0 (heading) unchanged
        assert _strip_index_fields(blocks_before[0]) == _strip_index_fields(blocks_after_list[0]), \
            "[Scenario 7] Heading (block 0) should be unchanged after insert"
        # Check blocks 1..N == blocks 2..N+1 in after (shifted by insert)
        for i in range(1, len(blocks_before)):
            before_stripped = _strip_index_fields(blocks_before[i])
            after_stripped = _strip_index_fields(blocks_after_list[i + 1])
            assert before_stripped == after_stripped, (
                f"[Scenario 7] Block {i} (pre) should match block {i+1} (post) after insert"
            )

        print(f"\n  Scenario 7 mutations: {len(requests)}")


# =============================================================================
# Scenario 8: Delete one paragraph → neighbors byte-identical
# =============================================================================


@pytest.mark.e2e
class TestDeleteParagraph:
    """Scenario 8: delete a paragraph → neighbors unchanged."""

    def test_delete_preserves_neighbors(self, docs_service, scratch_doc):
        """Delete one paragraph; neighbors are unchanged."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)

        # Identify the block to delete (e.g., second list item)
        # and snapshot its neighbors
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Find list items and remove the middle one
        list_indices = [i for i, b in enumerate(local_blocks) if isinstance(b, ListItem)]
        assert len(list_indices) >= 2, "Need at least 2 list items"
        delete_idx = list_indices[1]  # Remove 2nd list item

        deleted_text = local_blocks[delete_idx].text
        del local_blocks[delete_idx]

        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty

        requests = _apply_plan(docs_service, doc_id, plan, tab_id)

        # Re-fetch and verify deletion
        doc_after = _fetch_doc(docs_service, doc_id)
        blocks_after = from_doc_json(
            _get_body_content(doc_after), lists=_get_lists(doc_after)
        )

        # Deleted text should be gone
        all_text = " ".join(b.text for b in blocks_after if hasattr(b, "text"))
        assert deleted_text not in all_text, f"'{deleted_text}' should be deleted"

        # Remaining list items should still exist
        remaining_list = [b for b in blocks_after if isinstance(b, ListItem)]
        assert len(remaining_list) >= 1, "Other list items should survive"

        # INVARIANT 2: non-deleted blocks are unchanged (index-stripped)
        # After delete at position delete_idx, blocks before it stay in place,
        # blocks after shift up by one.
        blocks_before = _get_structural_blocks(doc)
        blocks_after_list = _get_structural_blocks(doc_after)
        for i in range(len(blocks_after_list)):
            # Map back to pre-edit index
            pre_idx = i if i < delete_idx else i + 1
            if pre_idx == delete_idx:
                continue
            if pre_idx >= len(blocks_before):
                break
            before_stripped = _strip_index_fields(blocks_before[pre_idx])
            after_stripped = _strip_index_fields(blocks_after_list[i])
            assert before_stripped == after_stripped, (
                f"[Scenario 8] Block {pre_idx} (pre) should match block {i} (post) after delete"
            )

        print(f"\n  Scenario 8 mutations: {len(requests)}")


# =============================================================================
# Scenario 9: Heading rename (text + level change)
# =============================================================================


@pytest.mark.e2e
class TestHeadingRename:
    """Scenario 9: rename heading + change level → surgical."""

    def test_heading_rename(self, docs_service, scratch_doc):
        """Rename heading and change level; section content untouched."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Find heading and rename it + change level
        heading_found = False
        for block in local_blocks:
            if isinstance(block, Heading) and "Test Heading" in block.text:
                # Rename
                block.spans = [Span(text="Renamed Heading", style=TextStyle())]
                block.level = 2
                heading_found = True
                break
        assert heading_found, "Should find the test heading"

        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty

        requests = _apply_plan(docs_service, doc_id, plan, tab_id)

        # Verify
        doc_after = _fetch_doc(docs_service, doc_id)
        blocks_after = from_doc_json(
            _get_body_content(doc_after), lists=_get_lists(doc_after)
        )

        # Find renamed heading
        found_renamed = False
        for b in blocks_after:
            if isinstance(b, Heading) and "Renamed Heading" in b.text:
                assert b.level == 2, f"Heading level should be 2, got {b.level}"
                found_renamed = True
                break
        assert found_renamed, "Renamed heading should exist"

        # Other content should still be present
        all_text = " ".join(
            b.text for b in blocks_after
            if isinstance(b, (Paragraph, ListItem))
        )
        assert "bold word" in all_text, "Section content should survive heading rename"

        # INVARIANT 2: all blocks except the heading are unchanged
        _assert_untouched_blocks_identical(doc, doc_after, {0}, "Scenario 9")

        print(f"\n  Scenario 9 mutations: {len(requests)}")


# =============================================================================
# Scenario 10: Table cell text edit → other cells byte-identical
# =============================================================================


@pytest.mark.e2e
class TestTableCellEdit:
    """Scenario 10: edit one table cell → other cells untouched."""

    def test_table_cell_edit(self, docs_service, scratch_doc):
        """Edit one table cell; other cells (incl styled) are byte-identical."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)

        # Find the table block
        table_block = None
        table_idx = None
        for i, b in enumerate(base_blocks):
            if isinstance(b, Table):
                table_block = b
                table_idx = i
                break

        if table_block is None:
            pytest.skip("No table found in rich doc")

        # Serialize and parse
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Edit one cell: change "Region" to "Area"
        for block in local_blocks:
            if isinstance(block, Table) and block.rows:
                if block.rows[0] and block.rows[0][0]:
                    # First cell of first row
                    block.rows[0][0] = [Span(text="Area", style=TextStyle())]
                    break

        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty

        requests = _apply_plan(docs_service, doc_id, plan, tab_id)

        # Re-fetch
        doc_after = _fetch_doc(docs_service, doc_id)
        blocks_after = from_doc_json(
            _get_body_content(doc_after), lists=_get_lists(doc_after)
        )

        # Find table and verify edit
        for b in blocks_after:
            if isinstance(b, Table) and b.rows:
                # Edited cell
                first_cell_text = "".join(s.text for s in b.rows[0][0])
                assert "Area" in first_cell_text, f"Cell should say 'Area', got '{first_cell_text}'"

                # Bold cell should be preserved
                if len(b.rows) > 1 and len(b.rows[1]) > 1:
                    styled_cell = b.rows[1][1]
                    has_bold_cell = any(s.style.bold for s in styled_cell)
                    assert has_bold_cell, "Bold in other cell should survive"
                break

        # INVARIANT 2: all non-table blocks are unchanged; within the table,
        # non-edited cells are unchanged. We check blocks outside the table.
        table_block_idx = None
        structural = _get_structural_blocks(doc)
        for i, e in enumerate(structural):
            if "table" in e:
                table_block_idx = i
                break
        if table_block_idx is not None:
            _assert_untouched_blocks_identical(
                doc, doc_after, {table_block_idx}, "Scenario 10"
            )

        print(f"\n  Scenario 10 mutations: {len(requests)}")


# =============================================================================
# Scenario 11: Emoji paragraph edit (UTF-16 surrogate pair stress)
# =============================================================================


@pytest.mark.e2e
class TestEmojiEdit:
    """Scenario 11: edit paragraph with emoji → index math correct."""

    def test_emoji_paragraph_edit(self, docs_service, scratch_doc):
        """Edit text around emoji; re-fetch confirms correct indexing."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Find the emoji paragraph and edit text around emoji
        emoji_found = False
        for block in local_blocks:
            if isinstance(block, Paragraph) and "🎉" in block.text:
                # Change "party" to "celebration"
                for i, span in enumerate(block.spans):
                    if "party" in span.text:
                        block.spans[i] = Span(
                            text=span.text.replace("party", "celebration"),
                            style=span.style,
                        )
                        emoji_found = True
                        break
                break
        assert emoji_found, "Should find emoji paragraph with 'party'"

        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty

        requests = _apply_plan(docs_service, doc_id, plan, tab_id)

        # Re-fetch and verify
        doc_after = _fetch_doc(docs_service, doc_id)
        blocks_after = from_doc_json(
            _get_body_content(doc_after), lists=_get_lists(doc_after)
        )

        # Find the emoji paragraph
        found_edit = False
        emoji_block_idx = None
        structural = _get_structural_blocks(doc)
        for i, e in enumerate(structural):
            if "paragraph" in e:
                content = "".join(
                    el.get("textRun", {}).get("content", "")
                    for el in e["paragraph"].get("elements", [])
                )
                if "🎉" in content:
                    emoji_block_idx = i
                    break

        for b in blocks_after:
            if isinstance(b, Paragraph) and "🎉" in b.text:
                assert "celebration" in b.text, f"Should have 'celebration', got: {b.text}"
                assert "🚀" in b.text, "Other emoji should survive"
                found_edit = True
                break
        assert found_edit, "Emoji paragraph should still exist with edits"

        # INVARIANT 2: all blocks except the emoji paragraph are unchanged
        if emoji_block_idx is not None:
            _assert_untouched_blocks_identical(
                doc, doc_after, {emoji_block_idx}, "Scenario 11"
            )

        print(f"\n  Scenario 11 mutations: {len(requests)}")


# =============================================================================
# Scenario 12: Comment anchor survival (gax-7sp)
# =============================================================================


@pytest.mark.e2e
class TestCommentAnchorSurvival:
    """Scenario 12: comment stays anchored after editing same paragraph.

    Stretch scenario from gax-jdz: add a comment via Drive API on a word
    in a paragraph, then edit a DIFFERENT word in that same paragraph via
    the Tree IR pipeline, and assert the comment still exists and is not
    deleted.
    """

    def test_comment_anchor_survival(self, docs_service, drive_service, scratch_doc):
        """Add comment, edit word in same paragraph, comment survives."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        # Create a comment on "plain text" (appears in the rich paragraph).
        # We comment before making any edits so the anchor exists in the
        # original doc position.
        comment = drive_service.comments().create(
            fileId=doc_id,
            body={
                "content": "Scenario 12 test comment",
                "quotedFileContent": {
                    "mimeType": "text/plain",
                    "value": "plain text",
                },
            },
            fields="id,content,quotedFileContent",
        ).execute()
        comment_id = comment["id"]
        assert comment_id, "Comment should be created with an id"

        # Re-fetch after comment creation so we get current doc state.
        doc = _fetch_doc(docs_service, doc_id)
        body_content = _get_body_content(doc)
        lists = _get_lists(doc)
        tab_id = _get_tab_id(doc)

        base_blocks = from_doc_json(body_content, lists=lists)
        yaml_str = serialize_tree(base_blocks)
        local_blocks = parse_tree(yaml_str)

        # Edit "see more" → "learn more" in the SAME paragraph that has the
        # comment anchor ("plain text" is earlier in the same sentence).
        edit_found = False
        for block in local_blocks:
            if isinstance(block, Paragraph) and "plain text" in block.text:
                for i, span in enumerate(block.spans):
                    if "see more" in span.text:
                        block.spans[i] = Span(
                            text=span.text.replace("see more", "learn more"),
                            style=span.style,
                        )
                        edit_found = True
                        break
                break
        assert edit_found, "Should find paragraph containing both 'plain text' and 'see more'"

        # Apply the plan.
        plan = compute_plan(base_blocks, local_blocks, tab_id)
        assert not plan.is_empty, "Edit should produce at least one mutation"
        requests = _apply_plan(docs_service, doc_id, plan, tab_id)

        # ASSERT 1: the text edit landed.
        doc_after = _fetch_doc(docs_service, doc_id)
        blocks_after = from_doc_json(
            _get_body_content(doc_after), lists=_get_lists(doc_after)
        )
        edit_verified = False
        for block in blocks_after:
            if isinstance(block, Paragraph) and "plain text" in block.text:
                assert "learn more" in block.text, (
                    f"Expected 'learn more' in paragraph, got: {block.text}"
                )
                edit_verified = True
                break
        assert edit_verified, "Edited paragraph must exist in re-fetched doc"

        # ASSERT 2: the comment still exists and is not deleted.
        comments_response = drive_service.comments().list(
            fileId=doc_id,
            fields="comments(id,content,deleted)",
            includeDeleted=False,
        ).execute()
        live_ids = {
            c["id"] for c in comments_response.get("comments", [])
            if not c.get("deleted")
        }
        assert comment_id in live_ids, (
            f"Comment {comment_id!r} should survive edit to same paragraph; "
            f"live comment ids: {live_ids}"
        )

        print(
            f"\n  Scenario 12: comment {comment_id!r} survived "
            f"{len(requests)} mutation(s) to same paragraph"
        )


# =============================================================================
# Token measurement
# =============================================================================


@pytest.mark.e2e
class TestTokenMeasurement:
    """Measure token counts for raw JSON vs YAML vs markdown."""

    def test_token_ratios(self, docs_service, scratch_doc):
        """Compare token sizes of raw JSON, YAML serialization, and markdown."""
        from gax.gdoc.ir import from_doc_json as prod_from_doc_json, render_markdown

        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)

        # Raw JSON
        raw_json = json.dumps(body_content, ensure_ascii=False)
        raw_json_chars = len(raw_json)
        raw_json_tokens = raw_json_chars // 4  # char/4 approximation

        # Enriched IR → YAML
        enriched_blocks = from_doc_json(body_content, lists=lists)
        yaml_str = serialize_tree(enriched_blocks)
        yaml_chars = len(yaml_str)
        yaml_tokens = yaml_chars // 4

        # Production IR → Markdown
        prod_blocks = prod_from_doc_json(body_content, lists=lists)
        md_str = render_markdown(prod_blocks)
        md_chars = len(md_str)
        md_tokens = md_chars // 4

        print("\n\n  === Token Measurement ===")
        print(f"  Raw JSON:  {raw_json_chars:>6} chars  ~{raw_json_tokens:>5} tokens")
        print(f"  Tree YAML: {yaml_chars:>6} chars  ~{yaml_tokens:>5} tokens")
        print(f"  Markdown:  {md_chars:>6} chars  ~{md_tokens:>5} tokens")
        print(f"  YAML/JSON ratio: {yaml_chars/raw_json_chars:.2f}x")
        print(f"  MD/JSON ratio:   {md_chars/raw_json_chars:.2f}x")
        print(f"  YAML/MD ratio:   {yaml_chars/md_chars:.2f}x")

        # Sanity assertions
        assert yaml_chars < raw_json_chars, "YAML should be smaller than raw JSON"
        assert md_chars < yaml_chars, "Markdown should be smaller than YAML (lossy)"

        # Store for report
        TestTokenMeasurement._results = {
            "raw_json_chars": raw_json_chars,
            "raw_json_tokens": raw_json_tokens,
            "yaml_chars": yaml_chars,
            "yaml_tokens": yaml_tokens,
            "md_chars": md_chars,
            "md_tokens": md_tokens,
            "yaml_json_ratio": yaml_chars / raw_json_chars,
            "md_json_ratio": md_chars / raw_json_chars,
            "yaml_md_ratio": yaml_chars / md_chars,
        }


# =============================================================================
# Round-trip identity test
# =============================================================================


@pytest.mark.e2e
class TestRoundTripIdentity:
    """Verify from_doc_json → serialize → parse reproduces equivalent IR."""

    def test_roundtrip_preserves_structure(self, docs_service, scratch_doc):
        """IR survives serialize/parse round-trip."""
        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        body_content = _get_body_content(doc)
        lists = _get_lists(doc)

        original_blocks = from_doc_json(body_content, lists=lists)
        yaml_str = serialize_tree(original_blocks)
        roundtrip_blocks = parse_tree(yaml_str)

        assert len(original_blocks) == len(roundtrip_blocks), (
            f"Block count mismatch: {len(original_blocks)} vs {len(roundtrip_blocks)}"
        )

        for i, (orig, rt) in enumerate(zip(original_blocks, roundtrip_blocks)):
            assert type(orig).__name__ == type(rt).__name__, (
                f"Block {i} type mismatch: {type(orig).__name__} vs {type(rt).__name__}"
            )
            # Text should be identical
            if hasattr(orig, "text") and hasattr(rt, "text"):
                assert orig.text == rt.text, (
                    f"Block {i} text mismatch: {orig.text!r} vs {rt.text!r}"
                )
            # Styles should be equivalent (after normalization)
            if isinstance(orig, (Heading, Paragraph, ListItem)):
                orig_norm = _normalize_spans(orig.spans)
                rt_norm = _normalize_spans(rt.spans)
                assert len(orig_norm) == len(rt_norm), (
                    f"Block {i} span count mismatch after normalization: "
                    f"{len(orig_norm)} vs {len(rt_norm)}"
                )
                for j, (os, rs) in enumerate(zip(orig_norm, rt_norm)):
                    assert os.text == rs.text, (
                        f"Block {i} span {j} text mismatch"
                    )
                    assert os.style.style_equal(rs.style), (
                        f"Block {i} span {j} style mismatch: "
                        f"{os.style} vs {rs.style}"
                    )
