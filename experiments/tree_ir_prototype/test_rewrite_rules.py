"""Per-rule inverse property tests and whole-document round-trip tests.

Tests the core invariant: expand(compress(node)) == node for every
node type matched by each rewrite rule.

Run with:
    direnv exec . python -m pytest experiments/tree_ir_prototype/test_rewrite_rules.py -v
"""

from __future__ import annotations

import copy
import json

import pytest

from .rewrite_rules import (
    ALL_RULES,
    CompressResult,
    compress_doc,
    coverage_report,
    expand_doc,
    extract_appendix,
    resolve_appendix,
    heading_rule,
    list_item_rule,
    paragraph_rule,
    table_rule,
    _compress_text_runs,
    _compress_text_style,
    _expand_text_runs,
    _expand_text_style,
    _strip_indices,
)


# =============================================================================
# Fixture: raw structural elements for property testing
# =============================================================================


def _make_heading(text: str, level: int = 1, bold: bool = False) -> dict:
    """Create a raw heading structural element."""
    style = {"bold": True} if bold else {}
    return {
        "startIndex": 1,
        "endIndex": 1 + len(text) + 1,
        "paragraph": {
            "elements": [
                {
                    "startIndex": 1,
                    "endIndex": 1 + len(text) + 1,
                    "textRun": {
                        "content": text + "\n",
                        "textStyle": style,
                    },
                }
            ],
            "paragraphStyle": {
                "namedStyleType": f"HEADING_{level}",
                "direction": "LEFT_TO_RIGHT",
            },
        },
    }


def _make_paragraph(text: str, style: dict | None = None, para_style: dict | None = None) -> dict:
    """Create a raw paragraph structural element."""
    text_style = style or {}
    ps = para_style or {}
    ps.setdefault("namedStyleType", "NORMAL_TEXT")
    return {
        "startIndex": 1,
        "endIndex": 1 + len(text) + 1,
        "paragraph": {
            "elements": [
                {
                    "startIndex": 1,
                    "endIndex": 1 + len(text) + 1,
                    "textRun": {
                        "content": text + "\n",
                        "textStyle": text_style,
                    },
                }
            ],
            "paragraphStyle": ps,
        },
    }


def _make_styled_paragraph(runs: list[tuple[str, dict]], para_style: dict | None = None) -> dict:
    """Create a paragraph with multiple styled runs."""
    ps = para_style or {}
    ps.setdefault("namedStyleType", "NORMAL_TEXT")
    elements = []
    idx = 1
    for i, (text, style) in enumerate(runs):
        content = text if i < len(runs) - 1 else text + "\n"
        elements.append({
            "startIndex": idx,
            "endIndex": idx + len(content),
            "textRun": {
                "content": content,
                "textStyle": style,
            },
        })
        idx += len(content)
    return {
        "startIndex": 1,
        "endIndex": idx,
        "paragraph": {
            "elements": elements,
            "paragraphStyle": ps,
        },
    }


def _make_list_item(text: str, list_id: str = "kix.abc123", nesting: int = 0) -> dict:
    """Create a raw list item structural element."""
    return {
        "startIndex": 1,
        "endIndex": 1 + len(text) + 1,
        "paragraph": {
            "elements": [
                {
                    "startIndex": 1,
                    "endIndex": 1 + len(text) + 1,
                    "textRun": {
                        "content": text + "\n",
                        "textStyle": {},
                    },
                }
            ],
            "paragraphStyle": {
                "namedStyleType": "NORMAL_TEXT",
                "indentStart": {"magnitude": 36, "unit": "PT"},
                "indentFirstLine": {"magnitude": 18, "unit": "PT"},
            },
            "bullet": {
                "listId": list_id,
                "nestingLevel": nesting,
            },
        },
    }


def _make_table(cells: list[list[str]]) -> dict:
    """Create a raw table structural element."""
    table_rows = []
    idx = 1
    for row in cells:
        table_cells = []
        for text in row:
            cell_start = idx
            cell_end = idx + len(text) + 1
            table_cells.append({
                "content": [{
                    "startIndex": cell_start,
                    "endIndex": cell_end,
                    "paragraph": {
                        "elements": [{
                            "startIndex": cell_start,
                            "endIndex": cell_end,
                            "textRun": {
                                "content": text + "\n",
                                "textStyle": {},
                            },
                        }],
                        "paragraphStyle": {
                            "namedStyleType": "NORMAL_TEXT",
                            "lineSpacing": 100,
                            "spacingMode": "COLLAPSE_LISTS",
                            "borderBetween": {"color": {}, "width": {"unit": "PT"}, "padding": {"unit": "PT"}, "dashStyle": "SOLID"},
                            "borderTop": {"color": {}, "width": {"unit": "PT"}, "padding": {"unit": "PT"}, "dashStyle": "SOLID"},
                            "borderBottom": {"color": {}, "width": {"unit": "PT"}, "padding": {"unit": "PT"}, "dashStyle": "SOLID"},
                            "borderLeft": {"color": {}, "width": {"unit": "PT"}, "padding": {"unit": "PT"}, "dashStyle": "SOLID"},
                            "borderRight": {"color": {}, "width": {"unit": "PT"}, "padding": {"unit": "PT"}, "dashStyle": "SOLID"},
                            "keepLinesTogether": False,
                            "keepWithNext": False,
                            "avoidWidowAndOrphan": False,
                            "shading": {"backgroundColor": {}},
                            "pageBreakBefore": False,
                        },
                    },
                }],
            })
            idx = cell_end + 1
        table_rows.append({"tableCells": table_cells})
    return {
        "startIndex": 1,
        "endIndex": idx,
        "table": {
            "rows": len(cells),
            "columns": len(cells[0]) if cells else 0,
            "tableRows": table_rows,
        },
    }


# =============================================================================
# Per-rule inverse property tests
# =============================================================================


class TestHeadingRule:
    """Property tests for the heading rule."""

    @pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
    def test_inverse_plain_heading(self, level):
        """expand(compress(heading)) == heading for plain headings."""
        node = _strip_indices(_make_heading(f"Heading Level {level}", level=level))
        assert heading_rule.matches(node, "body_element")
        compact = heading_rule.compress(node)
        restored = heading_rule.expand(compact)
        assert restored == node, f"Level {level} round-trip failed"

    def test_inverse_styled_heading(self):
        """expand(compress(heading)) == heading for bold heading."""
        node = _strip_indices(_make_heading("Bold Heading", level=2, bold=True))
        compact = heading_rule.compress(node)
        restored = heading_rule.expand(compact)
        assert restored == node

    def test_inverse_heading_with_mixed_runs(self):
        """Heading with mixed bold/plain runs."""
        raw = _strip_indices(_make_styled_paragraph(
            [("Normal ", {}), ("Bold", {"bold": True}), (" End", {})],
            para_style={"namedStyleType": "HEADING_1"},
        ))
        assert heading_rule.matches(raw, "body_element")
        compact = heading_rule.compress(raw)
        restored = heading_rule.expand(compact)
        assert restored == raw

    def test_does_not_match_paragraph(self):
        """Heading rule does not match normal paragraphs."""
        node = _strip_indices(_make_paragraph("Not a heading"))
        assert not heading_rule.matches(node, "body_element")

    def test_does_not_match_wrong_context(self):
        """Heading rule does not match in non-body context."""
        node = _strip_indices(_make_heading("H1"))
        assert not heading_rule.matches(node, "table_cell")


class TestParagraphRule:
    """Property tests for the paragraph rule."""

    def test_inverse_plain(self):
        """expand(compress(p)) == p for plain paragraph."""
        node = _strip_indices(_make_paragraph("Hello world"))
        assert paragraph_rule.matches(node, "body_element")
        compact = paragraph_rule.compress(node)
        restored = paragraph_rule.expand(compact)
        assert restored == node

    def test_inverse_with_alignment(self):
        """Paragraph with CENTER alignment."""
        node = _strip_indices(_make_paragraph(
            "Centered text",
            para_style={"namedStyleType": "NORMAL_TEXT", "alignment": "CENTER"},
        ))
        compact = paragraph_rule.compress(node)
        restored = paragraph_rule.expand(compact)
        assert restored == node

    def test_inverse_styled_runs(self):
        """Paragraph with bold + italic + link runs."""
        raw = _strip_indices(_make_styled_paragraph([
            ("Plain ", {}),
            ("bold", {"bold": True}),
            (" and ", {}),
            ("link", {"link": {"url": "https://x.com"}, "underline": True,
                      "foregroundColor": {"color": {"rgbColor": {"red": 0.067, "green": 0.333, "blue": 0.8}}}}),
        ]))
        assert paragraph_rule.matches(raw, "body_element")
        compact = paragraph_rule.compress(raw)
        restored = paragraph_rule.expand(compact)
        assert restored == raw

    def test_inverse_with_font_size(self):
        """Paragraph with font size."""
        raw = _strip_indices(_make_styled_paragraph(
            [("Small text", {"fontSize": {"magnitude": 9, "unit": "PT"}})],
            para_style={"namedStyleType": "NORMAL_TEXT", "alignment": "CENTER"},
        ))
        compact = paragraph_rule.compress(raw)
        restored = paragraph_rule.expand(compact)
        assert restored == raw

    def test_does_not_match_heading(self):
        """Paragraph rule skips headings."""
        node = _strip_indices(_make_heading("Heading"))
        assert not paragraph_rule.matches(node, "body_element")

    def test_does_not_match_bullet(self):
        """Paragraph rule skips bullet items."""
        node = _strip_indices(_make_list_item("Item"))
        assert not paragraph_rule.matches(node, "body_element")


class TestListItemRule:
    """Property tests for the list item rule."""

    def test_inverse_simple(self):
        """expand(compress(li)) == li for simple list item."""
        node = _strip_indices(_make_list_item("First item"))
        assert list_item_rule.matches(node, "body_element")
        compact = list_item_rule.compress(node)
        restored = list_item_rule.expand(compact)
        assert restored == node

    def test_inverse_nested(self):
        """Nested list item with depth > 0."""
        node = _strip_indices(_make_list_item("Nested item", nesting=1))
        compact = list_item_rule.compress(node)
        restored = list_item_rule.expand(compact)
        assert restored == node

    def test_does_not_match_plain_paragraph(self):
        """List rule skips paragraphs without bullet."""
        node = _strip_indices(_make_paragraph("Not a list"))
        assert not list_item_rule.matches(node, "body_element")


class TestListGrouping:
    """Tests for list item grouping into ul/ol containers."""

    def test_consecutive_items_grouped(self):
        """Consecutive list items with same listId become one container."""
        doc = [
            _make_list_item("Item 1", list_id="kix.abc"),
            _make_list_item("Item 2", list_id="kix.abc"),
            _make_list_item("Item 3", list_id="kix.abc"),
        ]
        result = compress_doc(doc)
        # Should be one container
        assert len(result.body) == 1
        assert "ul" in result.body[0]
        assert len(result.body[0]["ul"]["items"]) == 3

    def test_different_lists_separate(self):
        """Different listIds produce separate containers."""
        doc = [
            _make_list_item("Item A", list_id="kix.abc"),
            _make_list_item("Item B", list_id="kix.xyz"),
        ]
        result = compress_doc(doc)
        assert len(result.body) == 2
        assert "ul" in result.body[0]
        assert "ul" in result.body[1]
        assert result.body[0]["ul"]["_listId"] == "kix.abc"
        assert result.body[1]["ul"]["_listId"] == "kix.xyz"

    def test_list_interrupted_by_paragraph(self):
        """Paragraph between list items splits into two containers."""
        doc = [
            _make_list_item("Item 1", list_id="kix.abc"),
            _make_paragraph("Interruption"),
            _make_list_item("Item 2", list_id="kix.abc"),
        ]
        result = compress_doc(doc)
        assert len(result.body) == 3
        assert "ul" in result.body[0]
        assert "p" in result.body[1]
        assert "ul" in result.body[2]

    def test_round_trip_identity(self):
        """expand(compress(doc)) == strip_indices(doc) with list grouping."""
        doc = [
            _make_list_item("First", list_id="kix.abc"),
            _make_list_item("Second", list_id="kix.abc"),
            _make_list_item("Third", list_id="kix.abc"),
        ]
        result = compress_doc(doc)
        restored = expand_doc(result.body)
        expected = [_strip_indices(n) for n in doc]
        assert restored == expected

    def test_nested_items_in_same_container(self):
        """Items with different nesting levels but same listId are grouped."""
        doc = [
            _make_list_item("Top", list_id="kix.abc", nesting=0),
            _make_list_item("Nested", list_id="kix.abc", nesting=1),
            _make_list_item("Top again", list_id="kix.abc", nesting=0),
        ]
        result = compress_doc(doc)
        assert len(result.body) == 1
        assert "ul" in result.body[0]
        items = result.body[0]["ul"]["items"]
        assert len(items) == 3
        assert items[1].get("depth") == 1

    def test_ordered_list_detection(self):
        """When lists dict has ordered glyph, container is 'ol'."""
        doc = [_make_list_item("Numbered", list_id="kix.ord")]
        lists_dict = {
            "kix.ord": {
                "listProperties": {
                    "nestingLevels": [{"glyphType": "DECIMAL"}]
                }
            }
        }
        result = compress_doc(doc, lists=lists_dict)
        assert "ol" in result.body[0]


class TestTableRule:
    """Property tests for the table rule."""

    def test_inverse_simple_table(self):
        """expand(compress(table)) == table for simple 2x2."""
        node = _strip_indices(_make_table([["A", "B"], ["C", "D"]]))
        assert table_rule.matches(node, "body_element")
        compact = table_rule.compress(node)
        restored = table_rule.expand(compact)
        assert restored == node

    def test_inverse_single_cell(self):
        """1x1 table."""
        node = _strip_indices(_make_table([["Only cell"]]))
        compact = table_rule.compress(node)
        restored = table_rule.expand(compact)
        assert restored == node

    def test_inverse_3x3(self):
        """3x3 table with varied content."""
        node = _strip_indices(_make_table([
            ["Header 1", "Header 2", "Header 3"],
            ["Row 1", "Data", "More"],
            ["Row 2", "", "End"],
        ]))
        compact = table_rule.compress(node)
        restored = table_rule.expand(compact)
        assert restored == node

    def test_table_cell_defaults_elided(self):
        """Table cell paragraph style defaults are elided in compact form."""
        node = _strip_indices(_make_table([["Cell"]]))
        compact = table_rule.compress(node)
        # The compact form should NOT have border/shading noise
        cell_compact = compact["table"]["rows"][0][0]
        if isinstance(cell_compact, dict) and "style" in cell_compact:
            style = cell_compact["style"]
            assert "borderTop" not in str(style), "Border defaults should be elided"
        # But it should round-trip
        restored = table_rule.expand(compact)
        assert restored == node

    def test_does_not_match_paragraph(self):
        """Table rule skips paragraphs."""
        node = _strip_indices(_make_paragraph("Not a table"))
        assert not table_rule.matches(node, "body_element")


class TestTextRunCompression:
    """Property tests for text run compress/expand."""

    def test_plain_text_round_trip(self):
        """Plain text → string → element."""
        elements = [{
            "textRun": {"content": "Hello\n", "textStyle": {}},
        }]
        compact = _compress_text_runs(elements)
        assert compact == ["Hello"]
        restored = _expand_text_runs(compact)
        assert restored == elements

    def test_bold_round_trip(self):
        """Bold text round-trips."""
        elements = [{
            "textRun": {"content": "Bold\n", "textStyle": {"bold": True}},
        }]
        compact = _compress_text_runs(elements)
        assert compact == [{"t": "Bold", "b": True}]
        restored = _expand_text_runs(compact)
        assert restored == elements

    def test_link_suppresses_underline_and_color(self):
        """Link-implied underline and blue color are suppressed."""
        elements = [{
            "textRun": {
                "content": "click\n",
                "textStyle": {
                    "link": {"url": "https://x.com"},
                    "underline": True,
                    "foregroundColor": {"color": {"rgbColor": {"red": 0.067, "green": 0.333, "blue": 0.8}}},
                },
            },
        }]
        compact = _compress_text_runs(elements)
        assert len(compact) == 1
        assert isinstance(compact[0], dict)
        assert "u" not in compact[0], "Underline should be suppressed for links"
        assert "color" not in compact[0], "Link blue should be suppressed"
        assert compact[0].get("url") == "https://x.com"

        # Round-trip restores them
        restored = _expand_text_runs(compact)
        assert restored == elements

    def test_non_link_underline_preserved(self):
        """Underline without link is preserved."""
        elements = [{
            "textRun": {"content": "underlined\n", "textStyle": {"underline": True}},
        }]
        compact = _compress_text_runs(elements)
        assert compact[0].get("u") is True

    def test_multiple_runs(self):
        """Multiple runs in one paragraph."""
        elements = [
            {"textRun": {"content": "Plain ", "textStyle": {}}},
            {"textRun": {"content": "bold", "textStyle": {"bold": True}}},
            {"textRun": {"content": " end\n", "textStyle": {}}},
        ]
        compact = _compress_text_runs(elements)
        assert compact == ["Plain ", {"t": "bold", "b": True}, " end"]
        restored = _expand_text_runs(compact)
        assert restored == elements


class TestTextStyleCompression:
    """Property tests for text style compress/expand."""

    @pytest.mark.parametrize("style,expected_keys", [
        ({}, set()),
        ({"bold": True}, {"b"}),
        ({"italic": True}, {"i"}),
        ({"strikethrough": True}, {"s"}),
        ({"underline": True}, {"u"}),
        ({"bold": True, "italic": True}, {"b", "i"}),
    ])
    def test_simple_styles(self, style, expected_keys):
        """Simple boolean styles compress to short keys."""
        compact = _compress_text_style(style)
        assert set(compact.keys()) == expected_keys

    def test_full_round_trip(self):
        """Complex style round-trips through compress/expand."""
        style = {
            "bold": True,
            "italic": True,
            "foregroundColor": {"color": {"rgbColor": {"red": 0.8}}},
            "fontSize": {"magnitude": 14, "unit": "PT"},
            "weightedFontFamily": {"fontFamily": "Courier New", "weight": 400},
        }
        compact = _compress_text_style(style)
        restored = _expand_text_style(compact)
        assert restored == style


# =============================================================================
# Whole-document round-trip
# =============================================================================


class TestWholeDocRoundTrip:
    """Whole-document: expand(compress(body)) == strip_indices(body)."""

    def test_mixed_document(self):
        """A document with headings, paragraphs, lists, and a table."""
        body = [
            {"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}},
            _make_heading("Introduction", level=1),
            _make_paragraph("First paragraph of the doc."),
            _make_styled_paragraph([
                ("Normal ", {}),
                ("bold", {"bold": True}),
                (" and ", {}),
                ("italic", {"italic": True}),
            ]),
            _make_list_item("Item one"),
            _make_list_item("Item two"),
            _make_paragraph("Centered", para_style={
                "namedStyleType": "NORMAL_TEXT",
                "alignment": "CENTER",
            }),
            _make_table([["Col A", "Col B"], ["Data 1", "Data 2"]]),
            _make_paragraph("Final paragraph."),
        ]

        # Strip indices for comparison (serialized form has no indices)
        expected = [_strip_indices(e) for e in body if "sectionBreak" not in e]

        result = compress_doc(body)
        restored = expand_doc(result.body)

        assert len(restored) == len(expected), (
            f"Block count mismatch: {len(restored)} vs {len(expected)}"
        )
        for i, (exp, got) in enumerate(zip(expected, restored)):
            assert got == exp, (
                f"Block {i} mismatch:\n"
                f"  Expected: {json.dumps(exp, indent=2)[:200]}\n"
                f"  Got:      {json.dumps(got, indent=2)[:200]}"
            )

    def test_coverage_metric(self):
        """Coverage metric reports correct counts."""
        body = [
            {"startIndex": 0, "endIndex": 1, "sectionBreak": {"sectionStyle": {}}},
            _make_heading("H1", level=1),
            _make_paragraph("Para"),
            _make_list_item("Li"),
            _make_table([["Cell"]]),
        ]
        result = compress_doc(body)
        report = coverage_report(result)
        assert report["total_nodes"] == 4
        assert report["rewritten"] == 4
        assert report["verbatim"] == 0
        assert report["coverage_pct"] == 100.0
        assert report["per_rule"]["heading"] == 1
        assert report["per_rule"]["paragraph"] == 1
        assert report["per_rule"]["list_item"] == 1
        assert report["per_rule"]["table"] == 1

    def test_unknown_element_goes_verbatim(self):
        """An element with no matching rule passes through verbatim."""
        body = [
            _make_paragraph("Normal"),
            {"startIndex": 50, "endIndex": 60, "tableOfContents": {"content": []}},
        ]
        result = compress_doc(body)
        assert result.verbatim_count == 1
        report = coverage_report(result)
        assert report["coverage_pct"] == 50.0

        # Round-trip still works
        restored = expand_doc(result.body)
        assert len(restored) == 2
        # The verbatim element is preserved (sans indices)
        assert "tableOfContents" in restored[1]


# =============================================================================
# Appendix extraction and resolution
# =============================================================================


class TestAppendix:
    """Tests for appendix extraction (ref:rNN) and resolution."""

    def test_extract_moves_raw_ps_to_appendix(self):
        """Table cell _raw_ps is moved to appendix with a ref."""
        body = [{"table": {"rows": [[{
            "t": "Cell",
            "_raw_ps": {"namedStyleType": "NORMAL_TEXT", "lineSpacing": 100},
        }]]}}]
        result = extract_appendix(body)
        # Body should have ref instead of raw dict
        cell = result.body[0]["table"]["rows"][0][0]
        assert cell["_raw_ps"].startswith("ref:")
        # Appendix should have the payload
        ref_key = cell["_raw_ps"][4:]
        assert ref_key in result.appendix
        assert result.appendix[ref_key]["lineSpacing"] == 100

    def test_extract_moves_cellstyle_to_appendix(self):
        """Table cell _cellStyle is moved to appendix."""
        body = [{"table": {"rows": [[{
            "t": "Cell",
            "_cellStyle": {"rowSpan": 1, "columnSpan": 1},
        }]]}}]
        result = extract_appendix(body)
        cell = result.body[0]["table"]["rows"][0][0]
        assert cell["_cellStyle"].startswith("ref:")

    def test_extract_moves_verbatim_to_appendix(self):
        """Top-level _verbatim nodes go to appendix."""
        body = [{"_verbatim": {"tableOfContents": {"content": []}}}]
        result = extract_appendix(body)
        assert result.body[0]["_verbatim"].startswith("ref:")

    def test_resolve_restores_from_appendix(self):
        """resolve_appendix restores ref:rNN to original payloads."""
        body = [{"table": {"rows": [[{
            "t": "Cell",
            "_raw_ps": {"namedStyleType": "NORMAL_TEXT", "lineSpacing": 100},
            "_cellStyle": {"rowSpan": 1},
        }]]}}]
        extracted = extract_appendix(body)
        resolved = resolve_appendix(extracted.body, extracted.appendix)
        # Resolved should match original
        assert resolved == body

    def test_resolve_with_truncated_appendix(self):
        """Missing refs (truncated appendix) are left as-is."""
        body_with_refs = [{"_verbatim": "ref:r99"}]
        resolved = resolve_appendix(body_with_refs, {})
        # Ref left as-is since it's missing from appendix
        assert resolved[0]["_verbatim"] == "ref:r99"

    def test_round_trip_with_appendix(self):
        """Full round-trip: compress → extract → resolve → expand == original."""
        # Create a table with raw para styles (like the live API produces)
        table_node = _strip_indices(_make_table([["A", "B"], ["C", "D"]]))
        # Manually add table cell para styles to trigger appendix extraction
        for row in table_node["table"]["tableRows"]:
            for cell in row["tableCells"]:
                cell["tableCellStyle"] = {
                    "rowSpan": 1, "columnSpan": 1,
                    "backgroundColor": {},
                    "paddingLeft": {"magnitude": 5, "unit": "PT"},
                }

        body = [table_node]
        result = compress_doc(body)
        extracted = extract_appendix(result.body)
        # Verify appendix is non-empty (raw payloads extracted)
        assert len(extracted.appendix) > 0
        # Resolve and expand
        resolved = resolve_appendix(extracted.body, extracted.appendix)
        restored = expand_doc(resolved)
        expected = [_strip_indices(n) for n in body]
        assert restored == expected

    def test_no_verbatim_api_dicts_inline(self):
        """After extraction, body has no dict-valued _raw_ps or _cellStyle."""
        body = [{"table": {"rows": [[{
            "t": "Cell",
            "_raw_ps": {"big": "blob"},
            "_cellStyle": {"lots": "of data"},
        }]]}}]
        result = extract_appendix(body)
        # Walk body and assert no dict values for appendix keys
        import json
        body_str = json.dumps(result.body)
        # All _raw_ps and _cellStyle values should be "ref:rNN" strings
        cell = result.body[0]["table"]["rows"][0][0]
        assert isinstance(cell["_raw_ps"], str)
        assert isinstance(cell["_cellStyle"], str)

    def test_body_only_token_count(self):
        """Measure token savings from appendix extraction."""
        import json
        body = [{"table": {"rows": [[{
            "t": "Cell text",
            "_raw_ps": {"namedStyleType": "NORMAL_TEXT", "lineSpacing": 100,
                       "direction": "LEFT_TO_RIGHT", "spacingMode": "COLLAPSE_LISTS",
                       "keepLinesTogether": False, "keepWithNext": False,
                       "avoidWidowAndOrphan": False, "pageBreakBefore": False},
            "_cellStyle": {"rowSpan": 1, "columnSpan": 1, "backgroundColor": {},
                          "paddingLeft": {"magnitude": 5, "unit": "PT"},
                          "paddingRight": {"magnitude": 5, "unit": "PT"}},
        }]]}}]
        result = extract_appendix(body)
        body_chars = len(json.dumps(result.body))
        appendix_chars = len(json.dumps(result.appendix))
        total_chars = len(json.dumps(body))
        # Body-only should be significantly smaller than original
        assert body_chars < total_chars
        # Print for report
        print(f"\n  Body-only: {body_chars} chars")
        print(f"  Appendix:  {appendix_chars} chars")
        print(f"  Total (inline): {total_chars} chars")
        print(f"  Body/Total ratio: {body_chars/total_chars:.2f}")


# =============================================================================
# Live API round-trip test
# =============================================================================


@pytest.mark.e2e
class TestLiveDocRoundTrip:
    """Live API: expand(compress(doc)) == strip_indices(doc) for rich scratch doc."""

    def test_full_rich_doc_round_trip(self, docs_service, scratch_doc):
        """The rich scratch doc round-trips through compress/expand."""
        from .conftest import populate_rich_doc

        doc_id = scratch_doc
        doc = populate_rich_doc(docs_service, doc_id)

        tab = doc.get("tabs", [{}])[0]
        body_content = tab.get("documentTab", {}).get("body", {}).get("content", [])
        lists = tab.get("documentTab", {}).get("lists", {})

        # Compress
        result = compress_doc(body_content, lists=lists)
        report = coverage_report(result)

        print(f"\n  Coverage: {report['coverage_pct']:.1f}%")
        print(f"  Per rule: {report['per_rule']}")
        print(f"  Verbatim: {report['verbatim']}")

        # Expand back
        restored = expand_doc(result.body)

        # Compare against index-stripped original
        expected = [_strip_indices(e) for e in body_content if "sectionBreak" not in e]

        assert len(restored) == len(expected), (
            f"Block count: {len(restored)} vs {len(expected)}"
        )

        for i, (exp, got) in enumerate(zip(expected, restored)):
            assert got == exp, (
                f"Block {i} round-trip failed.\n"
                f"  Expected keys: {list(exp.keys())}\n"
                f"  Got keys: {list(got.keys())}\n"
                f"  Diff: {_dict_diff(exp, got)}"
            )

        # Assert high coverage
        assert report["coverage_pct"] >= 80.0, (
            f"Expected ≥80% coverage, got {report['coverage_pct']:.1f}%"
        )


# =============================================================================
# Enriched IR suppression tests (gax-24m)
# =============================================================================


class TestTableCellDefaultSuppression:
    """Unit tests for table-cell default paragraph style suppression (gax-24m)."""

    def _make_cell_body(self, para_style: dict) -> list[dict]:
        """Wrap a paragraphStyle in minimal table-body content."""
        return [{
            "startIndex": 1,
            "endIndex": 10,
            "table": {
                "rows": 1,
                "columns": 1,
                "tableRows": [{
                    "tableCells": [{
                        "content": [{
                            "startIndex": 1,
                            "endIndex": 10,
                            "paragraph": {
                                "elements": [{
                                    "startIndex": 1,
                                    "endIndex": 10,
                                    "textRun": {
                                        "content": "Cell text\n",
                                        "textStyle": {},
                                    },
                                }],
                                "paragraphStyle": para_style,
                            },
                        }],
                    }],
                }],
            },
        }]

    def test_border_defaults_not_in_raw(self):
        """Known border defaults are stripped from cell ParagraphStyle.raw."""
        from .enriched_ir import Table, from_doc_json

        para_style = {
            "namedStyleType": "NORMAL_TEXT",
            "lineSpacing": 100,
            "borderTop": {
                "color": {}, "width": {"unit": "PT"},
                "padding": {"unit": "PT"}, "dashStyle": "SOLID",
            },
            "borderLeft": {
                "color": {}, "width": {"unit": "PT"},
                "padding": {"unit": "PT"}, "dashStyle": "SOLID",
            },
            "keepLinesTogether": False,
            "shading": {"backgroundColor": {}},
            "pageBreakBefore": False,
        }
        blocks = from_doc_json(self._make_cell_body(para_style))
        assert len(blocks) == 1
        assert isinstance(blocks[0], Table)
        cell_ps = blocks[0].cell_styles[0][0]
        # raw should be None (all keys were defaults)
        assert cell_ps.raw is None, f"Expected raw=None, got: {cell_ps.raw}"

    def test_line_spacing_100_suppressed(self):
        """lineSpacing=100 (table-cell default) is suppressed from IR."""
        from .enriched_ir import Table, from_doc_json

        para_style = {"namedStyleType": "NORMAL_TEXT", "lineSpacing": 100}
        blocks = from_doc_json(self._make_cell_body(para_style))
        cell_ps = blocks[0].cell_styles[0][0]
        assert cell_ps.line_spacing is None, (
            f"lineSpacing=100 should be suppressed, got: {cell_ps.line_spacing}"
        )

    def test_non_default_line_spacing_kept(self):
        """Non-default lineSpacing (e.g. 115) is kept in the IR."""
        from .enriched_ir import Table, from_doc_json

        para_style = {"namedStyleType": "NORMAL_TEXT", "lineSpacing": 115}
        blocks = from_doc_json(self._make_cell_body(para_style))
        cell_ps = blocks[0].cell_styles[0][0]
        assert cell_ps.line_spacing == 115

    def test_spacingmode_default_suppressed(self):
        """spacingMode=COLLAPSE_LISTS (table-cell noise) is suppressed from raw."""
        from .enriched_ir import Table, from_doc_json

        para_style = {
            "namedStyleType": "NORMAL_TEXT",
            "spacingMode": "COLLAPSE_LISTS",
        }
        blocks = from_doc_json(self._make_cell_body(para_style))
        cell_ps = blocks[0].cell_styles[0][0]
        assert cell_ps.raw is None or "spacingMode" not in (cell_ps.raw or {})

    def test_yaml_contains_no_border_noise(self):
        """YAML output for table with cell border defaults has no border keys."""
        from .enriched_ir import from_doc_json
        from .yaml_serializer import serialize_tree

        para_style = {
            "namedStyleType": "NORMAL_TEXT",
            "lineSpacing": 100,
            "borderTop": {
                "color": {}, "width": {"unit": "PT"},
                "padding": {"unit": "PT"}, "dashStyle": "SOLID",
            },
            "borderBottom": {
                "color": {}, "width": {"unit": "PT"},
                "padding": {"unit": "PT"}, "dashStyle": "SOLID",
            },
            "keepLinesTogether": False,
            "keepWithNext": False,
            "avoidWidowAndOrphan": False,
            "shading": {"backgroundColor": {}},
            "pageBreakBefore": False,
            "spacingMode": "COLLAPSE_LISTS",
        }
        blocks = from_doc_json(self._make_cell_body(para_style))
        yaml_str = serialize_tree(blocks)
        assert "borderTop" not in yaml_str, "Border defaults must not appear in YAML"
        assert "borderBottom" not in yaml_str
        assert "keepLinesTogether" not in yaml_str
        assert "shading" not in yaml_str
        assert "spacingMode" not in yaml_str


def _dict_diff(a: dict, b: dict, path: str = "") -> str:
    """Find first difference between two nested dicts."""
    if a == b:
        return "equal"
    if type(a) != type(b):
        return f"{path}: type {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        all_keys = set(a.keys()) | set(b.keys())
        for k in sorted(all_keys):
            if k not in a:
                return f"{path}.{k}: missing in left"
            if k not in b:
                return f"{path}.{k}: missing in right"
            diff = _dict_diff(a[k], b[k], f"{path}.{k}")
            if diff != "equal":
                return diff
    elif isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: list len {len(a)} vs {len(b)}"
        for i, (ai, bi) in enumerate(zip(a, b)):
            diff = _dict_diff(ai, bi, f"{path}[{i}]")
            if diff != "equal":
                return diff
    else:
        return f"{path}: {a!r} vs {b!r}"
    return "equal"
