"""Formal schema and validation for the doc-tree/v1 YAML format.

Design goals (from gax-75t / ADR 035):
- Validate BEFORE any write/diff is computed: invalid YAML never reaches
  the plan engine.
- Lossless YAML parsing: uses BaseLoader so every scalar stays as the
  original string (No stays "No", 3.10 stays "3.10", 007 stays "007").
  Typed attributes (b/i/s/u, depth, size, indent_*) are converted at
  the schema layer where the expected type is known.
- Precise error paths: every validation error names the YAML node path
  (e.g. "body[2].p.runs[0].color") so the LLM can self-correct.
- Edit contract: appendix entries and ref:rNN values are immutable;
  edits to them are rejected.
- Canonical shape: validates the rewrite-tree emission from
  rewrite_rules.py (ul/ol containers with items, opaque _-prefixed keys).
"""

from __future__ import annotations

import re
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

# Block-level node type keys (canonical rewrite-tree shape)
_BLOCK_KEYS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table"}

# Heading keys (compact form: str, full form: dict)
_HEADING_KEYS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# List container keys (canonical: {ul: {items: [...]}})
_LIST_KEYS = {"ul", "ol"}

# Allowed keys inside a full-form heading/paragraph inner dict
_BLOCK_INNER_KEYS = {"t", "runs", "style", "raw"}

# Additional keys allowed inside a list item (inside items array or flat li)
_LIST_ITEM_EXTRA_KEYS = {"depth", "_bullet", "_indent_start", "_indent_first"}

# Allowed keys inside a list container dict (canonical shape)
_LIST_CONTAINER_KEYS = {"items", "_listId"}

# Allowed keys inside a text style run dict
_TEXT_STYLE_KEYS = {
    "t", "b", "i", "s", "u", "url", "color", "bg", "font", "size",
    "baseline", "raw", "_link_fg", "_verbatim_element",
}

# Allowed keys inside a paragraph style dict
_PARA_STYLE_KEYS = {
    "align", "named", "indent_start", "indent_end", "indent_first",
    "line_spacing", "space_above", "space_below", "raw",
}
# Also allow _raw_* prefixed keys (opaque passthrough from rewrite engine)
_PARA_STYLE_RAW_PREFIX = "_raw_"

# Alignment enum values (lowercase)
_ALIGN_VALUES = {"start", "center", "end", "justified",
                 "left", "right", "justify"}

# Baseline offset enum values (case-insensitive)
_BASELINE_VALUES = {"superscript", "subscript"}

# Color pattern: #rrggbb hex
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Table allowed keys (canonical shape)
_TABLE_KEYS = {"rows", "_nrows", "_cols", "_tableStyle", "_rowStyles"}

# Table cell inner keys (when cell is a dict, not a string)
_TABLE_CELL_KEYS = {"t", "runs", "style", "_raw_ps", "_cellStyle", "_verbatim"}

# Ref pattern
_REF_RE = re.compile(r"^ref:r\d+$")


# =============================================================================
# BaseLoader YAML parsing
# =============================================================================


def _yaml_baseload(yaml_str: str) -> Any:
    """Parse YAML with BaseLoader — all scalars remain as strings.

    This prevents YAML implicit typing from corrupting text content:
    No stays "No" (not False), 3.10 stays "3.10" (not 3.1),
    007 stays "007" (not 7), null stays "null" (not None).
    """
    import yaml

    return yaml.load(yaml_str, Loader=yaml.BaseLoader)  # noqa: S506


# =============================================================================
# Typed attribute conversion
# =============================================================================

# When BaseLoader is used, ALL values are strings. The schema layer knows
# which attributes are typed and converts them. Text positions stay as-is.


def _str_to_bool(value: str, path: str, errors: list[SchemaError]) -> bool | str:
    """Convert a BaseLoader string to bool for known boolean fields."""
    if value.lower() in ("true", "yes", "on", "1"):
        return True
    if value.lower() in ("false", "no", "off", "0"):
        return False
    errors.append(SchemaError(path, f"expected boolean, got '{value}'"))
    return value  # Leave as-is on error


def _str_to_int(value: str, path: str, errors: list[SchemaError]) -> int | str:
    """Convert a BaseLoader string to int for known integer fields."""
    try:
        return int(value)
    except (ValueError, TypeError):
        errors.append(SchemaError(path, f"expected integer, got '{value}'"))
        return value


def _str_to_number(value: str, path: str, errors: list[SchemaError]) -> int | float | str:
    """Convert a BaseLoader string to int or float for known numeric fields."""
    try:
        # Try int first, then float
        if "." in value:
            return float(value)
        return int(value)
    except (ValueError, TypeError):
        errors.append(SchemaError(path, f"expected number, got '{value}'"))
        return value


def convert_typed_attrs(doc: dict) -> tuple[dict, list[SchemaError]]:
    """Convert typed attributes in a BaseLoader-parsed doc from str to proper types.

    BaseLoader leaves everything as strings. This function converts known
    typed fields (booleans, numbers) at the schema layer, where the expected
    type is known. Text positions remain untouched — this is the key
    difference from the old coerce_text_values approach.

    Returns (converted_doc, conversion_errors).
    """
    import copy
    result = copy.deepcopy(doc)
    errors: list[SchemaError] = []

    if "body" in result and isinstance(result["body"], list):
        result["body"] = [
            _convert_block(b, f"$.body[{i}]", errors)
            for i, b in enumerate(result["body"])
        ]

    return result, errors


def _convert_block(block: Any, path: str, errors: list[SchemaError]) -> Any:
    """Convert typed attributes in a single block."""
    if not isinstance(block, dict):
        return block

    result = {}
    for key, value in block.items():
        if key in _HEADING_KEYS | {"p"}:
            result[key] = _convert_block_value(value, f"{path}.{key}", errors)
        elif key in _LIST_KEYS:
            result[key] = _convert_list_value(value, f"{path}.{key}", errors)
        elif key == "table":
            result[key] = _convert_table(value, f"{path}.table", errors)
        else:
            result[key] = value
    return result


def _convert_block_value(value: Any, path: str, errors: list[SchemaError]) -> Any:
    """Convert typed attrs in a block value (heading/p)."""
    if not isinstance(value, dict):
        return value  # Compact form — text stays as-is

    result = dict(value)
    if "runs" in result and isinstance(result["runs"], list):
        result["runs"] = [
            _convert_run(r, f"{path}.runs[{i}]", errors)
            for i, r in enumerate(result["runs"])
        ]
    if "style" in result:
        result["style"] = _convert_para_style(result["style"], f"{path}.style", errors)
    return result


def _convert_list_value(value: Any, path: str, errors: list[SchemaError]) -> Any:
    """Convert typed attrs in a list container or flat list item."""
    if not isinstance(value, dict):
        return value  # Compact form

    result = dict(value)

    # Canonical container form: {items: [...], _listId: ...}
    if "items" in result and isinstance(result["items"], list):
        result["items"] = [
            _convert_list_item(item, f"{path}.items[{i}]", errors)
            for i, item in enumerate(result["items"])
        ]
        return result

    # Flat list item form (legacy): {t/runs, depth, style}
    return _convert_list_item(result, path, errors)


def _convert_list_item(item: Any, path: str, errors: list[SchemaError]) -> Any:
    """Convert typed attrs in a single list item."""
    if not isinstance(item, dict):
        return item

    result = dict(item)
    if "depth" in result and isinstance(result["depth"], str):
        result["depth"] = _str_to_int(result["depth"], f"{path}.depth", errors)
    if "runs" in result and isinstance(result["runs"], list):
        result["runs"] = [
            _convert_run(r, f"{path}.runs[{i}]", errors)
            for i, r in enumerate(result["runs"])
        ]
    if "style" in result:
        result["style"] = _convert_para_style(result["style"], f"{path}.style", errors)
    return result


def _convert_run(run: Any, path: str, errors: list[SchemaError]) -> Any:
    """Convert typed attrs in a text run."""
    if not isinstance(run, dict):
        return run  # Plain string run

    result = dict(run)
    for bool_key in ("b", "i", "s", "u"):
        if bool_key in result and isinstance(result[bool_key], str):
            result[bool_key] = _str_to_bool(result[bool_key], f"{path}.{bool_key}", errors)
    if "size" in result and isinstance(result["size"], str):
        result["size"] = _str_to_number(result["size"], f"{path}.size", errors)
    return result


def _convert_para_style(style: Any, path: str, errors: list[SchemaError]) -> Any:
    """Convert typed attrs in a paragraph style dict."""
    if not isinstance(style, dict):
        return style

    result = dict(style)
    for num_key in ("indent_start", "indent_end", "indent_first",
                    "line_spacing", "space_above", "space_below"):
        if num_key in result and isinstance(result[num_key], str):
            result[num_key] = _str_to_number(result[num_key], f"{path}.{num_key}", errors)
    return result


def _convert_table(table_val: Any, path: str, errors: list[SchemaError]) -> Any:
    """Convert typed attrs in a table."""
    if not isinstance(table_val, dict):
        return table_val

    result = dict(table_val)
    if "_nrows" in result and isinstance(result["_nrows"], str):
        result["_nrows"] = _str_to_int(result["_nrows"], f"{path}._nrows", errors)
    if "_cols" in result and isinstance(result["_cols"], str):
        result["_cols"] = _str_to_int(result["_cols"], f"{path}._cols", errors)
    if "rows" in result and isinstance(result["rows"], list):
        result["rows"] = [
            [_convert_cell(cell, f"{path}.rows[{ri}][{ci}]", errors)
             for ci, cell in enumerate(row)]
            if isinstance(row, list) else row
            for ri, row in enumerate(result["rows"])
        ]
    return result


def _convert_cell(cell: Any, path: str, errors: list[SchemaError]) -> Any:
    """Convert typed attrs in a table cell."""
    if isinstance(cell, dict):
        result = dict(cell)
        if "runs" in result and isinstance(result["runs"], list):
            result["runs"] = [
                _convert_run(r, f"{path}.runs[{i}]", errors)
                for i, r in enumerate(result["runs"])
            ]
        if "style" in result:
            result["style"] = _convert_para_style(result["style"], f"{path}.style", errors)
        return result
    if isinstance(cell, list):
        return [
            _convert_run(r, f"{path}[{i}]", errors)
            for i, r in enumerate(cell)
        ]
    return cell  # Scalar text — stays as string


# =============================================================================
# Validation
# =============================================================================


def validate(doc: dict) -> list[SchemaError]:
    """Validate a parsed doc-tree/v1 document against the formal schema.

    Accepts both the canonical rewrite-tree shape (ul/ol containers with
    items) and the legacy flat shape (ul/ol as flat list items).

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
        # Check for _verbatim (canonical fallback)
        if "_verbatim" in block:
            return  # Opaque passthrough, no validation
        errors.append(SchemaError(path, f"block has no recognized type key; expected one of {sorted(_BLOCK_KEYS)}"))
        return
    if len(type_keys) > 1:
        errors.append(SchemaError(path, f"block has multiple type keys: {sorted(type_keys)}; expected exactly one"))
        return

    type_key = type_keys.pop()

    # At block level, only the type key is allowed (no extra sibling keys)
    unknown = set(block.keys()) - {type_key}
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown key '{key}' at block level"))

    value = block[type_key]

    if type_key in _HEADING_KEYS:
        _validate_heading_value(value, f"{path}.{type_key}", errors)
    elif type_key == "p":
        _validate_paragraph_value(value, f"{path}.p", errors)
    elif type_key in _LIST_KEYS:
        _validate_list_value(value, f"{path}.{type_key}", errors)
    elif type_key == "table":
        _validate_table_value(value, f"{path}.table", errors)


def _validate_heading_value(value: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate the value of an h1-h6 key."""
    if not isinstance(value, dict):
        return  # Compact form: scalar text

    _validate_inner_block(value, path, errors, extra_keys=set())


def _validate_paragraph_value(value: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate the value of a p key."""
    if not isinstance(value, dict):
        return  # Compact form

    _validate_inner_block(value, path, errors, extra_keys=set())


def _validate_list_value(value: Any, path: str, errors: list[SchemaError]) -> None:
    """Validate the value of a ul/ol key.

    Accepts two shapes:
    1. Canonical container: {items: [...], _listId: "..."}
    2. Legacy flat item: str or {t/runs, depth, style}
    """
    if not isinstance(value, dict):
        return  # Compact form (legacy flat)

    # Canonical container shape: has 'items' key
    if "items" in value:
        _validate_list_container(value, path, errors)
        return

    # Legacy flat item shape: {t/runs, depth, style}
    _validate_inner_block(value, path, errors, extra_keys=_LIST_ITEM_EXTRA_KEYS)


def _validate_list_container(value: dict, path: str, errors: list[SchemaError]) -> None:
    """Validate a canonical list container: {items: [...], _listId: "..."}."""
    unknown = set(value.keys()) - _LIST_CONTAINER_KEYS
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown list container key '{key}'"))

    items = value.get("items")
    if not isinstance(items, list):
        errors.append(SchemaError(f"{path}.items", f"items must be a list, got {type(items).__name__}"))
        return

    for i, item in enumerate(items):
        item_path = f"{path}.items[{i}]"
        if not isinstance(item, dict):
            errors.append(SchemaError(item_path, f"list item must be a mapping, got {type(item).__name__}"))
            continue
        _validate_inner_block(item, item_path, errors, extra_keys=_LIST_ITEM_EXTRA_KEYS)


def _validate_inner_block(value: dict, path: str, errors: list[SchemaError],
                          extra_keys: set[str]) -> None:
    """Validate the inner dict of a full-form block (heading/p/list item)."""
    allowed = _BLOCK_INNER_KEYS | extra_keys
    # Allow _-prefixed keys as opaque passthrough (rewrite engine metadata)
    unknown = {k for k in value.keys() if k not in allowed and not k.startswith("_")}
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown attribute '{key}'"))

    # Must have either 't' or 'runs' (not both)
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

    # Validate depth (only present when extra_keys permits it)
    if "depth" in value and "depth" in extra_keys:
        depth = value["depth"]
        if isinstance(depth, int):
            if depth < 0:
                errors.append(SchemaError(f"{path}.depth", f"depth must be a non-negative integer, got {depth!r}"))
        elif isinstance(depth, str):
            # BaseLoader: still a string, will be converted later
            pass
        else:
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
        # Scalar runs are fine (plain unstyled text)


def _validate_run_dict(run: dict, path: str, errors: list[SchemaError]) -> None:
    """Validate a styled run dict."""
    # Allow _-prefixed keys as opaque passthrough
    unknown = {k for k in run.keys() if k not in _TEXT_STYLE_KEYS and not k.startswith("_")}
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown text style key '{key}'"))

    # 't' is required in a styled run
    if "t" not in run:
        errors.append(SchemaError(path, "styled run must have 't' (text) key"))

    # Type checks for style keys (accept both native types and BaseLoader strings)
    for bool_key in ("b", "i", "s", "u"):
        if bool_key in run:
            val = run[bool_key]
            if isinstance(val, str):
                if val.lower() not in ("true", "false", "yes", "no", "on", "off", "1", "0"):
                    errors.append(SchemaError(
                        f"{path}.{bool_key}",
                        f"'{bool_key}' must be boolean, got '{val}'"
                    ))
            elif not isinstance(val, bool):
                errors.append(SchemaError(
                    f"{path}.{bool_key}",
                    f"'{bool_key}' must be boolean, got {type(val).__name__}"
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
        if isinstance(size, str):
            try:
                fval = float(size)
                if fval <= 0:
                    errors.append(SchemaError(f"{path}.size", f"size must be positive, got {size}"))
            except ValueError:
                errors.append(SchemaError(f"{path}.size", f"size must be a number, got '{size}'"))
        elif isinstance(size, (int, float)):
            if size <= 0:
                errors.append(SchemaError(f"{path}.size", f"size must be positive, got {size}"))
        else:
            errors.append(SchemaError(f"{path}.size", f"size must be a number, got {type(size).__name__}"))

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

    # Allow _raw_* prefixed keys (opaque passthrough from rewrite engine)
    unknown = {k for k in style.keys()
               if k not in _PARA_STYLE_KEYS and not k.startswith(_PARA_STYLE_RAW_PREFIX)}
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
            if isinstance(val, str):
                # BaseLoader string — will be converted later, just check it's numeric
                try:
                    float(val)
                except ValueError:
                    errors.append(SchemaError(
                        f"{path}.{num_key}",
                        f"'{num_key}' must be a number, got '{val}'"
                    ))
            elif not isinstance(val, (int, float)):
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

    # Allow _-prefixed keys (canonical rewrite-tree metadata)
    unknown = {k for k in value.keys() if k not in _TABLE_KEYS and not k.startswith("_")}
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
        return  # Any scalar is fine

    # List: runs
    if isinstance(cell, list):
        _validate_runs(cell, path, errors)
        return

    # Dict: cell with style or _-prefixed opaque keys
    # Allow _-prefixed keys (canonical rewrite-tree: _raw_ps, _cellStyle, _verbatim)
    unknown = {k for k in cell.keys() if k not in _TABLE_CELL_KEYS and not k.startswith("_")}
    for key in sorted(unknown):
        errors.append(SchemaError(f"{path}.{key}", f"unknown table cell key '{key}'"))

    has_t = "t" in cell
    has_runs = "runs" in cell
    has_verbatim = "_verbatim" in cell
    if not has_t and not has_runs and not has_verbatim:
        errors.append(SchemaError(path, "table cell dict must have either 't', 'runs', or '_verbatim'"))
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
    """Parse YAML losslessly, convert typed attrs, and validate against schema.

    Uses BaseLoader so all scalars remain as their original strings
    (No → "No", 3.10 → "3.10", 007 → "007"). Then converts typed
    attributes (booleans, numbers) at the schema layer where the
    expected type is known. Text positions are never touched.

    Returns the converted document dict.
    Raises SchemaValidationError if validation fails.

    This is the intended entry point: nothing downstream (diff/plan)
    should run on an invalid file.
    """
    doc = _yaml_baseload(yaml_str)
    if doc is None:
        raise SchemaValidationError([SchemaError("$", "empty document")])

    if not isinstance(doc, dict):
        raise SchemaValidationError([SchemaError("$", f"document must be a mapping, got {type(doc).__name__}")])

    # Validate structure first (before type conversion)
    errors = validate(doc)
    if errors:
        raise SchemaValidationError(errors)

    # Convert typed attributes (booleans, numbers) — text stays verbatim
    doc, conv_errors = convert_typed_attrs(doc)
    if conv_errors:
        raise SchemaValidationError(conv_errors)

    return doc
