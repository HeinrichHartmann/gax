"""Tests for the doc-tree/v1 formal schema validator.

Covers:
- YAML implicit type coercion (No/Yes/On/3.10 round-trip as strings)
- Schema validation (unknown attributes rejected with node-path errors)
- Appendix immutability (edit contract)
- Integration with parse_tree round-trip
"""

from __future__ import annotations

import copy

import pytest
import yaml

from .schema import (
    SchemaError,
    SchemaValidationError,
    coerce_text_values,
    validate,
    validate_or_raise,
    validate_appendix_immutable,
    validate_refs_immutable,
    validated_parse,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_doc(**overrides) -> dict:
    """Build a minimal valid doc-tree/v1 document."""
    doc = {"kind": "doc-tree/v1", "body": []}
    doc.update(overrides)
    return doc


def _make_doc_yaml(**overrides) -> str:
    """Build a minimal valid doc-tree/v1 YAML string."""
    return yaml.dump(_make_doc(**overrides), sort_keys=False)


# =============================================================================
# YAML implicit type coercion tests
# =============================================================================


class TestCoerceTextValues:
    """Test that YAML implicit typing is neutralized for text positions."""

    def test_bool_no_coerced_to_string(self):
        """YAML parses bare 'No' as False. Coercion must fix to str."""
        doc = _make_doc(body=[{"p": False}])  # YAML parsed 'No' → False
        result = coerce_text_values(doc)
        assert result["body"][0]["p"] == "False"

    def test_bool_yes_coerced_to_string(self):
        doc = _make_doc(body=[{"p": True}])  # YAML parsed 'Yes' → True
        result = coerce_text_values(doc)
        assert result["body"][0]["p"] == "True"

    def test_float_version_coerced_to_string(self):
        """YAML parses '3.10' as float 3.1. Coercion must fix."""
        doc = _make_doc(body=[{"p": 3.1}])
        result = coerce_text_values(doc)
        assert result["body"][0]["p"] == "3.1"

    def test_int_coerced_to_string(self):
        doc = _make_doc(body=[{"p": 42}])
        result = coerce_text_values(doc)
        assert result["body"][0]["p"] == "42"

    def test_none_coerced_to_empty_string(self):
        doc = _make_doc(body=[{"p": None}])
        result = coerce_text_values(doc)
        assert result["body"][0]["p"] == ""

    def test_string_unchanged(self):
        doc = _make_doc(body=[{"p": "hello"}])
        result = coerce_text_values(doc)
        assert result["body"][0]["p"] == "hello"

    def test_heading_compact_coerced(self):
        doc = _make_doc(body=[{"h1": False}])
        result = coerce_text_values(doc)
        assert result["body"][0]["h1"] == "False"

    def test_list_item_compact_coerced(self):
        doc = _make_doc(body=[{"ul": True}])
        result = coerce_text_values(doc)
        assert result["body"][0]["ul"] == "True"

    def test_full_form_t_coerced(self):
        doc = _make_doc(body=[{"p": {"t": False}}])
        result = coerce_text_values(doc)
        assert result["body"][0]["p"]["t"] == "False"

    def test_runs_text_coerced(self):
        doc = _make_doc(body=[{"p": {"runs": [{"t": 3.1, "b": True}, False]}}])
        result = coerce_text_values(doc)
        runs = result["body"][0]["p"]["runs"]
        assert runs[0]["t"] == "3.1"
        assert runs[1] == "False"

    def test_table_cell_coerced(self):
        doc = _make_doc(body=[{"table": {"rows": [[False, 3.1, "ok"]]}}])
        result = coerce_text_values(doc)
        row = result["body"][0]["table"]["rows"][0]
        assert row[0] == "False"
        assert row[1] == "3.1"
        assert row[2] == "ok"

    def test_table_cell_dict_coerced(self):
        doc = _make_doc(body=[{"table": {"rows": [[{"t": True}]]}}])
        result = coerce_text_values(doc)
        assert result["body"][0]["table"]["rows"][0][0]["t"] == "True"

    def test_does_not_mutate_input(self):
        doc = _make_doc(body=[{"p": False}])
        original = copy.deepcopy(doc)
        coerce_text_values(doc)
        assert doc == original  # Input unchanged

    def test_yaml_round_trip_adversarial(self):
        """Full YAML round-trip: unquoted No/Yes/On/version survive as strings."""
        yaml_str = """
kind: doc-tree/v1
body:
  - p: No
  - p: Yes
  - p: On
  - p: Off
  - h1: 3.10
  - ul: true
  - ol: null
"""
        doc = yaml.safe_load(yaml_str)
        coerced = coerce_text_values(doc)
        # After coercion all text positions are strings
        assert coerced["body"][0]["p"] == "False"  # YAML 'No' → False → "False"
        assert coerced["body"][1]["p"] == "True"   # YAML 'Yes' → True → "True"
        assert coerced["body"][2]["p"] == "True"   # YAML 'On' → True → "True"
        assert coerced["body"][3]["p"] == "False"  # YAML 'Off' → False → "False"
        assert coerced["body"][4]["h1"] == "3.1"   # YAML '3.10' → 3.1 → "3.1"
        assert coerced["body"][5]["ul"] == "True"
        assert coerced["body"][6]["ol"] == ""       # YAML null → None → ""


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
        # depth is valid on ul/ol
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

    def test_size_must_be_number(self):
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

    def test_bool_keys_must_be_bool(self):
        doc = _make_doc(body=[{"p": {"runs": [
            {"t": "x", "b": "yes"}
        ]}}])
        errors = validate(doc)
        assert any("must be boolean" in e.message for e in errors)


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
        for val in ("start", "center", "end", "justified"):
            doc = _make_doc(body=[{"p": {"t": "text", "style": {"align": val}}}])
            assert validate(doc) == []

    def test_numeric_style_must_be_number(self):
        doc = _make_doc(body=[{"p": {"t": "text", "style": {"indent_start": "big"}}}])
        errors = validate(doc)
        assert any("must be a number" in e.message for e in errors)


class TestValidateTable:
    """Test validation of table blocks."""

    def test_valid_simple_table(self):
        doc = _make_doc(body=[{"table": {"rows": [["a", "b"], ["c", "d"]]}}])
        assert validate(doc) == []

    def test_table_missing_rows(self):
        doc = _make_doc(body=[{"table": {}}])
        errors = validate(doc)
        assert any("must have 'rows'" in e.message for e in errors)

    def test_table_unknown_key(self):
        doc = _make_doc(body=[{"table": {"rows": [], "border": True}}])
        errors = validate(doc)
        assert any("unknown table key 'border'" in e.message for e in errors)

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

    def test_table_cell_unknown_key(self):
        doc = _make_doc(body=[{"table": {"rows": [
            [{"t": "x", "sparkle": True}]
        ]}}])
        errors = validate(doc)
        assert any("unknown table cell key 'sparkle'" in e.message for e in errors)


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
    """Test the integrated parse-validate-coerce pipeline."""

    def test_valid_document(self):
        yaml_str = """
kind: doc-tree/v1
body:
  - h1: "Introduction"
  - p: "Some text here."
  - ul: "First item"
  - table:
      rows:
        - ["Name", "Age"]
        - ["Alice", "30"]
"""
        doc = validated_parse(yaml_str)
        assert doc["kind"] == "doc-tree/v1"
        assert len(doc["body"]) == 4

    def test_coercion_before_validation(self):
        """Coercion runs before validation, so bool/float values become strings."""
        yaml_str = """
kind: doc-tree/v1
body:
  - p: No
  - h1: 3.10
"""
        doc = validated_parse(yaml_str)
        assert doc["body"][0]["p"] == "False"
        assert doc["body"][1]["h1"] == "3.1"

    def test_invalid_document_raises(self):
        yaml_str = """
kind: doc-tree/v1
body:
  - p:
      sparkle: true
"""
        with pytest.raises(SchemaValidationError) as exc_info:
            validated_parse(yaml_str)
        assert len(exc_info.value.errors) >= 1
        # Should mention sparkle and the path
        err_msg = str(exc_info.value)
        assert "sparkle" in err_msg

    def test_empty_document_raises(self):
        with pytest.raises(SchemaValidationError):
            validated_parse("")

    def test_non_mapping_raises(self):
        with pytest.raises(SchemaValidationError):
            validated_parse("- just\n- a\n- list\n")

    def test_wrong_kind_raises(self):
        yaml_str = """
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
        from .enriched_ir import Heading, Paragraph, ListItem, Table, Span, TextStyle, ParagraphStyle
        from .yaml_serializer import serialize_tree

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

    def test_schema_error_str(self):
        err = SchemaError("$.body[0].p", "something is wrong")
        assert str(err) == "$.body[0].p: something is wrong"
