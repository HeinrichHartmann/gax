"""Formal schema and validation for the doc-tree/v1 YAML format.

Design goals (from gax-75t / ADR 035):
- Validate BEFORE any write/diff is computed: invalid YAML never reaches
  the plan engine.
- YAML implicit type coercion: force all text positions to str so that
  No/Yes/On/3.10 round-trip as strings, not bools/floats.
- Precise error paths: every validation error names the YAML node path
  (e.g. "body[2].p.runs[0].color") so the LLM can self-correct.
- Edit contract: appendix entries and ref:rNN values are immutable;
  edits to them are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# =============================================================================
# Schema error
# =============================================================================


@dataclass
class SchemaError:
    """A single schema violation with its YAML node path."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class SchemaValidationError(Exception):
    """Raised when a doc-tree/v1 document fails schema validation."""

    def __init__(self, errors: list[SchemaError]):
        self.errors = errors
        msgs = "; ".join(str(e) for e in errors[:5])
        if len(errors) > 5:
            msgs += f" ... and {len(errors) - 5} more"
        super().__init__(f"Schema validation failed ({len(errors)} errors): {msgs}")


# =============================================================================
# Schema definitions
# =============================================================================

# Top-level document keys
_HEADER_KEYS = {"source", "kind", "body", "tab", "appendix"}

# The only accepted kind value
ACCEPTED_KIND = "doc-tree/v1"

# Block-level node type keys
_BLOCK_KEYS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table"}

# Heading keys (compact form: str, full form: dict)
_HEADING_KEYS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# List item keys
_LIST_KEYS = {"ul", "ol"}

# Allowed keys inside a full-form heading/paragraph/list-item dict
_BLOCK_INNER_KEYS = {"t", "runs", "style", "depth", "raw"}

# Allowed keys inside a text style run dict
_TEXT_STYLE_KEYS = {"t", "b", "i", "s", "u", "url", "color", "bg", "font", "size", "baseline", "raw"}

# Allowed keys inside a paragraph style dict
_PARA_STYLE_KEYS = {"align", "indent_start", "indent_end", "indent_first",
                     "line_spacing", "space_above", "space_below", "raw"}

# Alignment enum values (lowercase)
_ALIGN_VALUES = {"start", "center", "end", "justified"}

# Baseline offset enum values (lowercase)
_BASELINE_VALUES = {"superscript", "subscript"}

# Color pattern: #rrggbb hex
import re
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Table allowed keys
_TABLE_KEYS = {"rows"}

# Table cell inner keys (when cell is a dict, not a string)
_TABLE_CELL_KEYS = {"t", "runs", "style"}


# =============================================================================
# YAML implicit type coercion
# =============================================================================

def _coerce_to_str(value: Any) -> str:
    """Coerce a YAML-parsed value back to its string representation.

    Handles the YAML implicit typing problem:
    - No/Yes/On/Off → bool → "No"/"Yes"/"On"/"Off"
    - 3.10 → float 3.1 → "3.10" (information lost — we coerce to "3.1")
    - null → None → ""

    For bool, we use the canonical Python repr ("True"/"False") since we
    can't know the original casing. The LLM should quote these values.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # YAML parsed Yes/No/On/Off/True/False as bool
        return str(value)
    if isinstance(value, (int, float)):
        # YAML parsed 3.10 as 3.1, version numbers, etc.
        return str(value)
    if isinstance(value, str):
        return value
    # Fallback
    return str(value)


def coerce_text_values(doc: dict) -> dict:
    """Walk a parsed doc-tree/v1 document and coerce all text positions to str.

    Text positions are:
    - Compact block values (e.g. {p: No} → {p: "No"})
    - The 't' key in run dicts and full-form blocks
    - Table cell strings

    Returns a new dict (does not mutate input).
    """
    import copy
    result = copy.deepcopy(doc)

    if "body" in result and isinstance(result["body"], list):
        result["body"] = [_coerce_block(b) for b in result["body"]]

    return result


def _coerce_block(block: Any) -> Any:
    """Coerce text values in a single block."""
    if not isinstance(block, dict):
        return block

    result = {}
    for key, value in block.items():
        if key in _HEADING_KEYS | {"p"} | _LIST_KEYS:
            result[key] = _coerce_block_value(value)
        elif key == "table":
            result[key] = _coerce_table(value)
        else:
            result[key] = value
    return result


def _coerce_block_value(value: Any) -> Any:
    """Coerce the value of a block-type key (heading/p/ul/ol).

    Compact form: the value itself is the text.
    Full form: dict with t/runs keys.
    """
    # Compact form: value is the text content
    if not isinstance(value, dict):
        return _coerce_to_str(value)

    # Full form: dict with possible t/runs keys
    result = dict(value)
    if "t" in result:
        result["t"] = _coerce_to_str(result["t"])
    if "runs" in result and isinstance(result["runs"], list):
        result["runs"] = [_coerce_run(r) for r in result["runs"]]
    return result


def _coerce_run(run: Any) -> Any:
    """Coerce text in a single run."""
    if not isinstance(run, dict):
        return _coerce_to_str(run)
    result = dict(run)
    if "t" in result:
        result["t"] = _coerce_to_str(result["t"])
    return result


def _coerce_table(table_val: Any) -> Any:
    """Coerce text values inside a table."""
    if not isinstance(table_val, dict):
        return table_val
    result = dict(table_val)
    if "rows" in result and isinstance(result["rows"], list):
        result["rows"] = [
            [_coerce_cell(cell) for cell in row]
            if isinstance(row, list) else row
            for row in result["rows"]
        ]
    return result


def _coerce_cell(cell: Any) -> Any:
    """Coerce text in a table cell."""
    if isinstance(cell, dict):
        result = dict(cell)
        if "t" in result:
            result["t"] = _coerce_to_str(result["t"])
        if "runs" in result and isinstance(result["runs"], list):
            result["runs"] = [_coerce_run(r) for r in result["runs"]]
        return result
    if isinstance(cell, list):
        return [_coerce_run(r) for r in cell]
    # Scalar cell value
    return _coerce_to_str(cell)


# =============================================================================
# Validation
# =============================================================================


def validate(doc: dict) -> list[SchemaError]:
    """Validate a parsed doc-tree/v1 document against the formal schema.

    Returns a list of SchemaError (empty = valid). Does NOT raise.
    Call validate_or_raise() if you want an exception.
    """
    errors: list[SchemaError] = []

    if not isinstance(doc, dict):
        errors.append(SchemaError("$", "document must be a mapping"))
        return errors

    # Header validation
    _validate_header(doc, errors)

    # Body validation
    if "body" in doc:
        body = doc["body"]
        if not isinstance(body, list):
            errors.append(SchemaError("$.body", "body must be a list"))
        else:
            for i, block in enumerate(body):
                _validate_block(block, f"$.body[{i}]", errors)

    # Appendix validation (structural only — immutability is checked separately)
    if "appendix" in doc:
        appendix = doc["appendix"]
        if not isinstance(appendix, dict):
            errors.append(SchemaError("$.appendix", "appendix must be a mapping"))
        else:
            for key in appendix:
                if not isinstance(key, str) or not re.match(r"^r\d+$", key):
                    errors.append(SchemaError(
                        f"$.appendix.{key}",
                        f"appendix key must match r<N> pattern, got '{key}'"
                    ))

    return errors


def validate_or_raise(doc: dict) -> None:
    """Validate and raise SchemaValidationError if invalid."""
    errors = validate(doc)
    if errors:
        raise SchemaValidationError(errors)


def _validate_header(doc: dict, errors: list[SchemaError]) -> None:
    """Validate top-level document keys."""
    unknown = set(doc.keys()) - _HEADER_KEYS
    for key in sorted(unknown):
        errors.append(SchemaError(f"$.{key}", f"unknown header key '{key}'"))

    # kind is required
    if "kind" not in doc:
        errors.append(SchemaError("$", "missing required header key 'kind'"))
    elif doc["kind"] != ACCEPTED_KIND:
        errors.append(SchemaError(
            "$.kind",
            f"kind must be '{ACCEPTED_KIND}', got '{doc['kind']}'"
        ))

    # body is required
    if "body" not in doc:
        errors.append(SchemaError("$", "missing required header key 'body'"))

    # source is optional, must be str
    if "source" in doc and not isinstance(doc["source"], str):
        errors.append(SchemaError("$.source", "source must be a string"))

    # tab is optional, must be str
    if "tab" in doc and not isinstance(doc["tab"], str):
        errors.append(SchemaError("$.tab", "tab must be a string"))


def _validate_block(block: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate a single body block."""
    if not isinstance(block, dict):
        errors.append(SchemaError(path, f"block must be a mapping, got {type(block).__name__}"))
        return

    # A block must have exactly one type key
    type_keys = set(block.keys()) & _BLOCK_KEYS
    if len(type_keys) == 0:
        errors.append(SchemaError(path, f"block has no recognized type key; expected one of {sorted(_BLOCK_KEYS)}"))
        return
    if len(type_keys) > 1:
        errors.append(SchemaError(path, f"block has multiple type keys: {sorted(type_keys)}; expected exactly one"))
        return

    # Check for unknown keys at block level
    type_key = type_keys.pop()
    unknown = set(block.keys()) - {type_key}
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown key '{key}' at block level"))

    value = block[type_key]

    if type_key in _HEADING_KEYS:
        _validate_heading_value(value, f"{path}.{type_key}", errors)
    elif type_key == "p":
        _validate_paragraph_value(value, f"{path}.p", errors)
    elif type_key in _LIST_KEYS:
        _validate_list_item_value(value, f"{path}.{type_key}", errors)
    elif type_key == "table":
        _validate_table_value(value, f"{path}.table", errors)


def _validate_heading_value(value: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate the value of an h1-h6 key."""
    # Compact form: scalar (string, or YAML-coerced value)
    if not isinstance(value, dict):
        # Accept any scalar — coercion will fix types
        return

    # Full form: dict with inner keys
    _validate_inner_block(value, path, errors, allow_depth=False)


def _validate_paragraph_value(value: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate the value of a p key."""
    if not isinstance(value, dict):
        return  # Compact form

    _validate_inner_block(value, path, errors, allow_depth=False)


def _validate_list_item_value(value: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate the value of a ul/ol key."""
    if not isinstance(value, dict):
        return  # Compact form

    _validate_inner_block(value, path, errors, allow_depth=True)


def _validate_inner_block(value: dict, path: str, errors: list[SchemaError],
                          allow_depth: bool) -> None:
    """Validate the inner dict of a full-form block (heading/p/list item)."""
    allowed = set(_BLOCK_INNER_KEYS)
    if not allow_depth:
        allowed -= {"depth"}

    unknown = set(value.keys()) - allowed
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown attribute '{key}'"))

    # Must have either 't' or 'runs' (not both, ideally)
    has_t = "t" in value
    has_runs = "runs" in value
    if not has_t and not has_runs:
        errors.append(SchemaError(path, "must have either 't' (text) or 'runs' (run list)"))
    if has_t and has_runs:
        errors.append(SchemaError(path, "cannot have both 't' and 'runs'"))

    # Validate runs
    if has_runs:
        _validate_runs(value["runs"], f"{path}.runs", errors)

    # Validate style
    if "style" in value:
        _validate_para_style(value["style"], f"{path}.style", errors)

    # Validate depth
    if "depth" in value:
        depth = value["depth"]
        if not isinstance(depth, int) or depth < 0:
            errors.append(SchemaError(f"{path}.depth", f"depth must be a non-negative integer, got {depth!r}"))

    # Validate raw (opaque dict, we just check it's a dict)
    if "raw" in value:
        raw = value["raw"]
        if not isinstance(raw, dict):
            errors.append(SchemaError(f"{path}.raw", f"raw must be a mapping, got {type(raw).__name__}"))


def _validate_runs(runs: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate a runs list."""
    if not isinstance(runs, list):
        errors.append(SchemaError(path, f"runs must be a list, got {type(runs).__name__}"))
        return

    for i, run in enumerate(runs):
        run_path = f"{path}[{i}]"
        if isinstance(run, dict):
            _validate_run_dict(run, run_path, errors)
        # Scalar runs are fine (plain unstyled text) — coercion handles type


def _validate_run_dict(run: dict, path: str, errors: list[SchemaError]) -> None:
    """Validate a styled run dict."""
    unknown = set(run.keys()) - _TEXT_STYLE_KEYS
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown text style key '{key}'"))

    # 't' is required in a styled run
    if "t" not in run:
        errors.append(SchemaError(path, "styled run must have 't' (text) key"))

    # Type checks for style keys
    for bool_key in ("b", "i", "s", "u"):
        if bool_key in run and not isinstance(run[bool_key], bool):
            errors.append(SchemaError(
                f"{path}.{bool_key}",
                f"'{bool_key}' must be boolean, got {type(run[bool_key]).__name__}"
            ))

    if "url" in run and not isinstance(run["url"], str):
        errors.append(SchemaError(f"{path}.url", f"url must be a string, got {type(run['url']).__name__}"))

    if "color" in run:
        _validate_color(run["color"], f"{path}.color", errors)
    if "bg" in run:
        _validate_color(run["bg"], f"{path}.bg", errors)

    if "font" in run and not isinstance(run["font"], str):
        errors.append(SchemaError(f"{path}.font", f"font must be a string, got {type(run['font']).__name__}"))

    if "size" in run:
        size = run["size"]
        if not isinstance(size, (int, float)):
            errors.append(SchemaError(f"{path}.size", f"size must be a number, got {type(size).__name__}"))
        elif size <= 0:
            errors.append(SchemaError(f"{path}.size", f"size must be positive, got {size}"))

    if "baseline" in run:
        baseline = run["baseline"]
        if not isinstance(baseline, str):
            errors.append(SchemaError(f"{path}.baseline", f"baseline must be a string, got {type(baseline).__name__}"))
        elif baseline.lower() not in _BASELINE_VALUES:
            errors.append(SchemaError(
                f"{path}.baseline",
                f"baseline must be one of {sorted(_BASELINE_VALUES)}, got '{baseline}'"
            ))


def _validate_color(value: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate a color value (must be #rrggbb hex)."""
    if not isinstance(value, str):
        errors.append(SchemaError(path, f"color must be a hex string (#rrggbb), got {type(value).__name__}"))
        return
    if not _COLOR_RE.match(value):
        errors.append(SchemaError(path, f"color must match #rrggbb, got '{value}'"))


def _validate_para_style(style: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate a paragraph style dict."""
    if not isinstance(style, dict):
        errors.append(SchemaError(path, f"style must be a mapping, got {type(style).__name__}"))
        return

    unknown = set(style.keys()) - _PARA_STYLE_KEYS
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown paragraph style key '{key}'"))

    if "align" in style:
        align = style["align"]
        if not isinstance(align, str):
            errors.append(SchemaError(f"{path}.align", f"align must be a string, got {type(align).__name__}"))
        elif align.lower() not in _ALIGN_VALUES:
            errors.append(SchemaError(
                f"{path}.align",
                f"align must be one of {sorted(_ALIGN_VALUES)}, got '{align}'"
            ))

    for num_key in ("indent_start", "indent_end", "indent_first",
                    "line_spacing", "space_above", "space_below"):
        if num_key in style:
            val = style[num_key]
            if not isinstance(val, (int, float)):
                errors.append(SchemaError(
                    f"{path}.{num_key}",
                    f"'{num_key}' must be a number, got {type(val).__name__}"
                ))

    if "raw" in style:
        raw = style["raw"]
        if not isinstance(raw, dict):
            errors.append(SchemaError(f"{path}.raw", f"raw must be a mapping, got {type(raw).__name__}"))


def _validate_table_value(value: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate a table block value."""
    if not isinstance(value, dict):
        errors.append(SchemaError(path, f"table must be a mapping, got {type(value).__name__}"))
        return

    unknown = set(value.keys()) - _TABLE_KEYS
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown table key '{key}'"))

    if "rows" not in value:
        errors.append(SchemaError(path, "table must have 'rows' key"))
        return

    rows = value["rows"]
    if not isinstance(rows, list):
        errors.append(SchemaError(f"{path}.rows", f"rows must be a list, got {type(rows).__name__}"))
        return

    for ri, row in enumerate(rows):
        row_path = f"{path}.rows[{ri}]"
        if not isinstance(row, list):
            errors.append(SchemaError(row_path, f"row must be a list, got {type(row).__name__}"))
            continue
        for ci, cell in enumerate(row):
            _validate_table_cell(cell, f"{row_path}[{ci}]", errors)


def _validate_table_cell(cell: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate a single table cell."""
    # Scalar: plain text
    if not isinstance(cell, (dict, list)):
        return  # Any scalar is fine (coercion handles type)

    # List: runs
    if isinstance(cell, list):
        _validate_runs(cell, path, errors)
        return

    # Dict: cell with style
    unknown = set(cell.keys()) - _TABLE_CELL_KEYS
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown table cell key '{key}'"))

    has_t = "t" in cell
    has_runs = "runs" in cell
    if not has_t and not has_runs:
        errors.append(SchemaError(path, "table cell dict must have either 't' or 'runs'"))
    if has_t and has_runs:
        errors.append(SchemaError(path, "table cell dict cannot have both 't' and 'runs'"))

    if has_runs:
        _validate_runs(cell["runs"], f"{path}.runs", errors)

    if "style" in cell:
        _validate_para_style(cell["style"], f"{path}.style", errors)


# =============================================================================
# Appendix immutability (edit contract)
# =============================================================================


def validate_appendix_immutable(
    original: dict[str, Any],
    edited: dict[str, Any],
) -> list[SchemaError]:
    """Check that appendix entries have not been modified (edit contract).

    The appendix is the "untouchable tail": LLM edits must not change any
    appendix entry or ref:rNN value. This function compares the original
    appendix with the edited version and reports any differences.

    Returns a list of SchemaError (empty = no violations).
    """
    errors: list[SchemaError] = []

    # Check for removed keys
    for key in original:
        if key not in edited:
            errors.append(SchemaError(
                f"$.appendix.{key}",
                f"appendix entry '{key}' was removed (edit contract violation)"
            ))

    # Check for added keys
    for key in edited:
        if key not in original:
            errors.append(SchemaError(
                f"$.appendix.{key}",
                f"appendix entry '{key}' was added (edit contract violation)"
            ))

    # Check for modified values
    for key in original:
        if key in edited and original[key] != edited[key]:
            errors.append(SchemaError(
                f"$.appendix.{key}",
                f"appendix entry '{key}' was modified (edit contract violation)"
            ))

    return errors


def validate_refs_immutable(
    original_body: list[Any],
    edited_body: list[Any],
) -> list[SchemaError]:
    """Check that ref:rNN values in the body have not been modified.

    Walks both body trees and ensures that any value matching the
    ref:rNN pattern is unchanged.
    """
    errors: list[SchemaError] = []
    orig_refs = _collect_refs(original_body, "$.body")
    edited_refs = _collect_refs(edited_body, "$.body")

    # Check for changed refs
    for path, orig_val in orig_refs.items():
        if path in edited_refs:
            if edited_refs[path] != orig_val:
                errors.append(SchemaError(
                    path,
                    f"ref value changed from '{orig_val}' to '{edited_refs[path]}' (edit contract violation)"
                ))
        # Note: removed refs are okay if the whole block was removed
        # (legitimate edit). Only changed values are violations.

    return errors


_REF_RE = re.compile(r"^ref:r\d+$")


def _collect_refs(obj: Any, path: str) -> dict[str, str]:
    """Collect all ref:rNN values with their paths."""
    refs: dict[str, str] = {}

    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = f"{path}.{key}"
            if isinstance(value, str) and _REF_RE.match(value):
                refs[child_path] = value
            else:
                refs.update(_collect_refs(value, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            refs.update(_collect_refs(item, f"{path}[{i}]"))

    return refs


# =============================================================================
# Integrated parse-with-validation
# =============================================================================


def validated_parse(yaml_str: str) -> dict:
    """Parse a YAML string, coerce text values, and validate against schema.

    Returns the coerced document dict.
    Raises SchemaValidationError if validation fails.

    This is the intended entry point: nothing downstream (diff/plan)
    should run on an invalid file.
    """
    import yaml

    doc = yaml.safe_load(yaml_str)
    if doc is None:
        raise SchemaValidationError([SchemaError("$", "empty document")])

    if not isinstance(doc, dict):
        raise SchemaValidationError([SchemaError("$", f"document must be a mapping, got {type(doc).__name__}")])

    # Coerce text values (fix YAML implicit typing)
    doc = coerce_text_values(doc)

    # Validate against schema
    validate_or_raise(doc)

    return doc
