"""Tests for the doc-tree/v1 formal schema validator.

Covers:
- Lossless YAML parsing (No/Yes/On/3.10/007 round-trip verbatim as strings)
- Schema validation (unknown attributes rejected with node-path errors)
- Canonical rewrite-tree shape (ul/ol containers, _-prefixed keys)
- Appendix immutability (edit contract)
- Integration with parse_tree round-trip
- Integration: compress_doc output passes validated_parse
"""

from __future__ import annotations

import pytest
import yaml

from .schema import (
    SchemaError,
    SchemaValidationError,
    convert_typed_attrs,
    validate,
    validate_appendix_immutable,
    validate_refs_immutable,
    validated_parse,
    _yaml_baseload,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_doc(**overrides) -> dict:
    """Build a minimal valid doc-tree/v1 document."""
    doc = {"kind": "doc-tree/v1", "body": []}
    doc.update(overrides)
    return doc


# =============================================================================
# BaseLoader lossless parsing tests
# =============================================================================


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


# =============================================================================
# Typed attribute conversion tests
# =============================================================================


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


# =============================================================================
# Lossless YAML round-trip (adversarial)
# =============================================================================


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


# =============================================================================
# Schema validation tests
# =============================================================================


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
        """_verbatim blocks are opaque passthrough — accepted without validation."""
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
        """BaseLoader produces "true"/"false" strings — should be accepted."""
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


# =============================================================================
# Canonical rewrite-tree shape tests
# =============================================================================


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


# =============================================================================
# Appendix immutability (edit contract) tests
# =============================================================================


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


# =============================================================================
# Integrated validated_parse tests
# =============================================================================


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


# =============================================================================
# Round-trip integration: serialize → validate → parse
# =============================================================================


class TestSerializerRoundTrip:
    """Ensure serialized output from serialize_tree passes validation."""

    def test_serialize_output_is_valid(self):
        """The serializer must produce schema-valid YAML."""
        from .enriched_ir import Heading, Paragraph, ListItem, Table, Span, TextStyle

        blocks = [
            Heading(level=1, spans=[Span(text="Title", style=TextStyle())]),
            Paragraph(spans=[
                Span(text="Bold ", style=TextStyle(bold=True)),
                Span(text="normal", style=TextStyle()),
            ]),
            ListItem(spans=[Span(text="Item 1")], ordered=False),
            ListItem(spans=[Span(text="Step 1")], ordered=True),
            Table(
                rows=[
                    [[Span(text="A")], [Span(text="B")]],
                    [[Span(text="C")], [Span(text="D")]],
                ],
            ),
        ]
        from .yaml_serializer import serialize_tree
        yaml_str = serialize_tree(blocks, source="test-doc")
        doc = validated_parse(yaml_str)
        assert doc["kind"] == "doc-tree/v1"
        assert len(doc["body"]) == 5

    def test_styled_serialize_output_is_valid(self):
        """Serialized output with styles passes validation."""
        from .enriched_ir import Heading, Paragraph, Span, TextStyle, ParagraphStyle
        from .yaml_serializer import serialize_tree

        blocks = [
            Heading(
                level=2,
                spans=[Span(text="Styled", style=TextStyle(bold=True, foreground_color="#cc0000"))],
                para_style=ParagraphStyle(alignment="CENTER"),
            ),
            Paragraph(
                spans=[Span(text="text", style=TextStyle(
                    font_family="Arial",
                    font_size=14.0,
                    url="https://example.com",
                ))],
            ),
        ]
        yaml_str = serialize_tree(blocks)
        doc = validated_parse(yaml_str)
        assert len(doc["body"]) == 2


# =============================================================================
# compress_doc integration test
# =============================================================================


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
        """compress_doc → YAML emit → validated_parse passes with zero errors."""
        from .rewrite_rules import compress_doc, extract_appendix

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
        """compress_doc → YAML serialize → validated_parse round-trips."""
        from .rewrite_rules import compress_doc, extract_appendix

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


# =============================================================================
# Error path quality tests
# =============================================================================


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
