"""Tests for the tree plan front-end (compute_tree_plan and helpers).

Validates:
- AC1: Color change via tree -> single updateTextStyle (no delete/insert)
- AC2: Run re-split with identical content -> zero mutations
- AC3: Appendix edit -> rejected pre-plan
- AC4: Untouched blocks -> zero mutations
- Heading level changes
- Text change splice
- Paragraph style changes
- Revision guard
"""

from gax.gdoc.diff_push import (
    ThreeWayPlan,
    _build_tree_style_delta,
    _normalize_tree_runs,
    _tree_block_match_key,
    _tree_block_runs,
    _tree_block_text,
    _tree_block_type,
    _tree_heading_level_requests,
    _tree_para_style_diff_requests,
    _tree_run_style,
    _tree_run_text,
    _tree_style_diff_requests,
    compute_tree_plan,
)


# =============================================================================
# Helpers — build raw Google Docs body JSON
# =============================================================================


def _make_para_element(text: str, start: int, text_style: dict | None = None):
    """Build a raw paragraph structural element with correct indices."""
    end = start + len(text) + 1  # +1 for newline
    elem = {
        "startIndex": start,
        "endIndex": end,
        "textRun": {
            "content": text + "\n",
            "textStyle": text_style or {},
        },
    }
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "elements": [elem],
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
        },
    }


def _make_heading_element(text: str, start: int, level: int = 2):
    """Build a raw heading structural element."""
    end = start + len(text) + 1
    elem = {
        "startIndex": start,
        "endIndex": end,
        "textRun": {
            "content": text + "\n",
            "textStyle": {},
        },
    }
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "elements": [elem],
            "paragraphStyle": {"namedStyleType": f"HEADING_{level}"},
        },
    }


def _make_styled_para_element(
    runs: list[tuple[str, dict]], start: int, para_style: str = "NORMAL_TEXT",
):
    """Build a paragraph with multiple styled runs and correct indices."""
    elements = []
    idx = start
    for text, style in runs:
        content = text + "\n" if text == runs[-1][0] else text
        elements.append({
            "startIndex": idx,
            "endIndex": idx + len(content),
            "textRun": {
                "content": content,
                "textStyle": style,
            },
        })
        idx += len(content)
    end = idx
    return {
        "startIndex": start,
        "endIndex": end,
        "paragraph": {
            "elements": elements,
            "paragraphStyle": {"namedStyleType": para_style},
        },
    }


# =============================================================================
# Helper unit tests
# =============================================================================


class TestTreeBlockHelpers:
    def test_block_type_paragraph(self):
        assert _tree_block_type({"p": "hello"}) == "p"

    def test_block_type_heading(self):
        assert _tree_block_type({"h1": "Title"}) == "h1"
        assert _tree_block_type({"h3": "Sub"}) == "h3"

    def test_block_type_list_item(self):
        assert _tree_block_type({"li": "item"}) == "li"

    def test_block_type_table(self):
        assert _tree_block_type({"table": {"rows": []}}) == "table"

    def test_block_runs_string(self):
        runs = _tree_block_runs({"p": "hello"})
        assert runs == ["hello"]

    def test_block_runs_dict_with_runs(self):
        runs = _tree_block_runs({"p": {"runs": ["a", {"t": "b", "b": True}]}})
        assert len(runs) == 2

    def test_block_runs_heading(self):
        runs = _tree_block_runs({"h2": "Title"})
        assert runs == ["Title"]

    def test_run_text_string(self):
        assert _tree_run_text("hello") == "hello"

    def test_run_text_dict(self):
        assert _tree_run_text({"t": "hello", "b": True}) == "hello"

    def test_run_style_string(self):
        assert _tree_run_style("hello") == {}

    def test_run_style_dict(self):
        style = _tree_run_style({"t": "hello", "b": True, "color": "#ff0000"})
        assert style == {"b": True, "color": "#ff0000"}

    def test_block_text_paragraph(self):
        assert _tree_block_text({"p": "hello"}) == "hello"

    def test_block_text_multirun(self):
        block = {"p": {"runs": ["hello ", {"t": "world", "b": True}]}}
        assert _tree_block_text(block) == "hello world"

    def test_block_match_key_paragraph(self):
        assert _tree_block_match_key({"p": "hello"}) == "paragraph:hello"

    def test_block_match_key_heading(self):
        assert _tree_block_match_key({"h2": "Title"}) == "heading:Title"

    def test_block_match_key_list_item(self):
        assert _tree_block_match_key({"li": "item"}) == "list_item:item"


# =============================================================================
# Run normalization
# =============================================================================


class TestNormalizeTreeRuns:
    def test_merge_same_style(self):
        """Adjacent plain strings merge into one."""
        result = _normalize_tree_runs(["hello ", "world"])
        assert result == [("hello world", {})]

    def test_keep_different_styles(self):
        """Different styles stay separate."""
        runs = ["hello ", {"t": "world", "b": True}]
        result = _normalize_tree_runs(runs)
        assert len(result) == 2
        assert result[0] == ("hello ", {})
        assert result[1] == ("world", {"b": True})

    def test_empty_input(self):
        assert _normalize_tree_runs([]) == []

    def test_single_run(self):
        result = _normalize_tree_runs(["hello"])
        assert result == [("hello", {})]

    def test_merge_three_same(self):
        result = _normalize_tree_runs(["a", "b", "c"])
        assert result == [("abc", {})]

    def test_merge_styled_adjacent(self):
        runs = [{"t": "a", "b": True}, {"t": "b", "b": True}]
        result = _normalize_tree_runs(runs)
        assert result == [("ab", {"b": True})]


# =============================================================================
# Style delta
# =============================================================================


class TestBuildTreeStyleDelta:
    def test_bold_added(self):
        api, fields = _build_tree_style_delta({}, {"b": True})
        assert api == {"bold": True}
        assert fields == "bold"

    def test_bold_removed(self):
        api, fields = _build_tree_style_delta({"b": True}, {})
        assert api == {"bold": False}
        assert fields == "bold"

    def test_color_changed(self):
        api, fields = _build_tree_style_delta({}, {"color": "#ff0000"})
        assert "foregroundColor" in api
        assert "foregroundColor" in fields

    def test_no_change(self):
        api, fields = _build_tree_style_delta({"b": True}, {"b": True})
        assert api == {}
        assert fields == ""

    def test_multiple_changes(self):
        api, fields = _build_tree_style_delta(
            {}, {"b": True, "i": True, "color": "#00ff00"}
        )
        assert api["bold"] is True
        assert api["italic"] is True
        assert "foregroundColor" in api
        field_set = set(fields.split(","))
        assert {"bold", "italic", "foregroundColor"} == field_set

    def test_url_added(self):
        api, fields = _build_tree_style_delta({}, {"url": "https://example.com"})
        assert api == {"link": {"url": "https://example.com"}}
        assert fields == "link"

    def test_url_cleared_no_empty_link(self):
        """Clearing a URL must NOT emit link:{} — Docs API rejects it (gax-dcd).

        The correct approach is to include 'link' in the fields mask without
        setting api_style['link'], which resets the link field to its default.
        """
        api, fields = _build_tree_style_delta({"url": "https://example.com"}, {})
        assert "link" not in api, (
            "api_style must not contain 'link' when clearing — Docs API rejects "
            "link:{} with HttpError 400 'Links must include at least one type'"
        )
        assert "link" in fields.split(","), (
            "'link' must be in fields mask to clear the link on the run"
        )

    def test_url_changed(self):
        api, fields = _build_tree_style_delta(
            {"url": "https://old.com"}, {"url": "https://new.com"}
        )
        assert api == {"link": {"url": "https://new.com"}}
        assert fields == "link"


# =============================================================================
# Style diff requests
# =============================================================================


class TestTreeStyleDiffRequests:
    def test_style_only_color_change(self):
        """AC1: Color change -> single updateTextStyle (no delete/insert)."""
        base_runs = ["hello"]
        local_runs = [{"t": "hello", "color": "#ff0000"}]
        reqs = _tree_style_diff_requests(base_runs, local_runs, 10, "t1")
        assert len(reqs) == 1
        req = reqs[0]
        assert "updateTextStyle" in req
        assert "deleteContentRange" not in str(reqs)
        assert "insertText" not in str(reqs)
        ts = req["updateTextStyle"]
        assert "foregroundColor" in ts["textStyle"]
        assert ts["range"]["startIndex"] == 10

    def test_resplit_zero_mutations(self):
        """AC2: Run re-split with identical content -> zero mutations."""
        base_runs = ["hello world"]
        local_runs = ["hello ", "world"]  # Same text, different split
        reqs = _tree_style_diff_requests(base_runs, local_runs, 10, "t1")
        assert reqs == []

    def test_resplit_styled_zero_mutations(self):
        """Styled run re-split with same styles -> zero mutations."""
        base_runs = [{"t": "hello world", "b": True}]
        local_runs = [{"t": "hello ", "b": True}, {"t": "world", "b": True}]
        reqs = _tree_style_diff_requests(base_runs, local_runs, 10, "t1")
        assert reqs == []

    def test_text_differs_returns_empty(self):
        """When text differs, style diff returns empty (not its job)."""
        reqs = _tree_style_diff_requests(["hello"], ["world"], 10, "t1")
        assert reqs == []

    def test_partial_bold(self):
        """Bold applied to part of text -> updateTextStyle for that range."""
        base_runs = ["hello world"]
        local_runs = [{"t": "hello ", "b": True}, "world"]
        reqs = _tree_style_diff_requests(base_runs, local_runs, 10, "t1")
        assert len(reqs) == 1
        ts = reqs[0]["updateTextStyle"]
        assert ts["textStyle"]["bold"] is True
        assert ts["range"]["startIndex"] == 10
        assert ts["range"]["endIndex"] == 16  # "hello " is 6 chars

    def test_tab_id_propagated(self):
        reqs = _tree_style_diff_requests(
            ["hi"], [{"t": "hi", "b": True}], 5, "tab99"
        )
        assert reqs[0]["updateTextStyle"]["range"]["tabId"] == "tab99"


# =============================================================================
# Paragraph style diff
# =============================================================================


class TestTreeParaStyleDiff:
    def test_alignment_change(self):
        reqs = _tree_para_style_diff_requests(
            {}, {"align": "center"}, 10, 20, "t1"
        )
        assert len(reqs) == 1
        ps = reqs[0]["updateParagraphStyle"]
        assert ps["paragraphStyle"]["alignment"] == "CENTER"

    def test_no_change_no_mutations(self):
        reqs = _tree_para_style_diff_requests(
            {"align": "center"}, {"align": "center"}, 10, 20, "t1"
        )
        assert reqs == []

    def test_space_above_change(self):
        reqs = _tree_para_style_diff_requests(
            {}, {"space_above": 12}, 10, 20, "t1"
        )
        assert len(reqs) == 1
        ps = reqs[0]["updateParagraphStyle"]["paragraphStyle"]
        assert ps["spaceAbove"]["magnitude"] == 12


# =============================================================================
# Heading level requests
# =============================================================================


class TestTreeHeadingLevelRequests:
    def test_heading_level_change(self):
        reqs = _tree_heading_level_requests("h2", "h3", 10, 20, "t1")
        assert len(reqs) == 1
        ps = reqs[0]["updateParagraphStyle"]
        assert ps["paragraphStyle"]["namedStyleType"] == "HEADING_3"

    def test_same_level_no_change(self):
        reqs = _tree_heading_level_requests("h2", "h2", 10, 20, "t1")
        assert reqs == []

    def test_heading_to_paragraph(self):
        reqs = _tree_heading_level_requests("h2", "p", 10, 20, "t1")
        assert len(reqs) == 1
        ps = reqs[0]["updateParagraphStyle"]
        assert ps["paragraphStyle"]["namedStyleType"] == "NORMAL_TEXT"

    def test_paragraph_to_heading(self):
        reqs = _tree_heading_level_requests("p", "h1", 10, 20, "t1")
        assert len(reqs) == 1
        ps = reqs[0]["updateParagraphStyle"]
        assert ps["paragraphStyle"]["namedStyleType"] == "HEADING_1"


# =============================================================================
# compute_tree_plan integration tests
# =============================================================================


class TestComputeTreePlan:
    """Integration tests for compute_tree_plan.

    These build raw Google Docs body JSON as remote, compress it through
    compress_doc, and pass modified tree bodies as local to verify the
    full pipeline.
    """

    def _call(
        self,
        remote_body: list[dict],
        local_tree_body: list,
        local_appendix: dict | None = None,
        remote_revision: str = "rev1",
        stored_revision: str = "rev1",
        tab_id: str = "t1",
        lists: dict | None = None,
    ) -> ThreeWayPlan:
        return compute_tree_plan(
            local_tree_body=local_tree_body,
            local_appendix=local_appendix,
            remote_body=remote_body,
            remote_revision=remote_revision,
            stored_revision=stored_revision,
            tab_id=tab_id,
            lists=lists,
        )

    # --- AC4: Untouched blocks -> zero mutations ---

    def test_untouched_zero_mutations(self):
        """AC4: Identical local=remote -> zero mutations."""
        remote = [_make_para_element("Hello world", 1)]
        # Compress and use as local (identical)
        from gax.gdoc.tree import compress_doc
        compressed = compress_doc(remote)
        local = compressed.body

        plan = self._call(remote, local)
        assert plan.error is None
        assert plan.mutations == []

    def test_untouched_two_blocks(self):
        """Two blocks, both unchanged -> zero mutations."""
        remote = [
            _make_para_element("First paragraph", 1),
            _make_para_element("Second paragraph", 18),
        ]
        from gax.gdoc.tree import compress_doc
        compressed = compress_doc(remote)
        plan = self._call(remote, compressed.body)
        assert plan.error is None
        assert plan.mutations == []

    # --- Revision guard ---

    def test_revision_mismatch_refused(self):
        """Revision guard: mismatch -> refuse with error."""
        remote = [_make_para_element("Hello", 1)]
        from gax.gdoc.tree import compress_doc
        local = compress_doc(remote).body

        plan = self._call(
            remote, local,
            remote_revision="rev2",
            stored_revision="rev1",
        )
        assert plan.error is not None
        assert "Pull first" in plan.error
        assert plan.revision_changed is True

    def test_revision_match_ok(self):
        """Matching revisions -> no error."""
        remote = [_make_para_element("Hello", 1)]
        from gax.gdoc.tree import compress_doc
        local = compress_doc(remote).body

        plan = self._call(
            remote, local,
            remote_revision="rev1",
            stored_revision="rev1",
        )
        assert plan.error is None

    # --- AC1: Color change -> updateTextStyle only ---

    def test_color_change_style_only(self):
        """AC1: Color change emits updateTextStyle, no delete/insert."""
        remote = [_make_para_element("Hello world", 1)]
        # Local tree: same text, color added
        local = [{"p": {"t": "Hello world", "color": "#ff0000"}}]

        plan = self._call(remote, local)
        assert plan.error is None
        assert len(plan.mutations) > 0

        # Must have updateTextStyle
        style_muts = [m for m in plan.mutations if "updateTextStyle" in m]
        assert len(style_muts) >= 1

        # Must NOT have delete/insert
        deletes = [m for m in plan.mutations if "deleteContentRange" in m]
        inserts = [m for m in plan.mutations if "insertText" in m]
        assert deletes == []
        assert inserts == []

    def test_bold_change_style_only(self):
        """Bold change emits updateTextStyle, no delete/insert."""
        remote = [_make_para_element("Hello", 1)]
        local = [{"p": {"t": "Hello", "b": True}}]

        plan = self._call(remote, local)
        assert plan.error is None

        style_muts = [m for m in plan.mutations if "updateTextStyle" in m]
        assert len(style_muts) >= 1
        assert style_muts[0]["updateTextStyle"]["textStyle"].get("bold") is True

        deletes = [m for m in plan.mutations if "deleteContentRange" in m]
        inserts = [m for m in plan.mutations if "insertText" in m]
        assert deletes == []
        assert inserts == []

    # --- AC2: Run re-split with identical content -> zero mutations ---

    def test_resplit_zero_mutations(self):
        """AC2: Same text, different run splits -> zero mutations."""
        remote = [_make_para_element("Hello world", 1)]
        # Local splits differently but content identical
        local = [{"p": {"runs": ["Hello ", "world"]}}]

        plan = self._call(remote, local)
        assert plan.error is None
        assert plan.mutations == []

    # --- AC3: Appendix edit -> rejected ---

    def test_appendix_modification_rejected(self):
        """AC3: Editing appendix entry -> rejected pre-plan."""
        from gax.gdoc.tree import compress_doc, extract_appendix

        # Create a remote with paragraph style that has _raw_ps
        remote = [
            {
                "startIndex": 1,
                "endIndex": 8,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 1,
                            "endIndex": 8,
                            "textRun": {
                                "content": "Hello\n",
                                "textStyle": {},
                            },
                        }
                    ],
                    "paragraphStyle": {
                        "namedStyleType": "NORMAL_TEXT",
                        "lineSpacing": 115,
                        "direction": "LEFT_TO_RIGHT",
                        "spacingMode": "COLLAPSE_LISTS",
                        "avoidWidowAndOrphan": False,
                    },
                },
            }
        ]
        compressed = compress_doc(remote)
        app_result = extract_appendix(compressed.body)

        # Only test if there IS an appendix (depends on compression rules)
        if app_result.appendix:
            # Mutate a value in the appendix
            modified_appendix = dict(app_result.appendix)
            first_key = next(iter(modified_appendix))
            modified_appendix[first_key] = {"TAMPERED": True}

            plan = self._call(
                remote, app_result.body,
                local_appendix=modified_appendix,
            )
            assert plan.error is not None
            assert "Appendix" in plan.error or "appendix" in plan.error.lower()
        else:
            # No appendix extracted -- test with a synthetic one
            plan = self._call(
                remote, compressed.body,
                local_appendix={"r0": {"_raw_ps": {"x": 1}}},
            )
            # Should still reject since remote has no such appendix entry
            assert plan.error is not None

    # --- Text change splice ---

    def test_text_change_produces_splice(self):
        """Changed text -> delete+insert mutations."""
        remote = [_make_para_element("Hello world", 1)]
        local = [{"p": "Hello universe"}]

        plan = self._call(remote, local)
        assert plan.error is None
        assert len(plan.mutations) > 0
        # Should have either delete/insert for the text splice
        mut_types = set()
        for m in plan.mutations:
            for k in m:
                mut_types.add(k)
        assert "deleteContentRange" in mut_types or "insertText" in mut_types

    # --- Heading level change ---

    def test_heading_level_change(self):
        """h2 -> h3 produces updateParagraphStyle with HEADING_3."""
        remote = [_make_heading_element("Title", 1, level=2)]
        local = [{"h3": "Title"}]

        plan = self._call(remote, local)
        assert plan.error is None
        style_muts = [m for m in plan.mutations if "updateParagraphStyle" in m]
        assert len(style_muts) >= 1
        found_heading = False
        for m in style_muts:
            ps = m["updateParagraphStyle"]["paragraphStyle"]
            if ps.get("namedStyleType") == "HEADING_3":
                found_heading = True
        assert found_heading

    # --- Paragraph style change ---

    def test_paragraph_alignment_change(self):
        """Paragraph alignment change -> updateParagraphStyle."""
        remote = [_make_para_element("Hello", 1)]
        local = [{"p": {"t": "Hello", "style": {"align": "center"}}}]

        plan = self._call(remote, local)
        assert plan.error is None
        style_muts = [m for m in plan.mutations if "updateParagraphStyle" in m]
        assert len(style_muts) >= 1
        found_align = False
        for m in style_muts:
            ps = m["updateParagraphStyle"]["paragraphStyle"]
            if ps.get("alignment") == "CENTER":
                found_align = True
        assert found_align

    # --- Block insertion ---

    def test_insert_new_block(self):
        """Inserting a new paragraph produces insertText."""
        remote = [_make_para_element("Existing", 1)]
        from gax.gdoc.tree import compress_doc
        base = compress_doc(remote).body
        # Add a new paragraph to local
        local = list(base) + [{"p": "New paragraph"}]

        plan = self._call(remote, local)
        assert plan.error is None
        inserts = [m for m in plan.mutations if "insertText" in m]
        assert len(inserts) >= 1
        assert "New paragraph" in inserts[0]["insertText"]["text"]

    # --- Block deletion ---

    def test_delete_block(self):
        """Removing a block produces deleteContentRange."""
        remote = [
            _make_para_element("Keep this", 1),
            _make_para_element("Delete this", 12),
        ]
        from gax.gdoc.tree import compress_doc
        base = compress_doc(remote).body
        # Remove second block
        local = [base[0]]

        plan = self._call(remote, local)
        assert plan.error is None
        deletes = [m for m in plan.mutations if "deleteContentRange" in m]
        assert len(deletes) >= 1

    # --- Mutation ordering ---

    def test_mutations_reverse_sorted(self):
        """Mutations sorted by descending startIndex."""
        remote = [
            _make_para_element("First", 1),
            _make_para_element("Second", 8),
        ]
        # Both blocks change style
        local = [
            {"p": {"t": "First", "b": True}},
            {"p": {"t": "Second", "b": True}},
        ]

        plan = self._call(remote, local)
        assert plan.error is None
        if len(plan.mutations) >= 2:
            indices = []
            for m in plan.mutations:
                for val in m.values():
                    if isinstance(val, dict):
                        r = val.get("range") or val.get("location")
                        if r and "startIndex" in r:
                            indices.append(r["startIndex"])
                            break
            # Should be descending
            assert indices == sorted(indices, reverse=True)

    # --- Unsupported block type rejection ---

    def test_toc_edit_rejected(self):
        """Editing a TOC block is rejected."""
        from gax.gdoc.diff_push import _tree_diff_equal_range
        from gax.gdoc.ir import Paragraph, Span

        # Simulate equal-range with toc blocks that differ
        base_flat = [{"toc": {"entries": ["a"]}}]
        local_flat = [{"toc": {"entries": ["b"]}}]
        remote_blocks = [Paragraph(doc_range=(1, 10), spans=[Span("x")])]
        tree_to_block = {0: 0}

        result = _tree_diff_equal_range(
            base_flat, local_flat, 0, 1, 0, 1,
            tree_to_block, remote_blocks, "t1",
        )
        assert isinstance(result, str)
        assert "toc" in result.lower()


# =============================================================================
# Edge cases
# =============================================================================


class TestTreePlanEdgeCases:
    def test_empty_remote_empty_local(self):
        """Empty doc -> empty local -> zero mutations."""
        plan = compute_tree_plan(
            local_tree_body=[],
            local_appendix=None,
            remote_body=[],
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t1",
        )
        assert plan.error is None
        assert plan.mutations == []

    def test_heading_to_paragraph(self):
        """h2 -> p produces namedStyleType=NORMAL_TEXT."""
        remote = [_make_heading_element("Title", 1, level=2)]
        local = [{"p": "Title"}]

        plan = compute_tree_plan(
            local_tree_body=local,
            local_appendix=None,
            remote_body=remote,
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t1",
        )
        assert plan.error is None
        style_muts = [m for m in plan.mutations if "updateParagraphStyle" in m]
        found_normal = False
        for m in style_muts:
            ps = m["updateParagraphStyle"]["paragraphStyle"]
            if ps.get("namedStyleType") == "NORMAL_TEXT":
                found_normal = True
        assert found_normal

    def test_multiple_style_changes_in_one_block(self):
        """Bold + color on same block -> combined style mutations."""
        remote = [_make_para_element("Hello", 1)]
        local = [{"p": {"t": "Hello", "b": True, "color": "#ff0000"}}]

        plan = compute_tree_plan(
            local_tree_body=local,
            local_appendix=None,
            remote_body=remote,
            remote_revision="rev1",
            stored_revision="rev1",
            tab_id="t1",
        )
        assert plan.error is None
        style_muts = [m for m in plan.mutations if "updateTextStyle" in m]
        assert len(style_muts) >= 1
        # Should have both bold and foregroundColor
        ts = style_muts[0]["updateTextStyle"]["textStyle"]
        assert ts.get("bold") is True
        assert "foregroundColor" in ts
