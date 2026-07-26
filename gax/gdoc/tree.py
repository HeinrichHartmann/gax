"""Tree IR: rewrite-rule compressor + frozen doc-tree/v1 schema.

Ported from experiments/tree_ir_prototype/ (rewrite_rules.py + schema.py)
per gax-cvi.6 / ADR 035.

Design (ADR 035 "Implementation strategy: top-down node rewriting"):

The compressor starts from the full raw in-memory doc JSON tree and walks
it top-down, REPLACING nodes with compressed variants where an invertible
rewrite rule applies.  Unhandled nodes stay verbatim.

Each rule = (matches(node), compress(node), expand(compact)):
- matches: predicate on a raw JSON node
- compress: raw node -> compact representation
- expand: compact representation -> raw node (inverse)
- Property test: expand(compress(node)) == node for every matched node

The partially-rewritten mixed tree IS the Tree IR.

Schema layer (from gax-75t):
- Validates BEFORE any write/diff: invalid YAML never reaches the plan engine.
- Lossless YAML parsing via BaseLoader (No -> "No", 3.10 -> "3.10").
- Typed attribute conversion at the schema layer.
- Precise error paths for LLM self-correction.
- Edit contract: appendix entries and ref:rNN values are immutable.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ============================================================================
# Rule protocol
# ============================================================================


@dataclass
class RewriteRule:
    """A single invertible rewrite rule."""

    name: str
    # Which node types/positions this rule handles
    matches: Callable[[dict, str], bool]  # (node, context) -> bool
    # Raw node -> compact form
    compress: Callable[[dict], Any]
    # Compact form -> raw node
    expand: Callable[[Any], dict]
    # Priority (lower = applied first)
    priority: int = 100


# ============================================================================
# Helper: color conversion
# ============================================================================


def _color_to_hex(color_obj: dict) -> Optional[str]:
    """Convert Docs API color object to hex string."""
    if not color_obj:
        return None
    rgb = color_obj.get("color", {}).get("rgbColor", {})
    if not rgb:
        return None
    r = int(rgb.get("red", 0) * 255)
    g = int(rgb.get("green", 0) * 255)
    b = int(rgb.get("blue", 0) * 255)
    if r == 0 and g == 0 and b == 0:
        return None
    return f"#{r:02x}{g:02x}{b:02x}"


def _hex_to_color(hex_str: str) -> dict:
    """Convert hex string to Docs API color object.

    Omits zero-valued components to match the API's convention (the Docs API
    does not include 0.0 components in rgbColor).
    """
    hex_str = hex_str.lstrip("#")
    r = int(hex_str[0:2], 16) / 255.0
    g = int(hex_str[2:4], 16) / 255.0
    b = int(hex_str[4:6], 16) / 255.0
    rgb: dict = {}
    if r != 0.0:
        rgb["red"] = r
    if g != 0.0:
        rgb["green"] = g
    if b != 0.0:
        rgb["blue"] = b
    return {"color": {"rgbColor": rgb}}


def _is_link_blue(hex_color: str) -> bool:
    """Check if a color is the standard Google Docs link blue (with tolerance).

    The API reports link color as approximately #1155cc but float->int rounding
    can produce #1154cc or #1156cc.
    """
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    # Standard link blue is approximately R=17, G=85, B=204
    return abs(r - 17) <= 2 and abs(g - 85) <= 2 and abs(b - 204) <= 2


# ============================================================================
# Known table-cell paragraph style defaults
# ============================================================================

_TABLE_CELL_DEFAULT_KEYS = {
    "borderBetween", "borderTop", "borderBottom", "borderLeft", "borderRight",
    "keepLinesTogether", "keepWithNext", "avoidWidowAndOrphan",
    "shading", "pageBreakBefore",
}


def _is_table_cell_default(key: str, value: Any) -> bool:
    """Check if a paragraphStyle key/value is a known table-cell default."""
    if key in ("borderBetween", "borderTop", "borderBottom", "borderLeft", "borderRight"):
        # Default border: color={}, width/padding unit=PT, dashStyle=SOLID
        if isinstance(value, dict):
            return (
                value.get("dashStyle") == "SOLID"
                and value.get("color") == {}
                and value.get("width", {}).get("unit") == "PT"
                and not value.get("width", {}).get("magnitude")
            )
        return False
    if key == "keepLinesTogether":
        return value is False
    if key == "keepWithNext":
        return value is False
    if key == "avoidWidowAndOrphan":
        return value is False
    if key == "pageBreakBefore":
        return value is False
    if key == "shading":
        return isinstance(value, dict) and value.get("backgroundColor") == {}
    # Table cells carry alignment=START as default
    if key == "alignment":
        return value == "START"
    # Zero-magnitude dimension defaults
    if key in ("spaceAbove", "spaceBelow", "indentFirstLine", "indentStart", "indentEnd"):
        return isinstance(value, dict) and not value.get("magnitude") and value.get("unit") == "PT"
    # Default lineSpacing in table cells
    if key == "lineSpacing":
        return value == 100
    return False


# ============================================================================
# TEXT RUN compression/expansion (shared helper, not a top-level rule)
# ============================================================================


def _compress_text_runs(elements: list[dict]) -> list[Any]:
    """Compress a list of paragraph elements (textRuns) to compact runs.

    - Plain unstyled text -> string
    - Styled text -> {t: "text", b: true, ...}
    - Link-implied styles suppressed when url present
    """
    runs: list[Any] = []
    for idx, elem in enumerate(elements):
        tr = elem.get("textRun")
        if not tr:
            # Non-textRun element (inlineObjectElement, etc.) -> verbatim
            runs.append({"_verbatim_element": elem})
            continue

        text = tr["content"]
        is_last = idx == len(elements) - 1
        if is_last and text.endswith("\n"):
            text = text[:-1]
        if not text:
            continue

        style = tr.get("textStyle", {})
        compact_style = _compress_text_style(style)

        if not compact_style:
            runs.append(text)
        else:
            compact_style["t"] = text
            runs.append(compact_style)

    return runs


def _expand_text_runs(runs: list[Any]) -> list[dict]:
    """Expand compact runs back to raw paragraph elements."""
    elements = []
    for i, run in enumerate(runs):
        if isinstance(run, str):
            text = run
            if i == len(runs) - 1:
                text += "\n"  # Paragraph-terminating newline
            elements.append({
                "textRun": {
                    "content": text,
                    "textStyle": {},
                }
            })
        elif isinstance(run, dict):
            if "_verbatim_element" in run:
                elements.append(run["_verbatim_element"])
                continue
            text = run.get("t", "")
            if i == len(runs) - 1:
                text += "\n"
            style = _expand_text_style(run)
            elements.append({
                "textRun": {
                    "content": text,
                    "textStyle": style,
                }
            })
    # If no elements, add empty newline
    if not elements:
        elements.append({"textRun": {"content": "\n", "textStyle": {}}})
    return elements


def _compress_text_style(style: dict) -> dict:
    """Compress a textStyle dict to compact keys, suppressing link-implied styles."""
    if not style:
        return {}

    result: dict = {}
    has_link = "link" in style and style["link"].get("url")

    if style.get("bold"):
        result["b"] = True
    if style.get("italic"):
        result["i"] = True
    if style.get("strikethrough"):
        result["s"] = True
    # Suppress underline if link-implied
    if style.get("underline"):
        if not has_link:
            result["u"] = True
    if has_link:
        result["url"] = style["link"]["url"]

    # Color -- suppress link-implied blue (#1155cc and close variants)
    fg = style.get("foregroundColor")
    if fg:
        hex_color = _color_to_hex(fg)
        if hex_color:
            # Suppress the standard link blue when we have a URL
            if has_link and _is_link_blue(hex_color):
                # Store exact value for faithful round-trip
                result["_link_fg"] = fg
            else:
                result["color"] = hex_color

    bg = style.get("backgroundColor")
    if bg:
        hex_bg = _color_to_hex(bg)
        if hex_bg:
            result["bg"] = hex_bg

    wff = style.get("weightedFontFamily")
    if wff and wff.get("fontFamily"):
        result["font"] = wff["fontFamily"]

    fs = style.get("fontSize")
    if fs and fs.get("magnitude"):
        result["size"] = fs["magnitude"]

    baseline = style.get("baselineOffset")
    if baseline and baseline != "NONE":
        result["baseline"] = baseline

    return result


def _expand_text_style(compact: dict) -> dict:
    """Expand compact text style back to raw textStyle dict."""
    style: dict = {}
    has_url = "url" in compact

    if compact.get("b"):
        style["bold"] = True
    if compact.get("i"):
        style["italic"] = True
    if compact.get("s"):
        style["strikethrough"] = True
    if compact.get("u"):
        style["underline"] = True
    elif has_url:
        # Restore link-implied underline
        style["underline"] = True

    if has_url:
        style["link"] = {"url": compact["url"]}

    if "color" in compact:
        style["foregroundColor"] = _hex_to_color(compact["color"])
    elif has_url:
        # Restore link-implied foreground color (exact value if stored)
        if "_link_fg" in compact:
            style["foregroundColor"] = compact["_link_fg"]
        else:
            style["foregroundColor"] = _hex_to_color("#1155cc")

    if "bg" in compact:
        style["backgroundColor"] = _hex_to_color(compact["bg"])

    if "font" in compact:
        style["weightedFontFamily"] = {"fontFamily": compact["font"], "weight": 400}

    if "size" in compact:
        style["fontSize"] = {"magnitude": compact["size"], "unit": "PT"}

    if "baseline" in compact:
        style["baselineOffset"] = compact["baseline"]

    return style


# ============================================================================
# PARAGRAPH STYLE compression/expansion (shared helper)
# ============================================================================


def _compress_para_style(
    style: dict,
    exclude_named: bool = False,
    suppress_indent: bool = False,
    is_table_cell: bool = False,
) -> dict:
    """Compress paragraphStyle to compact form, eliding defaults."""
    result: dict = {}

    for key, value in style.items():
        # Skip namedStyleType (encoded in block key)
        if key == "namedStyleType":
            if not exclude_named:
                if value != "NORMAL_TEXT":
                    result["named"] = value
            continue

        # Preserve direction (common default is LEFT_TO_RIGHT but we keep it
        # for exact round-trip fidelity with the API)
        if key == "direction":
            result["_raw_direction"] = value
            continue

        # Preserve headingId (API-generated ID for headings)
        if key == "headingId":
            result["_raw_headingId"] = value
            continue

        # Suppress list indent when depth captures it
        if suppress_indent and key in ("indentStart", "indentEnd", "indentFirstLine"):
            continue

        # Elide known table-cell defaults
        if is_table_cell and _is_table_cell_default(key, value):
            continue

        # Compact specific keys
        if key == "alignment":
            if value != "START":
                result["align"] = value.lower()
            continue

        if key == "lineSpacing":
            result["line_spacing"] = value
            continue

        if key == "spaceAbove":
            if isinstance(value, dict) and value.get("magnitude"):
                result["space_above"] = value["magnitude"]
            continue

        if key == "spaceBelow":
            if isinstance(value, dict) and value.get("magnitude"):
                result["space_below"] = value["magnitude"]
            continue

        if key == "indentStart":
            if isinstance(value, dict) and value.get("magnitude"):
                result["indent_start"] = value["magnitude"]
            continue

        if key == "indentEnd":
            if isinstance(value, dict) and value.get("magnitude"):
                result["indent_end"] = value["magnitude"]
            continue

        if key == "indentFirstLine":
            if isinstance(value, dict) and value.get("magnitude"):
                result["indent_first"] = value["magnitude"]
            continue

        if key == "spacingMode":
            if is_table_cell and value == "COLLAPSE_LISTS":
                continue  # Table cell default
            result["_raw_" + key] = value
            continue

        # Unknown key -> preserve with _raw_ prefix
        result["_raw_" + key] = value

    return result


def _expand_para_style(compact: dict, is_table_cell: bool = False) -> dict:
    """Expand compact paragraph style back to raw."""
    style: dict = {}

    if "named" in compact:
        style["namedStyleType"] = compact["named"]

    if "align" in compact:
        style["alignment"] = compact["align"].upper()

    if "line_spacing" in compact:
        style["lineSpacing"] = compact["line_spacing"]

    if "space_above" in compact:
        style["spaceAbove"] = {"magnitude": compact["space_above"], "unit": "PT"}

    if "space_below" in compact:
        style["spaceBelow"] = {"magnitude": compact["space_below"], "unit": "PT"}

    if "indent_start" in compact:
        style["indentStart"] = {"magnitude": compact["indent_start"], "unit": "PT"}

    if "indent_end" in compact:
        style["indentEnd"] = {"magnitude": compact["indent_end"], "unit": "PT"}

    if "indent_first" in compact:
        style["indentFirstLine"] = {"magnitude": compact["indent_first"], "unit": "PT"}

    # Restore _raw_ prefixed keys
    for key, value in compact.items():
        if key.startswith("_raw_"):
            raw_key = key[5:]
            style[raw_key] = value

    # Restore table-cell defaults if expanding in table context
    if is_table_cell:
        _restore_table_cell_defaults(style)

    return style


def _restore_table_cell_defaults(style: dict):
    """Restore known table-cell default paragraph style fields."""
    default_border = {
        "color": {},
        "width": {"unit": "PT"},
        "padding": {"unit": "PT"},
        "dashStyle": "SOLID",
    }
    for key in ("borderBetween", "borderTop", "borderBottom", "borderLeft", "borderRight"):
        if key not in style:
            style[key] = copy.deepcopy(default_border)
    style.setdefault("keepLinesTogether", False)
    style.setdefault("keepWithNext", False)
    style.setdefault("avoidWidowAndOrphan", False)
    style.setdefault("shading", {"backgroundColor": {}})
    style.setdefault("pageBreakBefore", False)
    style.setdefault("spacingMode", "COLLAPSE_LISTS")
    # Additional table-cell defaults observed from the live API
    style.setdefault("alignment", "START")
    style.setdefault("lineSpacing", 100)
    style.setdefault("spaceAbove", {"unit": "PT"})
    style.setdefault("spaceBelow", {"unit": "PT"})
    style.setdefault("indentFirstLine", {"unit": "PT"})
    style.setdefault("indentStart", {"unit": "PT"})
    style.setdefault("indentEnd", {"unit": "PT"})


# ============================================================================
# HEADING rule
# ============================================================================

HEADING_STYLES = {
    "HEADING_1": 1, "HEADING_2": 2, "HEADING_3": 3,
    "HEADING_4": 4, "HEADING_5": 5, "HEADING_6": 6,
}
HEADING_STYLE_MAP = {v: k for k, v in HEADING_STYLES.items()}


def _is_heading_element(node: dict, context: str) -> bool:
    """Match a structural element that is a heading paragraph."""
    if context != "body_element":
        return False
    para = node.get("paragraph")
    if not para:
        return False
    style = para.get("paragraphStyle", {})
    return style.get("namedStyleType", "") in HEADING_STYLES


def _compress_heading(node: dict) -> dict:
    """Compress a heading element to compact form."""
    para = node["paragraph"]
    style = para.get("paragraphStyle", {})
    level = HEADING_STYLES[style["namedStyleType"]]
    elements = para.get("elements", [])

    runs = _compress_text_runs(elements)
    key = f"h{level}"

    # Compress paragraph style (minus the namedStyleType which is in the key)
    para_style = _compress_para_style(style, exclude_named=True)

    if len(runs) == 1 and isinstance(runs[0], str) and not para_style:
        return {key: runs[0]}
    result: dict = {}
    if len(runs) == 1 and isinstance(runs[0], str):
        result["t"] = runs[0]
    else:
        result["runs"] = runs
    if para_style:
        result["style"] = para_style
    return {key: result}


def _expand_heading(compact: dict) -> dict:
    """Expand a compact heading back to raw structural element."""
    # Find the hN key
    level = None
    key = None
    for k in compact:
        if k.startswith("h") and k[1:].isdigit():
            level = int(k[1:])
            key = k
            break
    if level is None:
        raise ValueError(f"Not a heading compact: {compact}")

    val = compact[key]
    named_style = HEADING_STYLE_MAP[level]

    if isinstance(val, str):
        runs = [val]
        para_style_extra = {}
    else:
        runs = val.get("runs", [val.get("t", "")])
        if isinstance(runs, str):
            runs = [runs]
        para_style_extra = val.get("style", {})

    elements = _expand_text_runs(runs)
    para_style = _expand_para_style(para_style_extra)
    para_style["namedStyleType"] = named_style

    return {
        "paragraph": {
            "elements": elements,
            "paragraphStyle": para_style,
        }
    }


heading_rule = RewriteRule(
    name="heading",
    matches=_is_heading_element,
    compress=_compress_heading,
    expand=_expand_heading,
    priority=10,
)


# ============================================================================
# PARAGRAPH rule (non-heading, non-bullet)
# ============================================================================


def _is_paragraph_element(node: dict, context: str) -> bool:
    """Match a plain paragraph (not heading, not bullet)."""
    if context != "body_element":
        return False
    para = node.get("paragraph")
    if not para:
        return False
    style = para.get("paragraphStyle", {})
    if style.get("namedStyleType", "NORMAL_TEXT") in HEADING_STYLES:
        return False
    if para.get("bullet"):
        return False
    return True


def _compress_paragraph(node: dict) -> dict:
    """Compress a paragraph element."""
    para = node["paragraph"]
    elements = para.get("elements", [])
    runs = _compress_text_runs(elements)

    style = para.get("paragraphStyle", {})
    para_style = _compress_para_style(style, exclude_named=False)

    # Simple case: single unstyled run, no paragraph style
    if len(runs) == 1 and isinstance(runs[0], str) and not para_style:
        return {"p": runs[0]}

    result: dict = {}
    if len(runs) == 1 and isinstance(runs[0], str):
        result["t"] = runs[0]
    else:
        result["runs"] = runs
    if para_style:
        result["style"] = para_style
    return {"p": result}


def _expand_paragraph(compact: dict) -> dict:
    """Expand a compact paragraph back to raw."""
    val = compact["p"]

    if isinstance(val, str):
        runs = [val]
        para_style_extra = {}
    else:
        runs = val.get("runs", [val.get("t", "")])
        if isinstance(runs, str):
            runs = [runs]
        para_style_extra = val.get("style", {})

    elements = _expand_text_runs(runs)
    para_style = _expand_para_style(para_style_extra)
    para_style["namedStyleType"] = "NORMAL_TEXT"

    return {
        "paragraph": {
            "elements": elements,
            "paragraphStyle": para_style,
        }
    }


paragraph_rule = RewriteRule(
    name="paragraph",
    matches=_is_paragraph_element,
    compress=_compress_paragraph,
    expand=_expand_paragraph,
    priority=20,
)


# ============================================================================
# LIST ITEM rule
# ============================================================================


def _is_list_item_element(node: dict, context: str) -> bool:
    """Match a bullet/numbered list paragraph."""
    if context != "body_element":
        return False
    para = node.get("paragraph")
    if not para:
        return False
    return para.get("bullet") is not None


def _compress_list_item(node: dict) -> dict:
    """Compress a list item paragraph."""
    para = node["paragraph"]
    bullet = para["bullet"]
    elements = para.get("elements", [])
    runs = _compress_text_runs(elements)

    style = para.get("paragraphStyle", {})

    # Capture raw indent before suppression for faithful round-trip
    raw_indent_start = style.get("indentStart")
    raw_indent_first = style.get("indentFirstLine")

    # Suppress list-indent from serialization (depth captures it)
    para_style = _compress_para_style(style, exclude_named=False, suppress_indent=True)

    nesting = bullet.get("nestingLevel", 0)

    # Determine ordered/unordered from listId context (stored in metadata)
    # For now, use "li" with metadata
    result: dict = {}
    if len(runs) == 1 and isinstance(runs[0], str):
        result["t"] = runs[0]
    else:
        result["runs"] = runs
    if nesting > 0:
        result["depth"] = nesting
    if para_style:
        result["style"] = para_style

    # Include bullet metadata for faithful round-trip
    result["_bullet"] = bullet

    # Store raw indent values for faithful restore (formula may not match)
    if raw_indent_start is not None:
        result["_indent_start"] = raw_indent_start
    if raw_indent_first is not None:
        result["_indent_first"] = raw_indent_first

    kind = "li"  # generic; ordering resolved at group level
    return {kind: result}


def _expand_list_item(compact: dict) -> dict:
    """Expand a compact list item back to raw."""
    val = compact["li"]

    runs = val.get("runs", [val.get("t", "")])
    if isinstance(runs, str):
        runs = [runs]
    para_style_extra = val.get("style", {})
    bullet = val.get("_bullet", {"listId": "", "nestingLevel": 0})
    depth = val.get("depth", 0)

    elements = _expand_text_runs(runs)
    para_style = _expand_para_style(para_style_extra)
    para_style["namedStyleType"] = "NORMAL_TEXT"

    # Restore indent -- use stored raw values for faithful round-trip,
    # fall back to formula for forward-compatibility.
    if "_indent_start" in val:
        para_style.setdefault("indentStart", val["_indent_start"])
    else:
        nesting = depth or bullet.get("nestingLevel", 0)
        para_style.setdefault("indentStart", {"magnitude": 36 * (nesting + 1), "unit": "PT"})
    if "_indent_first" in val:
        para_style.setdefault("indentFirstLine", val["_indent_first"])
    else:
        nesting = depth or bullet.get("nestingLevel", 0)
        para_style.setdefault("indentFirstLine", {"magnitude": 18 + 36 * nesting, "unit": "PT"})

    result = {
        "paragraph": {
            "elements": elements,
            "paragraphStyle": para_style,
            "bullet": bullet,
        }
    }
    return result


list_item_rule = RewriteRule(
    name="list_item",
    matches=_is_list_item_element,
    compress=_compress_list_item,
    expand=_expand_list_item,
    priority=15,
)


# ============================================================================
# TABLE rule
# ============================================================================


def _is_table_element(node: dict, context: str) -> bool:
    """Match a table structural element."""
    if context != "body_element":
        return False
    return "table" in node


def _compress_table(node: dict) -> dict:
    """Compress a table element."""
    table = node["table"]
    rows_data = []
    row_styles = []
    for row in table.get("tableRows", []):
        row_cells = []
        for cell in row.get("tableCells", []):
            cell_compact = _compress_table_cell(cell)
            row_cells.append(cell_compact)
        rows_data.append(row_cells)
        # Preserve tableRowStyle if present
        row_styles.append(row.get("tableRowStyle"))

    result: dict = {"rows": rows_data}
    # Preserve table-level metadata that isn't in cells
    if "rows" in table:
        result["_nrows"] = table["rows"]
    if "columns" in table:
        result["_cols"] = table["columns"]
    if "tableStyle" in table:
        result["_tableStyle"] = table["tableStyle"]
    # Preserve row styles for faithful round-trip
    if any(rs is not None for rs in row_styles):
        result["_rowStyles"] = row_styles
    return {"table": result}


def _compress_table_cell(cell: dict) -> Any:
    """Compress a single table cell.

    For faithful round-trip, stores the raw paragraphStyle as an opaque blob
    (_raw_ps) when table-cell defaults are present.  This is the "verbatim
    escape hatch" from ADR 035 -- readable content is lifted out, noise is
    preserved opaquely for faithful push.
    """
    content = cell.get("content", [])
    cell_style = cell.get("tableCellStyle")

    # Single-paragraph cell (most common)
    if len(content) == 1 and "paragraph" in content[0]:
        para = content[0]["paragraph"]
        elements = para.get("elements", [])
        runs = _compress_text_runs(elements)
        raw_style = para.get("paragraphStyle", {})

        # For simple cells without cell-level noise, return plain string
        if (len(runs) == 1 and isinstance(runs[0], str)
                and not cell_style
                and raw_style == {"namedStyleType": "NORMAL_TEXT"}):
            return runs[0]

        result: dict = {}
        if len(runs) == 1 and isinstance(runs[0], str):
            result["t"] = runs[0]
        else:
            result["runs"] = runs

        # Store raw paragraphStyle verbatim for faithful round-trip
        # (the readable serialization suppresses this; the push needs it)
        if raw_style:
            result["_raw_ps"] = raw_style

        # Preserve tableCellStyle for faithful round-trip
        if cell_style:
            result["_cellStyle"] = cell_style
        return result

    # Multi-paragraph or complex cell: preserve verbatim
    return {"_verbatim": content, "_cellStyle": cell_style}


def _expand_table(compact: dict) -> dict:
    """Expand a compact table back to raw."""
    table_data = compact["table"]
    rows_raw = table_data.get("rows", [])
    row_styles = table_data.get("_rowStyles", [None] * len(rows_raw))

    table_rows = []
    for i, row_cells in enumerate(rows_raw):
        cells = []
        for cell_compact in row_cells:
            cell = _expand_table_cell(cell_compact)
            cells.append(cell)
        row_dict: dict = {"tableCells": cells}
        # Restore tableRowStyle if it was preserved
        if i < len(row_styles) and row_styles[i] is not None:
            row_dict["tableRowStyle"] = row_styles[i]
        table_rows.append(row_dict)

    result: dict = {"tableRows": table_rows}
    if "_nrows" in table_data:
        result["rows"] = table_data["_nrows"]
    if "_cols" in table_data:
        result["columns"] = table_data["_cols"]
    if "_tableStyle" in table_data:
        result["tableStyle"] = table_data["_tableStyle"]
    return {"table": result}


def _expand_table_cell(compact: Any) -> dict:
    """Expand a compact table cell back to raw."""
    if isinstance(compact, str):
        # Simple text cell (no raw style stored -- minimal original)
        elements = _expand_text_runs([compact])
        return {
            "content": [{
                "paragraph": {
                    "elements": elements,
                    "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                }
            }]
        }
    if isinstance(compact, dict):
        if "_verbatim" in compact:
            cell: dict = {"content": compact["_verbatim"]}
            if compact.get("_cellStyle"):
                cell["tableCellStyle"] = compact["_cellStyle"]
            return cell
        # Runs + optional style
        runs = compact.get("runs", [compact.get("t", "")])
        if isinstance(runs, str):
            runs = [runs]
        elements = _expand_text_runs(runs)

        # Restore paragraphStyle from raw blob if stored, else minimal
        para_style = compact.get("_raw_ps", {"namedStyleType": "NORMAL_TEXT"})

        cell_result: dict = {
            "content": [{
                "paragraph": {
                    "elements": elements,
                    "paragraphStyle": para_style,
                }
            }]
        }
        # Restore tableCellStyle if preserved
        if "_cellStyle" in compact:
            cell_result["tableCellStyle"] = compact["_cellStyle"]
        return cell_result
    # Fallback
    return {"content": []}


table_rule = RewriteRule(
    name="table",
    matches=_is_table_element,
    compress=_compress_table,
    expand=_expand_table,
    priority=30,
)


# ============================================================================
# TABLE OF CONTENTS passthrough rule (100% corpus coverage)
# ============================================================================


def _is_toc_element(node: dict, context: str) -> bool:
    """Match a tableOfContents structural element."""
    if context != "body_element":
        return False
    return "tableOfContents" in node


def _compress_toc(node: dict) -> dict:
    """Compress a tableOfContents to compact form (opaque passthrough)."""
    return {"toc": node["tableOfContents"]}


def _expand_toc(compact: dict) -> dict:
    """Expand a compact tableOfContents back to raw."""
    return {"tableOfContents": compact["toc"]}


toc_rule = RewriteRule(
    name="toc",
    matches=_is_toc_element,
    compress=_compress_toc,
    expand=_expand_toc,
    priority=50,
)


# ============================================================================
# Rule registry
# ============================================================================

ALL_RULES: list[RewriteRule] = sorted(
    [heading_rule, paragraph_rule, list_item_rule, table_rule, toc_rule],
    key=lambda r: r.priority,
)


# ============================================================================
# Compressor: top-down walk
# ============================================================================


@dataclass
class CompressResult:
    """Result of compressing a document."""

    body: list[Any]  # Compressed body elements
    stats: dict = field(default_factory=dict)  # {rule_name: count}
    verbatim_count: int = 0  # Nodes that passed through uncompressed


def _is_ordered_list(list_id: str, lists: Optional[dict]) -> bool:
    """Determine if a list is ordered from the document's lists dict."""
    if not lists or list_id not in lists:
        return False  # Default to unordered
    list_def = lists[list_id]
    # Check first nesting level's glyph type
    props = list_def.get("listProperties", {}).get("nestingLevels", [{}])
    if props:
        glyph = props[0].get("glyphType", "")
        # Ordered lists use DECIMAL, ALPHA, ROMAN, etc.
        return glyph in ("DECIMAL", "ALPHA", "UPPER_ALPHA", "ROMAN", "UPPER_ROMAN")
    return False


def _group_list_items(body: list[Any], lists: Optional[dict] = None) -> list[Any]:
    """Group consecutive {li: ...} items into {ul: ...} / {ol: ...} containers.

    Adjacent list items with the same listId become one container node.
    This is a post-compression structural transform -- individual items
    retain their internal structure; the container just wraps them.
    """
    grouped: list[Any] = []
    i = 0
    while i < len(body):
        item = body[i]
        if isinstance(item, dict) and "li" in item:
            # Start of a list run -- collect all consecutive items with same listId
            list_id = item["li"].get("_bullet", {}).get("listId", "")
            items_run = [item["li"]]
            j = i + 1
            while j < len(body):
                next_item = body[j]
                if isinstance(next_item, dict) and "li" in next_item:
                    next_id = next_item["li"].get("_bullet", {}).get("listId", "")
                    if next_id == list_id:
                        items_run.append(next_item["li"])
                        j += 1
                        continue
                break
            # Determine list type
            ordered = _is_ordered_list(list_id, lists)
            kind = "ol" if ordered else "ul"
            container: dict = {"items": items_run, "_listId": list_id}
            grouped.append({kind: container})
            i = j
        else:
            grouped.append(item)
            i += 1
    return grouped


def _ungroup_list_containers(body: list[Any]) -> list[Any]:
    """Ungroup {ul: ...} / {ol: ...} containers back into individual {li: ...} items.

    This is the inverse of _group_list_items -- a pre-expansion structural transform.
    """
    ungrouped: list[Any] = []
    for item in body:
        if isinstance(item, dict):
            if "ul" in item or "ol" in item:
                kind = "ul" if "ul" in item else "ol"
                container = item[kind]
                items = container.get("items", [])
                for li_val in items:
                    ungrouped.append({"li": li_val})
                continue
        ungrouped.append(item)
    return ungrouped


def compress_doc(body_content: list[dict], lists: Optional[dict] = None) -> CompressResult:
    """Compress a document body using top-down rewrite rules.

    Args:
        body_content: The body.content array from documents().get()
        lists: The document's lists dict (for list item context)

    Returns:
        CompressResult with compressed body and stats
    """
    result = CompressResult(body=[], stats={r.name: 0 for r in ALL_RULES})

    for elem in body_content:
        # Skip section breaks
        if "sectionBreak" in elem:
            continue

        # Strip index fields (they don't belong in the serialized form)
        node = _strip_indices(elem)

        # Try each rule
        matched = False
        for rule in ALL_RULES:
            if rule.matches(node, "body_element"):
                compact = rule.compress(node)
                result.body.append(compact)
                result.stats[rule.name] += 1
                matched = True
                break

        if not matched:
            # Verbatim passthrough
            result.body.append({"_verbatim": node})
            result.verbatim_count += 1

    # Group consecutive list items into containers
    result.body = _group_list_items(result.body, lists)

    return result


def expand_doc(compressed_body: list[Any]) -> list[dict]:
    """Expand a compressed body back to raw structural elements.

    This is the inverse of compress_doc.
    """
    # Ungroup list containers before expanding individual items
    flat_body = _ungroup_list_containers(compressed_body)

    elements = []
    for item in flat_body:
        if isinstance(item, dict):
            if "_verbatim" in item:
                elements.append(item["_verbatim"])
                continue

            # Find which rule handles this compact form
            expanded = _expand_item(item)
            if expanded:
                elements.append(expanded)
            else:
                # Can't expand -- treat as verbatim
                elements.append(item)
    return elements


def _expand_item(compact: dict) -> Optional[dict]:
    """Expand a single compact item by detecting its type."""
    # Heading: h1, h2, ... h6
    for level in range(1, 7):
        key = f"h{level}"
        if key in compact:
            return heading_rule.expand(compact)

    if "p" in compact:
        return paragraph_rule.expand(compact)

    if "li" in compact:
        return list_item_rule.expand(compact)

    if "table" in compact:
        return table_rule.expand(compact)

    if "toc" in compact:
        return toc_rule.expand(compact)

    return None


def _strip_indices(node: dict) -> dict:
    """Strip startIndex/endIndex from a node tree (deep copy)."""
    node = copy.deepcopy(node)
    _strip_indices_inplace(node)
    return node


def _strip_indices_inplace(obj: Any):
    """Recursively strip startIndex/endIndex from dicts."""
    if isinstance(obj, dict):
        obj.pop("startIndex", None)
        obj.pop("endIndex", None)
        for v in obj.values():
            _strip_indices_inplace(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_indices_inplace(item)


# ============================================================================
# Coverage metric
# ============================================================================


def coverage_report(result: CompressResult) -> dict:
    """Compute coverage metrics from a compress result."""
    total = sum(result.stats.values()) + result.verbatim_count
    rewritten = sum(result.stats.values())
    return {
        "total_nodes": total,
        "rewritten": rewritten,
        "verbatim": result.verbatim_count,
        "coverage_pct": (rewritten / total * 100) if total > 0 else 0,
        "per_rule": dict(result.stats),
    }


# ============================================================================
# Appendix: extract opaque payloads from body, replace with ref:rNN
# ============================================================================

# Keys that are opaque raw payloads (candidates for appendix extraction)
_APPENDIX_KEYS = {"_raw_ps", "_cellStyle", "_verbatim", "_verbatim_element"}


@dataclass
class AppendixResult:
    """Result of appendix extraction."""

    body: list[Any]  # Body with refs replacing raw payloads
    appendix: dict[str, Any]  # {rNN: payload}


def extract_appendix(body: list[Any]) -> AppendixResult:
    """Extract opaque raw payloads from the body into an appendix section.

    Walks the compressed body tree and replaces _raw_ps, _cellStyle, and
    _verbatim dicts with ref:rNN placeholders.  The payloads are collected
    into an appendix dict.

    This is the "readable head, untouchable tail" from ADR 035.
    """
    appendix: dict[str, Any] = {}
    counter = [0]  # mutable for closure

    def _next_ref() -> str:
        counter[0] += 1
        return f"r{counter[0]}"

    cleaned_body = _extract_from_list(body, appendix, _next_ref)
    return AppendixResult(body=cleaned_body, appendix=appendix)


def _extract_from_list(items: list[Any], appendix: dict, next_ref) -> list[Any]:
    """Recursively extract appendix refs from a list."""
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(_extract_from_dict(item, appendix, next_ref))
        elif isinstance(item, list):
            result.append(_extract_from_list(item, appendix, next_ref))
        else:
            result.append(item)
    return result


def _extract_from_dict(d: dict, appendix: dict, next_ref) -> dict:
    """Recursively extract appendix refs from a dict."""
    result: dict = {}
    for key, value in d.items():
        if key in _APPENDIX_KEYS and isinstance(value, (dict, list)):
            ref = next_ref()
            appendix[ref] = value
            result[key] = f"ref:{ref}"
        elif isinstance(value, dict):
            result[key] = _extract_from_dict(value, appendix, next_ref)
        elif isinstance(value, list):
            result[key] = _extract_from_list(value, appendix, next_ref)
        else:
            result[key] = value
    return result


def resolve_appendix(body: list[Any], appendix: dict[str, Any]) -> list[Any]:
    """Resolve ref:rNN placeholders in the body by looking up the appendix.

    This is the inverse of extract_appendix.  If the appendix is truncated
    (missing refs), the ref string is left as-is -- the caller should resolve
    from the baseline and warn.
    """
    return _resolve_list(body, appendix)


def _resolve_list(items: list[Any], appendix: dict) -> list[Any]:
    """Recursively resolve appendix refs in a list."""
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(_resolve_dict(item, appendix))
        elif isinstance(item, list):
            result.append(_resolve_list(item, appendix))
        else:
            result.append(item)
    return result


def _resolve_dict(d: dict, appendix: dict) -> dict:
    """Recursively resolve appendix refs in a dict."""
    result: dict = {}
    for key, value in d.items():
        if key in _APPENDIX_KEYS and isinstance(value, str) and value.startswith("ref:"):
            ref = value[4:]  # strip "ref:"
            if ref in appendix:
                result[key] = appendix[ref]
            else:
                # Missing ref (truncated appendix) -- leave as-is
                result[key] = value
        elif isinstance(value, dict):
            result[key] = _resolve_dict(value, appendix)
        elif isinstance(value, list):
            result[key] = _resolve_list(value, appendix)
        else:
            result[key] = value
    return result


# ############################################################################
#
# SCHEMA: doc-tree/v1 formal validation
#
# ############################################################################


# ============================================================================
# Schema error
# ============================================================================


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


# ============================================================================
# Schema definitions
# ============================================================================

# Top-level document keys
_HEADER_KEYS = {"source", "kind", "body", "tab", "appendix"}

# The only accepted kind value
ACCEPTED_KIND = "doc-tree/v1"

# Block-level node type keys (canonical rewrite-tree shape)
_BLOCK_KEYS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "table", "toc"}

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


# ============================================================================
# BaseLoader YAML parsing
# ============================================================================


def _yaml_baseload(yaml_str: str) -> Any:
    """Parse YAML with BaseLoader -- all scalars remain as strings.

    This prevents YAML implicit typing from corrupting text content:
    No stays "No" (not False), 3.10 stays "3.10" (not 3.1),
    007 stays "007" (not 7), null stays "null" (not None).
    """
    import yaml

    return yaml.load(yaml_str, Loader=yaml.BaseLoader)  # noqa: S506


# ============================================================================
# Typed attribute conversion
# ============================================================================

# When BaseLoader is used, ALL values are strings.  The schema layer knows
# which attributes are typed and converts them.  Text positions stay as-is.


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

    BaseLoader leaves everything as strings.  This function converts known
    typed fields (booleans, numbers) at the schema layer, where the expected
    type is known.  Text positions remain untouched -- this is the key
    difference from the old coerce_text_values approach.

    Returns (converted_doc, conversion_errors).
    """
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
        return value  # Compact form -- text stays as-is

    result = dict(value)
    if "runs" in result and isinstance(result["runs"], list):
        result["runs"] = [
            _convert_run(r, f"{path}.runs[{i}]", errors)
            for i, r in enumerate(result["runs"])
        ]
    if "style" in result:
        result["style"] = _convert_para_style_schema(result["style"], f"{path}.style", errors)
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
        result["style"] = _convert_para_style_schema(result["style"], f"{path}.style", errors)
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


def _convert_para_style_schema(style: Any, path: str, errors: list[SchemaError]) -> Any:
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
            result["style"] = _convert_para_style_schema(
                result["style"], f"{path}.style", errors
            )
        return result
    if isinstance(cell, list):
        return [
            _convert_run(r, f"{path}[{i}]", errors)
            for i, r in enumerate(cell)
        ]
    return cell  # Scalar text -- stays as string


# ============================================================================
# Validation
# ============================================================================


def validate(doc: dict) -> list[SchemaError]:
    """Validate a parsed doc-tree/v1 document against the formal schema.

    Accepts both the canonical rewrite-tree shape (ul/ol containers with
    items) and the legacy flat shape (ul/ol as flat list items).

    Returns a list of SchemaError (empty = valid).  Does NOT raise.
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

    # Appendix validation (structural only -- immutability is checked separately)
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
    elif type_key == "toc":
        pass  # Opaque passthrough -- no inner validation


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
        _validate_para_style_schema(value["style"], f"{path}.style", errors)

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


def _validate_para_style_schema(style: Any, path: str, errors: list[SchemaError]) -> None:
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
                # BaseLoader string -- will be converted later, just check it's numeric
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
        _validate_para_style_schema(cell["style"], f"{path}.style", errors)


# ============================================================================
# Appendix immutability (edit contract)
# ============================================================================


def validate_appendix_immutable(
    original: dict[str, Any],
    edited: dict[str, Any],
) -> list[SchemaError]:
    """Check that appendix entries have not been modified (edit contract).

    The appendix is the "untouchable tail": LLM edits must not change any
    appendix entry or ref:rNN value.  This function compares the original
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
        # (legitimate edit).  Only changed values are violations.

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


# ============================================================================
# Integrated parse-with-validation
# ============================================================================


def validated_parse(yaml_str: str) -> dict:
    """Parse YAML losslessly, convert typed attrs, and validate against schema.

    Uses BaseLoader so all scalars remain as their original strings
    (No -> "No", 3.10 -> "3.10", 007 -> "007").  Then converts typed
    attributes (booleans, numbers) at the schema layer where the
    expected type is known.  Text positions are never touched.

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

    # Convert typed attributes (booleans, numbers) -- text stays verbatim
    doc, conv_errors = convert_typed_attrs(doc)
    if conv_errors:
        raise SchemaValidationError(conv_errors)

    return doc
