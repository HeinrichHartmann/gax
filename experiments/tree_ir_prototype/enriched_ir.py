"""Enriched IR for Tree IR prototype.

Extends gax/gdoc/ir.py concepts to capture formatting attributes that
the production IR currently drops: color, font, size, underline,
alignment, background color. Also carries an opaque `raw` passthrough
per node for anything not explicitly modeled.

This is a PROTOTYPE copy — intentionally not modifying gax/gdoc/ir.py.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# Helper
# =============================================================================


def _utf16_len(s: str) -> int:
    """Length of s in UTF-16 code units (Google Docs API index unit)."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


# =============================================================================
# Data types — enriched spans and blocks
# =============================================================================


@dataclass
class TextStyle:
    """All text style attributes modeled explicitly."""

    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    underline: bool = False
    url: Optional[str] = None
    # Color as hex string e.g. "#cc0000", None = default/inherited
    foreground_color: Optional[str] = None
    background_color: Optional[str] = None
    # Font family e.g. "Arial", None = inherited
    font_family: Optional[str] = None
    # Font size in points, None = inherited
    font_size: Optional[float] = None
    # Baseline offset: "NONE", "SUPERSCRIPT", "SUBSCRIPT"
    baseline_offset: Optional[str] = None
    # Opaque: everything in textStyle we don't model explicitly
    raw: Optional[dict] = field(default=None, repr=False)

    def is_default(self) -> bool:
        """True if all style fields are at their default."""
        return (
            not self.bold
            and not self.italic
            and not self.strikethrough
            and not self.underline
            and self.url is None
            and self.foreground_color is None
            and self.background_color is None
            and self.font_family is None
            and self.font_size is None
            and self.baseline_offset is None
            and self.raw is None
        )

    def style_equal(self, other: "TextStyle") -> bool:
        """Compare two text styles semantically (ignoring raw for diff purposes)."""
        return (
            self.bold == other.bold
            and self.italic == other.italic
            and self.strikethrough == other.strikethrough
            and self.underline == other.underline
            and self.url == other.url
            and self.foreground_color == other.foreground_color
            and self.background_color == other.background_color
            and self.font_family == other.font_family
            and self.font_size == other.font_size
            and self.baseline_offset == other.baseline_offset
        )


@dataclass
class ParagraphStyle:
    """Paragraph-level style attributes."""

    alignment: Optional[str] = None  # "START", "CENTER", "END", "JUSTIFIED"
    named_style: Optional[str] = None  # "HEADING_1", "NORMAL_TEXT", etc.
    indent_start: Optional[float] = None  # points
    indent_end: Optional[float] = None
    indent_first_line: Optional[float] = None
    line_spacing: Optional[float] = None  # e.g. 115 for 1.15
    space_above: Optional[float] = None  # points
    space_below: Optional[float] = None
    # Opaque: everything in paragraphStyle we don't model explicitly
    raw: Optional[dict] = field(default=None, repr=False)

    def is_default(self) -> bool:
        return (
            self.alignment is None
            and self.named_style is None
            and self.indent_start is None
            and self.indent_end is None
            and self.indent_first_line is None
            and self.line_spacing is None
            and self.space_above is None
            and self.space_below is None
            and self.raw is None
        )

    def style_equal(self, other: "ParagraphStyle") -> bool:
        return (
            self.alignment == other.alignment
            and self.named_style == other.named_style
            and self.indent_start == other.indent_start
            and self.indent_end == other.indent_end
            and self.indent_first_line == other.indent_first_line
            and self.line_spacing == other.line_spacing
            and self.space_above == other.space_above
            and self.space_below == other.space_below
        )


@dataclass
class Span:
    """Inline text with enriched formatting."""

    text: str
    style: TextStyle = field(default_factory=TextStyle)

    @property
    def bold(self) -> bool:
        return self.style.bold

    @property
    def italic(self) -> bool:
        return self.style.italic


@dataclass
class Block:
    """Base for block-level nodes."""

    doc_range: Optional[tuple[int, int]] = field(default=None, repr=False)
    # Opaque raw data for this block element
    raw: Optional[dict] = field(default=None, repr=False)


@dataclass
class Heading(Block):
    level: int = 1
    spans: list[Span] = field(default_factory=list)
    para_style: ParagraphStyle = field(default_factory=ParagraphStyle)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class Paragraph(Block):
    spans: list[Span] = field(default_factory=list)
    para_style: ParagraphStyle = field(default_factory=ParagraphStyle)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class ListItem(Block):
    spans: list[Span] = field(default_factory=list)
    ordered: bool = False
    depth: int = 0
    para_style: ParagraphStyle = field(default_factory=ParagraphStyle)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class Table(Block):
    rows: list[list[list[Span]]] = field(default_factory=list)
    # Cell-level paragraph styles (rows x cols)
    cell_styles: list[list[ParagraphStyle]] = field(default_factory=list)
    _raw_table: Optional[dict] = field(default=None, repr=False, compare=False)


# =============================================================================
# Google Docs JSON → Enriched IR
# =============================================================================

HEADING_STYLES = {
    "HEADING_1": 1,
    "HEADING_2": 2,
    "HEADING_3": 3,
    "HEADING_4": 4,
    "HEADING_5": 5,
    "HEADING_6": 6,
}

HEADING_STYLE_MAP = {v: k for k, v in HEADING_STYLES.items()}


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
    # Don't report black as explicit color (it's the default)
    if r == 0 and g == 0 and b == 0:
        return None
    return f"#{r:02x}{g:02x}{b:02x}"


def _hex_to_color(hex_str: str) -> dict:
    """Convert hex color string to Docs API color object."""
    hex_str = hex_str.lstrip("#")
    r = int(hex_str[0:2], 16) / 255.0
    g = int(hex_str[2:4], 16) / 255.0
    b = int(hex_str[4:6], 16) / 255.0
    return {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}


# Link-blue RGB (gax-tvv / gax-q5m): #1155cc = (17, 85, 204)
_LINK_BLUE_RGB = (17, 85, 204)


def _is_link_blue(hex_color: str) -> bool:
    """True if hex_color is approximately the link-default blue (#1155cc ± 2/channel).

    Google Docs sets foregroundColor to this value on every linked textRun.
    The ±2 tolerance absorbs float-to-int rounding in the Docs API response.
    """
    if not hex_color or not hex_color.startswith("#") or len(hex_color) != 7:
        return False
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        lr, lg, lb = _LINK_BLUE_RGB
        return abs(r - lr) <= 2 and abs(g - lg) <= 2 and abs(b - lb) <= 2
    except (ValueError, IndexError):
        return False


def _extract_text_style(style_dict: dict) -> TextStyle:
    """Extract a TextStyle from a Docs API textStyle dict.

    Link-implied attributes are suppressed at extraction time (gax-tvv/gax-q5m):
    when a link is present the API always reports underline=True and
    foregroundColor≈#1155cc. Clearing them here ensures that both the base
    IR (from_doc_json) and the local IR (parse_tree) agree after a
    serialize→parse round-trip, preventing spurious updateTextStyle mutations.
    """
    if not style_dict:
        return TextStyle()

    # Known fields we model
    known_keys = {
        "bold", "italic", "strikethrough", "underline",
        "link", "foregroundColor", "backgroundColor",
        "fontSize", "weightedFontFamily", "baselineOffset",
    }

    # Collect raw (unknown) keys
    raw = {k: v for k, v in style_dict.items() if k not in known_keys}

    font_size = None
    fs = style_dict.get("fontSize")
    if fs:
        font_size = fs.get("magnitude")

    font_family = None
    wff = style_dict.get("weightedFontFamily")
    if wff:
        font_family = wff.get("fontFamily")

    baseline = style_dict.get("baselineOffset")
    if baseline == "NONE":
        baseline = None

    url = style_dict.get("link", {}).get("url") if "link" in style_dict else None
    underline = style_dict.get("underline", False)
    fg_color = _color_to_hex(style_dict.get("foregroundColor"))

    # Suppress link-implied attributes at extraction so both diff sides agree.
    if url:
        if underline:
            underline = False
        if fg_color and _is_link_blue(fg_color):
            fg_color = None

    return TextStyle(
        bold=style_dict.get("bold", False),
        italic=style_dict.get("italic", False),
        strikethrough=style_dict.get("strikethrough", False),
        underline=underline,
        url=url,
        foreground_color=fg_color,
        background_color=_color_to_hex(style_dict.get("backgroundColor")),
        font_family=font_family,
        font_size=font_size,
        baseline_offset=baseline,
        raw=raw if raw else None,
    )


# =============================================================================
# Known table-cell paragraph style defaults (gax-24m)
# =============================================================================

# These keys appear verbatim on every table cell paragraph style with their
# default values. Suppressing them in the IR reduces YAML noise significantly
# while the original data is preserved in Table._raw_table for push.
_TABLE_CELL_PARA_DEFAULT_KEYS: frozenset[str] = frozenset({
    "borderBetween", "borderTop", "borderBottom", "borderLeft", "borderRight",
    "keepLinesTogether", "keepWithNext", "avoidWidowAndOrphan",
    "shading", "pageBreakBefore", "spacingMode",
})

# lineSpacing=100 is the table-cell default; suppress it when table_cell=True.
_TABLE_CELL_DEFAULT_LINE_SPACING = 100


def _extract_para_style(
    style_dict: dict,
    table_cell: bool = False,
    list_item: bool = False,
) -> ParagraphStyle:
    """Extract a ParagraphStyle from a Docs API paragraphStyle dict.

    Args:
        style_dict: The raw paragraphStyle dict from the Docs API.
        table_cell: When True, suppress known table-cell default keys from raw
            (borders, shading, keepLines*, etc.) and the default lineSpacing
            of 100. These defaults are preserved in Table._raw_table for push.
        list_item: When True, suppress indentStart and indentFirstLine because
            they are implied by the list nesting depth (gax-tvv). Suppressing
            them here prevents spurious diffs when serialize/parse round-trips
            through YAML (where they are also elided via suppress_indent=True).
    """
    if not style_dict:
        return ParagraphStyle()

    known_keys = {
        "alignment", "namedStyleType", "indentStart", "indentEnd",
        "indentFirstLine", "lineSpacing", "spaceAbove", "spaceBelow",
        "direction", "headingId",
    }

    # For table cells also exclude the known-default noise keys from raw.
    exclude = known_keys | _TABLE_CELL_PARA_DEFAULT_KEYS if table_cell else known_keys
    raw = {k: v for k, v in style_dict.items() if k not in exclude}

    alignment = style_dict.get("alignment")
    if alignment == "START":
        alignment = None  # Default, don't report

    def _mag(key):
        obj = style_dict.get(key)
        if obj and isinstance(obj, dict):
            return obj.get("magnitude")
        return None

    line_spacing = style_dict.get("lineSpacing")
    # Suppress the table-cell default lineSpacing=100 from the IR.
    if table_cell and line_spacing == _TABLE_CELL_DEFAULT_LINE_SPACING:
        line_spacing = None

    return ParagraphStyle(
        alignment=alignment,
        named_style=style_dict.get("namedStyleType"),
        # For list items, indentStart/indentFirstLine are derived from depth;
        # suppress them to prevent spurious diffs during serialize/parse cycles.
        indent_start=None if list_item else _mag("indentStart"),
        indent_end=_mag("indentEnd"),
        indent_first_line=None if list_item else _mag("indentFirstLine"),
        line_spacing=line_spacing,
        space_above=_mag("spaceAbove"),
        space_below=_mag("spaceBelow"),
        raw=raw if raw else None,
    )


def _spans_from_textruns(elements: list[dict]) -> list[Span]:
    """Convert Google Docs textRun elements to enriched Span list."""
    spans: list[Span] = []
    for idx, elem in enumerate(elements):
        tr = elem.get("textRun")
        if not tr:
            # For non-textRun elements, include a placeholder
            continue
        text = tr["content"]
        is_last = idx == len(elements) - 1

        if is_last and text.endswith("\n"):
            text = text[:-1]

        if not text:
            continue

        style = _extract_text_style(tr.get("textStyle", {}))
        spans.append(Span(text=text, style=style))
    return spans


def from_doc_json(
    body_content: list[dict],
    lists: Optional[dict] = None,
) -> list[Block]:
    """Walk Google Docs body content and produce enriched Block list."""
    blocks: list[Block] = []

    for elem in body_content:
        start = elem.get("startIndex", 0)
        end = elem.get("endIndex", 0)
        doc_range = (start, end)

        # Table
        if "table" in elem:
            table_data = elem["table"]
            rows: list[list[list[Span]]] = []
            cell_styles: list[list[ParagraphStyle]] = []
            for row in table_data.get("tableRows", []):
                row_spans: list[list[Span]] = []
                row_styles: list[ParagraphStyle] = []
                for cell in row.get("tableCells", []):
                    cell_spans: list[Span] = []
                    cell_para_style = ParagraphStyle()
                    for ce in cell.get("content", []):
                        if "paragraph" in ce:
                            cell_spans.extend(
                                _spans_from_textruns(ce["paragraph"].get("elements", []))
                            )
                            cell_para_style = _extract_para_style(
                                ce["paragraph"].get("paragraphStyle", {}),
                                table_cell=True,
                            )
                    row_spans.append(cell_spans)
                    row_styles.append(cell_para_style)
                rows.append(row_spans)
                cell_styles.append(row_styles)
            blocks.append(Table(
                doc_range=doc_range,
                rows=rows,
                cell_styles=cell_styles,
                _raw_table=elem,
                raw=None,
            ))
            continue

        if "paragraph" not in elem:
            continue

        para = elem["paragraph"]
        elements = para.get("elements", [])
        spans = _spans_from_textruns(elements)

        if not spans:
            continue

        style = para.get("paragraphStyle", {})
        named_style = style.get("namedStyleType", "NORMAL_TEXT")
        bullet = para.get("bullet")
        # Pass list_item=True when this paragraph is a bullet; suppresses
        # indentStart/indentFirstLine which are implied by depth (gax-tvv).
        para_style = _extract_para_style(style, list_item=(bullet is not None))

        # Heading
        if named_style in HEADING_STYLES:
            blocks.append(
                Heading(
                    doc_range=doc_range,
                    level=HEADING_STYLES[named_style],
                    spans=spans,
                    para_style=para_style,
                )
            )
            continue

        # List item
        if bullet is not None:
            nesting = bullet.get("nestingLevel", 0)
            list_id = bullet.get("listId", "")
            ordered = False
            if lists and list_id in lists:
                nesting_levels = (
                    lists[list_id].get("listProperties", {}).get("nestingLevels", [])
                )
                if nesting < len(nesting_levels):
                    glyph = nesting_levels[nesting].get("glyphType", "")
                    ordered = glyph not in ("", "GLYPH_TYPE_UNSPECIFIED")
            blocks.append(
                ListItem(
                    doc_range=doc_range,
                    spans=spans,
                    ordered=ordered,
                    depth=nesting,
                    para_style=para_style,
                )
            )
            continue

        # Regular paragraph
        blocks.append(Paragraph(doc_range=doc_range, spans=spans, para_style=para_style))

    return blocks
