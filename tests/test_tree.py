"""Per-rule inverse property tests, schema validation, and whole-document
round-trip tests for gax.gdoc.tree (production Tree IR module).

Ported from experiments/tree_ir_prototype/test_rewrite_rules.py and
test_schema.py per gax-cvi.6.

Run with:
    direnv exec . uv run python -m pytest tests/test_tree.py -v
"""

from __future__ import annotations

import json

import pytest
import yaml

from gax.gdoc.tree import (
    compress_doc,
    coverage_report,
    expand_doc,
    extract_appendix,
    resolve_appendix,
    heading_rule,
    list_item_rule,
    paragraph_rule,
    table_rule,
    toc_rule,
    _compress_text_runs,
    _compress_text_style,
    _expand_text_runs,
    _expand_text_style,
    _strip_indices,
    # Schema exports
    SchemaError,
    SchemaValidationError,
    convert_typed_attrs,
    validate,
    validate_appendix_immutable,
    validate_refs_immutable,
    validated_parse,
    _yaml_baseload,
)


# ============================================================================
# Fixture: raw structural elements for property testing
# ============================================================================


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


# ============================================================================
# Per-rule inverse property tests
# ============================================================================


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


class TestTocRule:
    """Property tests for the tableOfContents passthrough rule."""

    def test_toc_matches(self):
        """TOC rule matches tableOfContents elements."""
        node = {"tableOfContents": {"content": []}}
        assert toc_rule.matches(node, "body_element")

    def test_toc_does_not_match_paragraph(self):
        node = _strip_indices(_make_paragraph("Not a TOC"))
        assert not toc_rule.matches(node, "body_element")

    def test_toc_round_trip(self):
        """expand(compress(toc)) == toc."""
        node = {"tableOfContents": {"content": [{"paragraph": {}}]}}
        compact = toc_rule.compress(node)
        assert "toc" in compact
        restored = toc_rule.expand(compact)
        assert restored == node

    def test_toc_in_compress_doc(self):
        """tableOfContents goes through the toc rule (not verbatim)."""
        body = [
            _make_paragraph("Normal"),
            {"startIndex": 50, "endIndex": 60, "tableOfContents": {"content": []}},
        ]
        result = compress_doc(body)
        assert result.verbatim_count == 0
        assert result.stats["toc"] == 1
        report = coverage_report(result)
        assert report["coverage_pct"] == 100.0

    def test_toc_expand_in_doc(self):
        """TOC round-trips through compress_doc/expand_doc."""
        body = [
            {"startIndex": 50, "endIndex": 60, "tableOfContents": {"content": [{"x": 1}]}},
        ]
        result = compress_doc(body)
        restored = expand_doc(result.body)
        expected = [_strip_indices(e) for e in body]
        assert restored == expected


class TestTextRunCompression:
    """Property tests for text run compress/expand."""

    def test_plain_text_round_trip(self):
        """Plain text -> string -> element."""
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


# ============================================================================
# Whole-document round-trip
# ============================================================================


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

    def test_toc_element_covered_not_verbatim(self):
        """A tableOfContents element is handled by the toc rule, not verbatim."""
        body = [
            _make_paragraph("Normal"),
            {"startIndex": 50, "endIndex": 60, "tableOfContents": {"content": []}},
        ]
        result = compress_doc(body)
        assert result.verbatim_count == 0
        report = coverage_report(result)
        assert report["coverage_pct"] == 100.0

        # Round-trip still works
        restored = expand_doc(result.body)
        assert len(restored) == 2
        assert "tableOfContents" in restored[1]


# ============================================================================
# Appendix extraction and resolution
# ============================================================================


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
        """Full round-trip: compress -> extract -> resolve -> expand == original."""
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
        # All _raw_ps and _cellStyle values should be "ref:rNN" strings
        cell = result.body[0]["table"]["rows"][0][0]
        assert isinstance(cell["_raw_ps"], str)
        assert isinstance(cell["_cellStyle"], str)


# ############################################################################
#
# SCHEMA TESTS
#
# ############################################################################


# ============================================================================
# Helpers
# ============================================================================


def _make_doc(**overrides) -> dict:
    """Build a minimal valid doc-tree/v1 document."""
    doc = {"kind": "doc-tree/v1", "body": []}
    doc.update(overrides)
    return doc


# ============================================================================
# BaseLoader lossless parsing tests
# ============================================================================


class TestBaseLoaderParsing:
    """Verify that BaseLoader preserves all scalar values as strings."""

    def test_no_stays_string(self):
        result = _yaml_baseload("value: No")
        assert result["value"] == "No"

    def test_yes_stays_string(self):
        result = _yaml_baseload("value: Yes")
        assert result["value"] == "Yes"

    def test_on_stays_string(self):
        result = _yaml_baseload("value: On")
        assert result["value"] == "On"

    def test_off_stays_string(self):
        result = _yaml_baseload("value: Off")
        assert result["value"] == "Off"

    def test_true_stays_string(self):
        result = _yaml_baseload("value: true")
        assert result["value"] == "true"

    def test_false_stays_string(self):
        result = _yaml_baseload("value: false")
        assert result["value"] == "false"

    def test_version_310_stays_string(self):
        result = _yaml_baseload("value: 3.10")
        assert result["value"] == "3.10"

    def test_leading_zeros_stay_string(self):
        result = _yaml_baseload("value: 007")
        assert result["value"] == "007"

    def test_null_stays_string(self):
        result = _yaml_baseload("value: null")
        assert result["value"] == "null"

    def test_integer_stays_string(self):
        result = _yaml_baseload("value: 42")
        assert result["value"] == "42"


# ============================================================================
# Typed attribute conversion tests
# ============================================================================


class TestConvertTypedAttrs:
    """Test that typed attributes are converted while text stays verbatim."""

    def test_bool_run_attrs_converted(self):
        doc = _make_doc(body=[{"p": {"runs": [{"t": "No", "b": "true", "i": "false"}]}}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        run = result["body"][0]["p"]["runs"][0]
        assert run["b"] is True
        assert run["i"] is False
        assert run["t"] == "No"  # Text stays verbatim!

    def test_size_converted_to_number(self):
        doc = _make_doc(body=[{"p": {"runs": [{"t": "x", "size": "14"}]}}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        assert result["body"][0]["p"]["runs"][0]["size"] == 14

    def test_size_float_converted(self):
        doc = _make_doc(body=[{"p": {"runs": [{"t": "x", "size": "14.5"}]}}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        assert result["body"][0]["p"]["runs"][0]["size"] == 14.5

    def test_depth_converted_to_int(self):
        doc = _make_doc(body=[{"ul": {"t": "item", "depth": "2"}}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        assert result["body"][0]["ul"]["depth"] == 2

    def test_para_style_numerics_converted(self):
        doc = _make_doc(body=[{"p": {"t": "x", "style": {
            "indent_start": "36",
            "line_spacing": "115",
            "space_above": "6.0",
        }}}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        style = result["body"][0]["p"]["style"]
        assert style["indent_start"] == 36
        assert style["line_spacing"] == 115
        assert style["space_above"] == 6.0

    def test_compact_text_not_converted(self):
        """Compact paragraph text stays as-is (no type conversion)."""
        doc = _make_doc(body=[{"p": "No"}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        assert result["body"][0]["p"] == "No"

    def test_t_text_not_converted(self):
        """Full-form 't' text stays as-is."""
        doc = _make_doc(body=[{"p": {"t": "3.10"}}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        assert result["body"][0]["p"]["t"] == "3.10"

    def test_table_cell_text_not_converted(self):
        doc = _make_doc(body=[{"table": {"rows": [["No", "3.10", "007"]]}}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        row = result["body"][0]["table"]["rows"][0]
        assert row == ["No", "3.10", "007"]

    def test_invalid_bool_reports_error(self):
        doc = _make_doc(body=[{"p": {"runs": [{"t": "x", "b": "maybe"}]}}])
        _, errors = convert_typed_attrs(doc)
        assert len(errors) == 1
        assert "expected boolean" in errors[0].message

    def test_invalid_number_reports_error(self):
        doc = _make_doc(body=[{"p": {"runs": [{"t": "x", "size": "big"}]}}])
        _, errors = convert_typed_attrs(doc)
        assert len(errors) == 1
        assert "expected number" in errors[0].message

    def test_list_container_items_converted(self):
        """Canonical list container: items array gets depth/style conversion."""
        doc = _make_doc(body=[{"ul": {
            "items": [
                {"t": "item 1", "depth": "0"},
                {"t": "nested", "depth": "1"},
            ],
            "_listId": "kix.abc",
        }}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        items = result["body"][0]["ul"]["items"]
        assert items[0]["depth"] == 0
        assert items[1]["depth"] == 1
        assert items[0]["t"] == "item 1"  # Text untouched

    def test_table_meta_converted(self):
        doc = _make_doc(body=[{"table": {"rows": [], "_nrows": "3", "_cols": "2"}}])
        result, errors = convert_typed_attrs(doc)
        assert errors == []
        assert result["body"][0]["table"]["_nrows"] == 3
        assert result["body"][0]["table"]["_cols"] == 2


# ============================================================================
# Lossless YAML round-trip (adversarial)
# ============================================================================


class TestLosslessRoundTrip:
    """Full YAML round-trip: unquoted values survive verbatim as strings."""

    def test_adversarial_text_preserved(self):
        yaml_str = """\
kind: doc-tree/v1
body:
  - p: "No"
  - p: "Yes"
  - p: "On"
  - p: "Off"
  - h1: "3.10"
  - ul: "007"
  - p: "null"
"""
        doc = validated_parse(yaml_str)
        assert doc["body"][0]["p"] == "No"
        assert doc["body"][1]["p"] == "Yes"
        assert doc["body"][2]["p"] == "On"
        assert doc["body"][3]["p"] == "Off"
        assert doc["body"][4]["h1"] == "3.10"
        assert doc["body"][5]["ul"] == "007"
        assert doc["body"][6]["p"] == "null"

    def test_unquoted_adversarial_text_preserved(self):
        """Even unquoted, BaseLoader keeps original strings."""
        yaml_str = """\
kind: doc-tree/v1
body:
  - p: No
  - p: Yes
  - p: On
  - h1: 3.10
  - ul: 007
"""
        doc = validated_parse(yaml_str)
        assert doc["body"][0]["p"] == "No"
        assert doc["body"][1]["p"] == "Yes"
        assert doc["body"][2]["p"] == "On"
        assert doc["body"][3]["h1"] == "3.10"
        assert doc["body"][4]["ul"] == "007"


# ============================================================================
# Schema validation tests
# ============================================================================


class TestValidateHeader:
    """Test validation of top-level document structure."""

    def test_valid_minimal(self):
        doc = _make_doc()
        assert validate(doc) == []

    def test_valid_with_all_header_fields(self):
        doc = _make_doc(source="doc-123", tab="t.0")
        assert validate(doc) == []

    def test_missing_kind(self):
        doc = {"body": []}
        errors = validate(doc)
        assert any("missing required header key 'kind'" in e.message for e in errors)

    def test_wrong_kind(self):
        doc = {"kind": "doc-tree/v2", "body": []}
        errors = validate(doc)
        assert any("kind must be" in e.message for e in errors)

    def test_missing_body(self):
        doc = {"kind": "doc-tree/v1"}
        errors = validate(doc)
        assert any("missing required header key 'body'" in e.message for e in errors)

    def test_body_not_list(self):
        doc = _make_doc(body="oops")
        errors = validate(doc)
        assert any("body must be a list" in e.message for e in errors)

    def test_unknown_header_key(self):
        doc = _make_doc()
        doc["flavor"] = "chocolate"
        errors = validate(doc)
        assert any("unknown header key 'flavor'" in e.message for e in errors)
        assert errors[0].path == "$.flavor"

    def test_source_must_be_string(self):
        doc = _make_doc(source=42)
        errors = validate(doc)
        assert any("source must be a string" in e.message for e in errors)

    def test_not_a_dict(self):
        errors = validate([1, 2, 3])
        assert errors[0].message == "document must be a mapping"


class TestValidateBlocks:
    """Test validation of body blocks."""

    def test_valid_compact_paragraph(self):
        doc = _make_doc(body=[{"p": "hello"}])
        assert validate(doc) == []

    def test_valid_compact_heading(self):
        doc = _make_doc(body=[{"h1": "Title"}])
        assert validate(doc) == []

    def test_valid_compact_list_items(self):
        doc = _make_doc(body=[{"ul": "item"}, {"ol": "step 1"}])
        assert validate(doc) == []

    def test_valid_full_paragraph(self):
        doc = _make_doc(body=[{"p": {"t": "hello", "style": {"align": "center"}}}])
        assert validate(doc) == []

    def test_valid_full_heading_with_runs(self):
        doc = _make_doc(body=[{
            "h2": {
                "runs": [{"t": "bold ", "b": True}, "text"],
                "style": {"align": "center"},
            }
        }])
        assert validate(doc) == []

    def test_valid_list_item_with_depth(self):
        doc = _make_doc(body=[{"ul": {"t": "nested", "depth": 2}}])
        assert validate(doc) == []

    def test_valid_table(self):
        doc = _make_doc(body=[{"table": {"rows": [["a", "b"], ["c", "d"]]}}])
        assert validate(doc) == []

    def test_valid_toc_block(self):
        doc = _make_doc(body=[{"toc": {"content": []}}])
        assert validate(doc) == []

    def test_block_no_type_key(self):
        doc = _make_doc(body=[{"flavor": "chocolate"}])
        errors = validate(doc)
        assert len(errors) >= 1
        assert "no recognized type key" in errors[0].message
        assert errors[0].path == "$.body[0]"

    def test_block_multiple_type_keys(self):
        doc = _make_doc(body=[{"p": "a", "h1": "b"}])
        errors = validate(doc)
        assert any("multiple type keys" in e.message for e in errors)

    def test_block_not_a_dict(self):
        doc = _make_doc(body=["just a string"])
        errors = validate(doc)
        assert any("block must be a mapping" in e.message for e in errors)

    def test_invented_attribute_rejected(self):
        """Core acceptance: invented attribute is rejected with node-path error."""
        doc = _make_doc(body=[{"p": {"t": "hello", "sparkle": True}}])
        errors = validate(doc)
        assert len(errors) >= 1
        err = errors[0]
        assert "sparkle" in err.message
        assert err.path == "$.body[0].p.sparkle"

    def test_unknown_block_level_key(self):
        doc = _make_doc(body=[{"p": "text", "extra": "bad"}])
        errors = validate(doc)
        assert any("unknown key 'extra'" in e.message for e in errors)

    def test_missing_t_and_runs(self):
        doc = _make_doc(body=[{"p": {"style": {"align": "center"}}}])
        errors = validate(doc)
        assert any("must have either 't'" in e.message for e in errors)

    def test_both_t_and_runs(self):
        doc = _make_doc(body=[{"p": {"t": "a", "runs": ["b"]}}])
        errors = validate(doc)
        assert any("cannot have both" in e.message for e in errors)

    def test_depth_only_on_list_items(self):
        # depth is valid on ul/ol (flat form)
        doc = _make_doc(body=[{"ul": {"t": "x", "depth": 1}}])
        assert validate(doc) == []
        # depth is NOT valid on headings
        doc = _make_doc(body=[{"h1": {"t": "x", "depth": 1}}])
        errors = validate(doc)
        assert any("depth" in e.message for e in errors)

    def test_negative_depth_rejected(self):
        doc = _make_doc(body=[{"ul": {"t": "x", "depth": -1}}])
        errors = validate(doc)
        assert any("non-negative integer" in e.message for e in errors)

    def test_verbatim_block_accepted(self):
        """_verbatim blocks are opaque passthrough -- accepted without validation."""
        doc = _make_doc(body=[{"_verbatim": {"anything": "goes"}}])
        assert validate(doc) == []


class TestValidateTextStyle:
    """Test validation of text style keys in runs."""

    def test_valid_styled_run(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "hello", "b": True, "i": True, "color": "#cc0000", "size": 14}
        ]}}])
        assert validate(doc) == []

    def test_unknown_text_style_key(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "hello", "glow": True}
        ]}}])
        errors = validate(doc)
        assert any("unknown text style key 'glow'" in e.message for e in errors)
        assert "runs[0].glow" in errors[0].path

    def test_styled_run_missing_t(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"b": True, "i": True}
        ]}}])
        errors = validate(doc)
        assert any("must have 't'" in e.message for e in errors)

    def test_color_must_be_hex(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "color": "red"}
        ]}}])
        errors = validate(doc)
        assert any("#rrggbb" in e.message for e in errors)

    def test_color_valid_hex(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "color": "#FF0000"}
        ]}}])
        assert validate(doc) == []

    def test_size_must_be_positive(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "size": 0}
        ]}}])
        errors = validate(doc)
        assert any("positive" in e.message for e in errors)

    def test_size_string_zero_rejected(self):
        """BaseLoader: size as string "0" still fails positive check."""
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "size": "0"}
        ]}}])
        errors = validate(doc)
        assert any("positive" in e.message for e in errors)

    def test_size_string_valid(self):
        """BaseLoader: size as string "14" is accepted."""
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "size": "14"}
        ]}}])
        assert validate(doc) == []

    def test_size_string_non_numeric_rejected(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "size": "big"}
        ]}}])
        errors = validate(doc)
        assert any("number" in e.message for e in errors)

    def test_baseline_valid_values(self):
        for val in ("SUPERSCRIPT", "subscript"):
            doc = _make_doc(body=[{"p": {"runs": [
                {"t": "x", "baseline": val}
            ]}}])
            assert validate(doc) == []

    def test_baseline_invalid_value(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "baseline": "MIDDLE"}
        ]}}])
        errors = validate(doc)
        assert any("baseline must be one of" in e.message for e in errors)

    def test_url_must_be_string(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "url": 42}
        ]}}])
        errors = validate(doc)
        assert any("url must be a string" in e.message for e in errors)

    def test_bool_key_as_string_accepted(self):
        """BaseLoader produces "true"/"false" strings -- should be accepted."""
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "b": "true", "i": "false"}
        ]}}])
        assert validate(doc) == []

    def test_bool_key_invalid_string_rejected(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "b": "maybe"}
        ]}}])
        errors = validate(doc)
        assert any("must be boolean" in e.message for e in errors)

    def test_underscore_prefixed_keys_accepted(self):
        """_-prefixed keys are opaque passthrough from rewrite engine."""
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "_link_fg": {"some": "data"}, "_verbatim_element": {}}
        ]}}])
        assert validate(doc) == []


class TestValidateParaStyle:
    """Test validation of paragraph style."""

    def test_valid_full_style(self):
        doc = _make_doc(body=[{"p": {
            "t": "text",
            "style": {
                "align": "center",
                "indent_start": 36.0,
                "indent_end": 0.0,
                "indent_first": 18.0,
                "line_spacing": 115,
                "space_above": 6.0,
                "space_below": 6.0,
            }
        }}])
        assert validate(doc) == []

    def test_unknown_style_key(self):
        doc = _make_doc(body=[{"p": {"t": "text", "style": {"kerning": 2}}}])
        errors = validate(doc)
        assert any("unknown paragraph style key 'kerning'" in e.message for e in errors)

    def test_invalid_align_value(self):
        doc = _make_doc(body=[{"p": {"t": "text", "style": {"align": "middle"}}}])
        errors = validate(doc)
        assert any("align must be one of" in e.message for e in errors)

    def test_valid_align_values(self):
        for val in ("start", "center", "end", "justified", "left", "right", "justify"):
            doc = _make_doc(body=[{"p": {"t": "text", "style": {"align": val}}}])
            assert validate(doc) == [], f"align={val} should be valid"

    def test_numeric_style_as_string_accepted(self):
        """BaseLoader: numeric style values as strings accepted."""
        doc = _make_doc(body=[{"p": {"t": "text", "style": {"indent_start": "36"}}}])
        assert validate(doc) == []

    def test_numeric_style_string_non_numeric_rejected(self):
        doc = _make_doc(body=[{"p": {"t": "text", "style": {"indent_start": "big"}}}])
        errors = validate(doc)
        assert any("must be a number" in e.message for e in errors)

    def test_named_style_accepted(self):
        """'named' key for named paragraph styles (rewrite engine)."""
        doc = _make_doc(body=[{"p": {"t": "text", "style": {"named": "HEADING_1"}}}])
        assert validate(doc) == []

    def test_raw_prefixed_style_keys_accepted(self):
        """_raw_* keys are opaque passthrough from rewrite engine."""
        doc = _make_doc(body=[{"p": {"t": "text", "style": {
            "align": "center",
            "_raw_direction": "LEFT_TO_RIGHT",
            "_raw_headingId": "h.xyz",
        }}}])
        assert validate(doc) == []


class TestValidateTable:
    """Test validation of table blocks."""

    def test_valid_simple_table(self):
        doc = _make_doc(body=[{"table": {"rows": [["a", "b"], ["c", "d"]]}}])
        assert validate(doc) == []

    def test_table_missing_rows(self):
        doc = _make_doc(body=[{"table": {}}])
        errors = validate(doc)
        assert any("must have 'rows'" in e.message for e in errors)

    def test_table_unknown_non_prefixed_key(self):
        doc = _make_doc(body=[{"table": {"rows": [], "border": True}}])
        errors = validate(doc)
        assert any("unknown table key 'border'" in e.message for e in errors)

    def test_table_underscore_keys_accepted(self):
        """Canonical rewrite-tree metadata keys accepted."""
        doc = _make_doc(body=[{"table": {
            "rows": [["a"]],
            "_nrows": 1,
            "_cols": 1,
            "_tableStyle": {"some": "data"},
            "_rowStyles": [None],
        }}])
        assert validate(doc) == []

    def test_table_cell_with_style(self):
        doc = _make_doc(body=[{"table": {"rows": [
            [{"t": "centered", "style": {"align": "center"}}]
        ]}}])
        assert validate(doc) == []

    def test_table_cell_with_runs(self):
        doc = _make_doc(body=[{"table": {"rows": [
            [{"runs": [{"t": "bold", "b": True}]}]
        ]}}])
        assert validate(doc) == []

    def test_table_cell_runs_list(self):
        doc = _make_doc(body=[{"table": {"rows": [
            [[{"t": "bold", "b": True}, "plain"]]
        ]}}])
        assert validate(doc) == []

    def test_table_row_not_list(self):
        doc = _make_doc(body=[{"table": {"rows": ["not a row"]}}])
        errors = validate(doc)
        assert any("row must be a list" in e.message for e in errors)

    def test_table_cell_unknown_non_prefixed_key(self):
        doc = _make_doc(body=[{"table": {"rows": [
            [{"t": "x", "sparkle": True}]
        ]}}])
        errors = validate(doc)
        assert any("unknown table cell key 'sparkle'" in e.message for e in errors)

    def test_table_cell_underscore_keys_accepted(self):
        """Canonical rewrite-tree cell keys accepted."""
        doc = _make_doc(body=[{"table": {"rows": [
            [{"t": "x", "_raw_ps": {"some": "data"}, "_cellStyle": {"border": 1}}]
        ]}}])
        assert validate(doc) == []

    def test_table_cell_verbatim_accepted(self):
        """Complex cell with _verbatim content accepted."""
        doc = _make_doc(body=[{"table": {"rows": [
            [{"_verbatim": [{"paragraph": {}}], "_cellStyle": {}}]
        ]}}])
        assert validate(doc) == []


class TestValidateAppendix:
    """Test structural validation of appendix."""

    def test_valid_appendix(self):
        doc = _make_doc(appendix={"r1": {"some": "data"}, "r2": [1, 2, 3]})
        assert validate(doc) == []

    def test_invalid_appendix_key(self):
        doc = _make_doc(appendix={"mykey": {"some": "data"}})
        errors = validate(doc)
        assert any("must match r<N>" in e.message for e in errors)

    def test_appendix_not_dict(self):
        doc = _make_doc(appendix=[1, 2, 3])
        errors = validate(doc)
        assert any("appendix must be a mapping" in e.message for e in errors)


# ============================================================================
# Canonical rewrite-tree shape tests
# ============================================================================


class TestCanonicalRewriteTree:
    """Test validation of canonical rewrite-tree emission shape."""

    def test_list_container_with_items(self):
        doc = _make_doc(body=[{
            "ul": {
                "items": [
                    {"t": "First item", "depth": 0, "_bullet": {"listId": "kix.a", "nestingLevel": 0}},
                    {"t": "Second item", "depth": 0, "_bullet": {"listId": "kix.a", "nestingLevel": 0}},
                ],
                "_listId": "kix.a",
            }
        }])
        assert validate(doc) == []

    def test_ordered_list_container(self):
        doc = _make_doc(body=[{
            "ol": {
                "items": [
                    {"t": "Step 1"},
                    {"t": "Step 2"},
                ],
                "_listId": "kix.b",
            }
        }])
        assert validate(doc) == []

    def test_list_item_with_indent_keys(self):
        doc = _make_doc(body=[{
            "ul": {
                "items": [
                    {
                        "t": "item",
                        "depth": 1,
                        "_bullet": {"listId": "kix.a", "nestingLevel": 1},
                        "_indent_start": {"magnitude": 72, "unit": "PT"},
                        "_indent_first": {"magnitude": 54, "unit": "PT"},
                    },
                ],
                "_listId": "kix.a",
            }
        }])
        assert validate(doc) == []

    def test_list_container_unknown_key_rejected(self):
        doc = _make_doc(body=[{
            "ul": {
                "items": [{"t": "item"}],
                "_listId": "kix.a",
                "sparkle": True,
            }
        }])
        errors = validate(doc)
        assert any("unknown list container key 'sparkle'" in e.message for e in errors)

    def test_list_item_invented_attr_rejected(self):
        doc = _make_doc(body=[{
            "ul": {
                "items": [{"t": "item", "sparkle": True}],
                "_listId": "kix.a",
            }
        }])
        errors = validate(doc)
        assert any("sparkle" in e.message for e in errors)

    def test_verbatim_block_passthrough(self):
        doc = _make_doc(body=[{"_verbatim": {"sectionBreak": {}, "startIndex": 0}}])
        assert validate(doc) == []

    def test_heading_with_style_named(self):
        """Heading with named paragraph style from rewrite engine."""
        doc = _make_doc(body=[{"h1": {
            "t": "Title",
            "style": {"named": "HEADING_1", "_raw_headingId": "h.abc"},
        }}])
        assert validate(doc) == []


# ============================================================================
# Appendix immutability (edit contract) tests
# ============================================================================


class TestAppendixImmutability:
    """Test that appendix edits are rejected (edit contract)."""

    def test_no_change_passes(self):
        original = {"r1": {"a": 1}, "r2": [1, 2]}
        errors = validate_appendix_immutable(original, original)
        assert errors == []

    def test_removed_entry_rejected(self):
        original = {"r1": {"a": 1}, "r2": [1, 2]}
        edited = {"r1": {"a": 1}}
        errors = validate_appendix_immutable(original, edited)
        assert len(errors) == 1
        assert "removed" in errors[0].message
        assert "r2" in errors[0].path

    def test_added_entry_rejected(self):
        original = {"r1": {"a": 1}}
        edited = {"r1": {"a": 1}, "r2": [1, 2]}
        errors = validate_appendix_immutable(original, edited)
        assert len(errors) == 1
        assert "added" in errors[0].message

    def test_modified_entry_rejected(self):
        original = {"r1": {"a": 1}}
        edited = {"r1": {"a": 999}}
        errors = validate_appendix_immutable(original, edited)
        assert len(errors) == 1
        assert "modified" in errors[0].message

    def test_multiple_violations(self):
        original = {"r1": {"a": 1}, "r2": [1]}
        edited = {"r1": {"a": 2}, "r3": "new"}
        errors = validate_appendix_immutable(original, edited)
        # r1 modified, r2 removed, r3 added = 3 errors
        assert len(errors) == 3


class TestRefsImmutability:
    """Test that ref:rNN values in the body are immutable."""

    def test_unchanged_refs_pass(self):
        body = [{"p": {"t": "text", "raw": "ref:r1"}}]
        errors = validate_refs_immutable(body, body)
        assert errors == []

    def test_changed_ref_rejected(self):
        original_body = [{"p": {"t": "text", "raw": "ref:r1"}}]
        edited_body = [{"p": {"t": "text", "raw": "ref:r99"}}]
        errors = validate_refs_immutable(original_body, edited_body)
        assert len(errors) == 1
        assert "ref value changed" in errors[0].message


# ============================================================================
# Integrated validated_parse tests
# ============================================================================


class TestValidatedParse:
    """Test the integrated parse-validate-convert pipeline."""

    def test_valid_document(self):
        yaml_str = """\
kind: doc-tree/v1
body:
  - h1: Introduction
  - p: Some text here.
  - ul: First item
  - table:
      rows:
        - - Name
          - Age
        - - Alice
          - "30"
"""
        doc = validated_parse(yaml_str)
        assert doc["kind"] == "doc-tree/v1"
        assert len(doc["body"]) == 4

    def test_lossless_text_preservation(self):
        """Text positions are preserved verbatim through validated_parse."""
        yaml_str = """\
kind: doc-tree/v1
body:
  - p: No
  - h1: 3.10
  - ul: 007
"""
        doc = validated_parse(yaml_str)
        assert doc["body"][0]["p"] == "No"
        assert doc["body"][1]["h1"] == "3.10"
        assert doc["body"][2]["ul"] == "007"

    def test_typed_attrs_converted(self):
        """Typed attributes are converted during validated_parse."""
        yaml_str = """\
kind: doc-tree/v1
body:
  - p:
      runs:
        - t: text
          b: true
          size: 14
      style:
        indent_start: 36
"""
        doc = validated_parse(yaml_str)
        run = doc["body"][0]["p"]["runs"][0]
        assert run["b"] is True
        assert run["size"] == 14
        assert run["t"] == "text"
        assert doc["body"][0]["p"]["style"]["indent_start"] == 36

    def test_invalid_document_raises(self):
        yaml_str = """\
kind: doc-tree/v1
body:
  - p:
      sparkle: true
"""
        with pytest.raises(SchemaValidationError) as exc_info:
            validated_parse(yaml_str)
        assert len(exc_info.value.errors) >= 1
        err_msg = str(exc_info.value)
        assert "sparkle" in err_msg

    def test_empty_document_raises(self):
        with pytest.raises(SchemaValidationError):
            validated_parse("")

    def test_non_mapping_raises(self):
        with pytest.raises(SchemaValidationError):
            validated_parse("- just\n- a\n- list\n")

    def test_wrong_kind_raises(self):
        yaml_str = """\
kind: doc-tree/v99
body: []
"""
        with pytest.raises(SchemaValidationError):
            validated_parse(yaml_str)


# ============================================================================
# compress_doc integration test
# ============================================================================


class TestCompressDocIntegration:
    """Integration: compress_doc output passes validated_parse."""

    def _make_rich_doc_json(self):
        """Build a rich fixture doc JSON with paragraphs, headings, lists, table."""
        return {
            "body": {
                "content": [
                    {"sectionBreak": {}, "startIndex": 0, "endIndex": 1},
                    {
                        "paragraph": {
                            "paragraphStyle": {
                                "namedStyleType": "HEADING_1",
                                "headingId": "h.abc",
                            },
                            "elements": [
                                {"textRun": {"content": "Introduction\n", "textStyle": {}}}
                            ],
                        },
                        "startIndex": 1,
                        "endIndex": 14,
                    },
                    {
                        "paragraph": {
                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                            "elements": [
                                {"textRun": {
                                    "content": "Bold ",
                                    "textStyle": {"bold": True},
                                }},
                                {"textRun": {
                                    "content": "and italic",
                                    "textStyle": {"italic": True},
                                }},
                                {"textRun": {"content": " text\n", "textStyle": {}}},
                            ],
                        },
                        "startIndex": 14,
                        "endIndex": 30,
                    },
                    {
                        "paragraph": {
                            "paragraphStyle": {
                                "namedStyleType": "NORMAL_TEXT",
                                "alignment": "CENTER",
                            },
                            "elements": [
                                {"textRun": {
                                    "content": "Colored ",
                                    "textStyle": {
                                        "foregroundColor": {
                                            "color": {"rgbColor": {"red": 0.8, "green": 0, "blue": 0}}
                                        }
                                    },
                                }},
                                {"textRun": {"content": "text\n", "textStyle": {}}},
                            ],
                        },
                        "startIndex": 30,
                        "endIndex": 43,
                    },
                    {
                        "paragraph": {
                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                            "bullet": {"listId": "kix.list1", "nestingLevel": 0},
                            "elements": [
                                {"textRun": {"content": "First item\n", "textStyle": {}}}
                            ],
                        },
                        "startIndex": 43,
                        "endIndex": 54,
                    },
                    {
                        "paragraph": {
                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                            "bullet": {"listId": "kix.list1", "nestingLevel": 0},
                            "elements": [
                                {"textRun": {"content": "Second item\n", "textStyle": {}}}
                            ],
                        },
                        "startIndex": 54,
                        "endIndex": 66,
                    },
                    {
                        "table": {
                            "rows": 2,
                            "columns": 2,
                            "tableRows": [
                                {"tableCells": [
                                    {"content": [{"paragraph": {
                                        "paragraphStyle": {},
                                        "elements": [{"textRun": {"content": "A\n", "textStyle": {}}}],
                                    }}]},
                                    {"content": [{"paragraph": {
                                        "paragraphStyle": {},
                                        "elements": [{"textRun": {"content": "B\n", "textStyle": {}}}],
                                    }}]},
                                ]},
                                {"tableCells": [
                                    {"content": [{"paragraph": {
                                        "paragraphStyle": {},
                                        "elements": [{"textRun": {"content": "C\n", "textStyle": {}}}],
                                    }}]},
                                    {"content": [{"paragraph": {
                                        "paragraphStyle": {},
                                        "elements": [{"textRun": {"content": "D\n", "textStyle": {}}}],
                                    }}]},
                                ]},
                            ],
                        },
                        "startIndex": 66,
                        "endIndex": 80,
                    },
                ],
            },
            "lists": {
                "kix.list1": {
                    "listProperties": {
                        "nestingLevels": [
                            {"glyphType": "GLYPH_TYPE_UNSPECIFIED"},
                        ]
                    }
                }
            },
        }

    def test_compress_doc_output_validates(self):
        """compress_doc -> YAML emit -> validated_parse passes with zero errors."""
        doc_json = self._make_rich_doc_json()
        body_content = doc_json["body"]["content"]
        lists = doc_json.get("lists")

        result = compress_doc(body_content, lists=lists)

        # Extract appendix
        appendix_result = extract_appendix(result.body)

        # Build YAML doc
        doc = {
            "kind": "doc-tree/v1",
            "body": appendix_result.body,
        }
        if appendix_result.appendix:
            doc["appendix"] = appendix_result.appendix

        # Validate the structure
        errors = validate(doc)
        assert errors == [], f"Validation errors: {errors}"

    def test_compress_doc_yaml_round_trip(self):
        """compress_doc -> YAML serialize -> validated_parse round-trips."""
        doc_json = self._make_rich_doc_json()
        body_content = doc_json["body"]["content"]
        lists = doc_json.get("lists")

        result = compress_doc(body_content, lists=lists)
        appendix_result = extract_appendix(result.body)

        doc = {
            "kind": "doc-tree/v1",
            "body": appendix_result.body,
        }
        if appendix_result.appendix:
            doc["appendix"] = appendix_result.appendix

        # Serialize to YAML string
        yaml_str = yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)

        # Parse back with validation
        parsed = validated_parse(yaml_str)
        assert parsed["kind"] == "doc-tree/v1"
        assert len(parsed["body"]) > 0


# ============================================================================
# Error path quality tests
# ============================================================================


class TestErrorPaths:
    """Verify that error paths are precise and useful for LLM self-correction."""

    def test_nested_run_error_has_full_path(self):
        doc = _make_doc(body=[
            {"p": "ok"},
            {"h2": {"runs": [
                {"t": "fine"},
                {"t": "bad", "glow": True},
            ]}},
        ])
        errors = validate(doc)
        assert len(errors) == 1
        assert errors[0].path == "$.body[1].h2.runs[1].glow"

    def test_table_cell_error_has_full_path(self):
        doc = _make_doc(body=[{"table": {"rows": [
            ["ok", {"t": "bad", "sparkle": True}],
        ]}}])
        errors = validate(doc)
        assert len(errors) == 1
        assert errors[0].path == "$.body[0].table.rows[0][1].sparkle"

    def test_multiple_errors_collected(self):
        """Validator collects ALL errors, not just the first one."""
        doc = _make_doc(body=[
            {"p": {"t": "a", "glow": True}},
            {"p": {"t": "b", "sparkle": True}},
        ])
        errors = validate(doc)
        assert len(errors) == 2
        paths = {e.path for e in errors}
        assert "$.body[0].p.glow" in paths
        assert "$.body[1].p.sparkle" in paths

    def test_list_container_item_error_has_full_path(self):
        doc = _make_doc(body=[{
            "ul": {
                "items": [
                    {"t": "ok"},
                    {"t": "bad", "sparkle": True},
                ],
                "_listId": "kix.a",
            }
        }])
        errors = validate(doc)
        assert len(errors) == 1
        assert errors[0].path == "$.body[0].ul.items[1].sparkle"

    def test_schema_error_str(self):
        err = SchemaError("$.body[0].p", "something is wrong")
        assert str(err) == "$.body[0].p: something is wrong"


# ============================================================================
# serialize_tree_yaml tests (gax-cvi.8)
# ============================================================================


class TestSerializeTreeYaml:
    """Test the serialize_tree_yaml convenience function."""

    def test_basic_serialization(self):
        """Serialize a simple doc body to tree YAML."""
        from gax.gdoc.tree import serialize_tree_yaml

        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "elements": [{"textRun": {"content": "Title\n", "textStyle": {}}}],
                }
            },
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"textRun": {"content": "Hello world\n", "textStyle": {}}}],
                }
            },
        ]

        yaml_str = serialize_tree_yaml(body, source="https://example.com", tab="Tab 1")
        doc = validated_parse(yaml_str)

        assert doc["kind"] == "doc-tree/v1"
        assert doc["source"] == "https://example.com"
        assert doc["tab"] == "Tab 1"
        assert len(doc["body"]) == 2
        assert "h1" in doc["body"][0]
        assert "p" in doc["body"][1]

    def test_serialization_with_lists(self):
        """Serialize body with list items and lists metadata."""
        from gax.gdoc.tree import serialize_tree_yaml

        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "bullet": {"listId": "kix.a", "nestingLevel": 0},
                    "elements": [{"textRun": {"content": "Item one\n", "textStyle": {}}}],
                }
            },
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "bullet": {"listId": "kix.a", "nestingLevel": 0},
                    "elements": [{"textRun": {"content": "Item two\n", "textStyle": {}}}],
                }
            },
        ]
        lists = {
            "kix.a": {
                "listProperties": {
                    "nestingLevels": [{"glyphType": "GLYPH_TYPE_UNSPECIFIED"}],
                }
            }
        }

        yaml_str = serialize_tree_yaml(body, lists=lists)
        doc = validated_parse(yaml_str)
        assert doc["kind"] == "doc-tree/v1"
        # Should be grouped into a ul container
        assert any("ul" in b for b in doc["body"] if isinstance(b, dict))

    def test_round_trip_through_validated_parse(self):
        """serialize_tree_yaml output passes validated_parse without errors."""
        from gax.gdoc.tree import serialize_tree_yaml

        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"textRun": {"content": "Simple\n", "textStyle": {}}}],
                }
            },
        ]
        yaml_str = serialize_tree_yaml(body)
        # Should not raise
        doc = validated_parse(yaml_str)
        assert doc["body"][0]["p"] == "Simple"

    def test_revision_field_stamped(self):
        """serialize_tree_yaml stamps revision: in output (ADR 037 guard)."""
        from gax.gdoc.tree import serialize_tree_yaml

        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"textRun": {"content": "Text\n", "textStyle": {}}}],
                }
            },
        ]
        yaml_str = serialize_tree_yaml(
            body, source="https://example.com", revision="rev-abc123",
        )
        doc = validated_parse(yaml_str)
        assert doc.get("revision") == "rev-abc123"

    def test_revision_omitted_when_empty(self):
        """serialize_tree_yaml omits revision: when not provided."""
        from gax.gdoc.tree import serialize_tree_yaml

        body = [
            {
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                    "elements": [{"textRun": {"content": "Text\n", "textStyle": {}}}],
                }
            },
        ]
        yaml_str = serialize_tree_yaml(body)
        doc = validated_parse(yaml_str)
        assert "revision" not in doc


class TestIsTreeFile:
    """Test _is_tree_file and _parse_tree_file helpers."""

    def test_doc_gax_yaml_detected(self, tmp_path):
        from gax.gdoc.doc import _is_tree_file
        f = tmp_path / "report.doc.gax.yaml"
        f.write_text("kind: doc-tree/v1\nbody: []\n")
        assert _is_tree_file(f) is True

    def test_tab_gax_yaml_detected(self, tmp_path):
        from gax.gdoc.doc import _is_tree_file
        f = tmp_path / "details.tab.gax.yaml"
        f.write_text("kind: doc-tree/v1\nbody: []\n")
        assert _is_tree_file(f) is True

    def test_doc_gax_md_not_detected(self, tmp_path):
        from gax.gdoc.doc import _is_tree_file
        f = tmp_path / "report.doc.gax.md"
        f.write_text("---\ntype: gax/doc\n---\ncontent\n")
        assert _is_tree_file(f) is False

    def test_parse_tree_file(self, tmp_path):
        from gax.gdoc.doc import _parse_tree_file
        f = tmp_path / "test.doc.gax.yaml"
        f.write_text(
            "kind: doc-tree/v1\n"
            "source: https://docs.google.com/document/d/abc/edit\n"
            "tab: Overview\n"
            "body:\n"
            "- h1: Hello\n"
            "- p: World\n"
        )
        doc = _parse_tree_file(f)
        assert doc["kind"] == "doc-tree/v1"
        assert doc["source"] == "https://docs.google.com/document/d/abc/edit"
        assert doc["tab"] == "Overview"
        assert len(doc["body"]) == 2

    def test_parse_tree_file_with_revision(self, tmp_path):
        """_parse_tree_file preserves the revision: field (ADR 037)."""
        from gax.gdoc.doc import _parse_tree_file
        f = tmp_path / "test.doc.gax.yaml"
        f.write_text(
            "kind: doc-tree/v1\n"
            "source: https://docs.google.com/document/d/abc/edit\n"
            "tab: Overview\n"
            "revision: rev-xyz789\n"
            "body:\n"
            "- p: Hello\n"
        )
        doc = _parse_tree_file(f)
        assert doc.get("revision") == "rev-xyz789"


class TestPullTreeForce:
    """Unit tests for _pull_tree --force recovery path (gax-iuf)."""

    def _write_corrupt_yaml(self, path) -> None:
        """Write a tree YAML file with a known source/tab but invalid body."""
        path.write_text(
            "kind: doc-tree/v1\n"
            "source: https://docs.google.com/document/d/DOCID123/edit\n"
            "tab: Overview\n"
            "body:\n"
            "  - invalid_key_not_in_schema: oops\n"
            "    nested_garbage: [1, 2, 3]\n",
            encoding="utf-8",
        )

    def test_corrupt_file_raises_on_normal_pull(self, tmp_path):
        """A corrupt .doc.gax.yaml fails validated_parse without --force."""
        from gax.gdoc.doc import _parse_tree_file
        from gax.gdoc.tree import SchemaValidationError

        f = tmp_path / "report.doc.gax.yaml"
        self._write_corrupt_yaml(f)
        with pytest.raises(SchemaValidationError):
            _parse_tree_file(f)

    def test_pull_tree_force_reads_source_from_raw_yaml(self, tmp_path, monkeypatch):
        """_pull_tree(force=True) reads source/tab via yaml.safe_load and calls fetch."""
        import yaml
        from unittest.mock import MagicMock, patch
        from gax.gdoc import doc as doc_mod

        f = tmp_path / "report.doc.gax.yaml"
        self._write_corrupt_yaml(f)

        # Build a minimal fake API response so _pull_tree can complete.
        fake_tab_content = {
            "kind": "doc-tree/v1",
            "source": "https://docs.google.com/document/d/DOCID123/edit",
            "tab": "Overview",
            "body": [{"p": "Hello"}],
        }

        captured_calls = []

        def fake_fetch_doc(document_id):
            captured_calls.append(document_id)
            return {
                "tabs": [
                    {
                        "tabProperties": {"title": "Overview", "tabId": "t0"},
                        "documentTab": {},
                    }
                ]
            }

        def fake_tab_to_yaml(doc, tab, source_url):
            import yaml as _yaml
            return _yaml.dump(fake_tab_content)

        with (
            patch.object(doc_mod, "_fetch_doc", side_effect=fake_fetch_doc),
            patch.object(doc_mod, "_tab_content_to_tree_yaml", side_effect=fake_tab_to_yaml),
            patch.object(doc_mod, "_flatten_tabs", return_value=[
                ({"documentTab": {}}, MagicMock(title="Overview"))
            ]),
        ):
            resource = doc_mod.Tab(path=f)
            resource._pull_tree(force=True)

        assert captured_calls == ["DOCID123"], "Expected _fetch_doc called with extracted doc ID"
        written = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert written["tab"] == "Overview"

    def test_pull_force_kwarg_forwarded(self, tmp_path, monkeypatch):
        """Tab.pull(force=True) must call _pull_tree(force=True)."""
        from unittest.mock import patch
        from gax.gdoc import doc as doc_mod

        f = tmp_path / "report.doc.gax.yaml"
        self._write_corrupt_yaml(f)

        called_with = []

        def fake_pull_tree(self_inner, force=False):
            called_with.append(force)

        with patch.object(doc_mod.Tab, "_pull_tree", fake_pull_tree):
            resource = doc_mod.Tab(path=f)
            resource.pull(force=True)

        assert called_with == [True], "_pull_tree should have been called with force=True"


class TestPushTreeForce:
    """Unit tests for _push_tree force=True path (gax-fuh).

    AC: _push_tree(force=True) bypasses the revision guard so a push can
    succeed even when the stored revision is stale (corrupt-state recovery).
    """

    def _write_tree_yaml(self, path, *, revision: str = "rev1") -> None:
        """Write a valid tree YAML file with a known source/tab and body."""
        path.write_text(
            "kind: doc-tree/v1\n"
            "source: https://docs.google.com/document/d/DOCID123/edit\n"
            f"revision: {revision}\n"
            "tab: Overview\n"
            "body:\n"
            "  - p: Hello world\n",
            encoding="utf-8",
        )

    def _write_corrupt_yaml(self, path) -> None:
        """Write a tree YAML file with invalid body (mimics corrupt round-trip)."""
        path.write_text(
            "kind: doc-tree/v1\n"
            "source: https://docs.google.com/document/d/DOCID123/edit\n"
            "revision: stale-rev\n"
            "tab: Overview\n"
            "body:\n"
            "  - invalid_key_not_in_schema: oops\n",
            encoding="utf-8",
        )

    def test_push_force_kwarg_forwarded(self, tmp_path):
        """Tab.push(force=True) on a tree file must call _push_tree(force=True)."""
        from unittest.mock import patch
        from gax.gdoc import doc as doc_mod

        f = tmp_path / "report.doc.gax.yaml"
        self._write_tree_yaml(f)

        called_with = []

        def fake_push_tree(self_inner, *, force=False):
            called_with.append(force)

        with patch.object(doc_mod.Tab, "_push_tree", fake_push_tree):
            resource = doc_mod.Tab(path=f)
            resource.push(force=True)

        assert called_with == [True], "_push_tree should have been called with force=True"

    def test_push_no_force_uses_stored_revision(self, tmp_path):
        """Tab.push() without force=True passes the stored revision to compute_tree_plan."""
        from unittest.mock import patch
        from gax.gdoc import doc as doc_mod
        from gax.gdoc import diff_push as dp_mod
        from gax.gdoc.diff_push import ThreeWayPlan

        f = tmp_path / "report.doc.gax.yaml"
        self._write_tree_yaml(f, revision="rev-stored")

        captured = []

        def fake_compute_tree_plan(**kw):
            captured.append(kw.get("stored_revision"))
            return ThreeWayPlan(ops=[], mutations=[], summary_lines=[])

        fake_doc = {
            "revisionId": "rev-stored",
            "tabs": [{
                "tabProperties": {"title": "Overview", "tabId": "t0"},
                "documentTab": {"body": {"content": []}},
            }],
        }

        with (
            patch.object(doc_mod, "_fetch_doc", return_value=fake_doc),
            patch.object(dp_mod, "compute_tree_plan", side_effect=fake_compute_tree_plan),
        ):
            resource = doc_mod.Tab(path=f)
            resource.push()  # no force

        assert captured == ["rev-stored"], "stored revision must be passed to compute_tree_plan"

    def test_push_force_bypasses_revision_guard(self, tmp_path):
        """_push_tree(force=True) passes stored_revision='' to bypass the guard."""
        from unittest.mock import patch
        from gax.gdoc import doc as doc_mod
        from gax.gdoc import diff_push as dp_mod
        from gax.gdoc.diff_push import ThreeWayPlan

        f = tmp_path / "report.doc.gax.yaml"
        # Stale revision that would normally trip the guard
        self._write_tree_yaml(f, revision="old-rev")

        captured = []

        def fake_compute_tree_plan(**kw):
            captured.append(kw.get("stored_revision"))
            return ThreeWayPlan(ops=[], mutations=[], summary_lines=[])

        fake_doc = {
            "revisionId": "new-rev",   # remote changed — would block without force
            "tabs": [{
                "tabProperties": {"title": "Overview", "tabId": "t0"},
                "documentTab": {"body": {"content": []}},
            }],
        }

        with (
            patch.object(doc_mod, "_fetch_doc", return_value=fake_doc),
            patch.object(dp_mod, "compute_tree_plan", side_effect=fake_compute_tree_plan),
        ):
            resource = doc_mod.Tab(path=f)
            resource._push_tree(force=True)

        # force=True must have passed "" so revision_changed evaluates to False
        assert captured == [""], "force=True must clear stored_revision for the guard"

    def test_push_force_reads_corrupt_yaml(self, tmp_path):
        """_push_tree(force=True) reads routing keys from a corrupt YAML file."""
        from unittest.mock import patch
        from gax.gdoc import doc as doc_mod
        from gax.gdoc import diff_push as dp_mod
        from gax.gdoc.diff_push import ThreeWayPlan

        f = tmp_path / "report.doc.gax.yaml"
        self._write_corrupt_yaml(f)

        captured_doc_id = []

        def fake_fetch_doc(document_id):
            captured_doc_id.append(document_id)
            return {
                "revisionId": "new-rev",
                "tabs": [{
                    "tabProperties": {"title": "Overview", "tabId": "t0"},
                    "documentTab": {"body": {"content": []}},
                }],
            }

        def fake_compute_tree_plan(**kw):
            return ThreeWayPlan(ops=[], mutations=[], summary_lines=[])

        with (
            patch.object(doc_mod, "_fetch_doc", side_effect=fake_fetch_doc),
            patch.object(dp_mod, "compute_tree_plan", side_effect=fake_compute_tree_plan),
        ):
            resource = doc_mod.Tab(path=f)
            # Should NOT raise SchemaValidationError — routing uses raw yaml.safe_load
            resource._push_tree(force=True)

        assert captured_doc_id == ["DOCID123"], "Expected fetch with correct doc ID"


class TestDoForceReplacePushTree:
    """Unit tests for _do_force_replace_push with .doc.gax.yaml tree files (gax-fuh).

    AC: _do_force_replace_push must detect tree files and call t.push(force=True)
    instead of parse_multipart / from_markdown, which would reject YAML with
    'No valid sections found'.
    """

    def _write_tree_yaml(self, path) -> None:
        path.write_text(
            "kind: doc-tree/v1\n"
            "source: https://docs.google.com/document/d/DOCID123/edit\n"
            "revision: old-rev\n"
            "tab: Overview\n"
            "body:\n"
            "  - p: Hello world\n",
            encoding="utf-8",
        )

    def test_tree_file_calls_push_with_force(self, tmp_path):
        """_do_force_replace_push on a .doc.gax.yaml file calls t.push(force=True)."""
        from unittest.mock import MagicMock
        from gax.gdoc.cli import _do_force_replace_push

        f = tmp_path / "report.doc.gax.yaml"
        self._write_tree_yaml(f)

        # Build a Tab mock that records push() calls and returns a diff
        tab_mock = MagicMock()
        tab_mock.path = f
        tab_mock.diff.return_value = "--- remote\n+++ local\n some change"

        _do_force_replace_push(tab_mock, f, None, yes=True)

        tab_mock.push.assert_called_once_with(force=True)

    def test_tree_file_no_diff_skips_push(self, tmp_path):
        """_do_force_replace_push on a tree file with no diff does not push."""
        from unittest.mock import MagicMock
        from gax.gdoc.cli import _do_force_replace_push

        f = tmp_path / "report.doc.gax.yaml"
        self._write_tree_yaml(f)

        tab_mock = MagicMock()
        tab_mock.path = f
        tab_mock.diff.return_value = None  # no differences

        _do_force_replace_push(tab_mock, f, None, yes=True)

        tab_mock.push.assert_not_called()

    def test_markdown_file_uses_original_path(self, tmp_path):
        """_do_force_replace_push on a .doc.gax.md file uses old code path."""
        from unittest.mock import MagicMock
        from gax.gdoc.cli import _do_force_replace_push

        f = tmp_path / "report.doc.gax.md"
        f.write_text(
            "---\ntype: gax/doc\ntab: Overview\nsource: "
            "https://docs.google.com/document/d/X/edit\n---\n\nHello\n",
            encoding="utf-8",
        )

        tab_mock = MagicMock()
        tab_mock.path = f
        tab_mock.diff.return_value = "--- remote\n+++ local\n change"

        _do_force_replace_push(tab_mock, f, None, yes=True)

        # For markdown path: push is called without force= kwarg
        call_kwargs = tab_mock.push.call_args[1] if tab_mock.push.call_args else {}
        assert "force" not in call_kwargs, (
            "Markdown path must NOT pass force= to push"
        )
