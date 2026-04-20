"""YAML frontmatter parsing for .sheet.gax.md files"""

from dataclasses import dataclass
from pathlib import Path

from ..gaxfile import GaxFile, parse as gaxfile_parse, format as gaxfile_format


@dataclass
class SheetConfig:
    spreadsheet_id: str
    tab: str
    format: str
    url: str | None = None
    range: str | None = None
    separator: str | None = None


def parse_file(path: Path) -> tuple[SheetConfig, str]:
    """Parse a .sheet.gax.md file into config and data sections."""
    gf = GaxFile.from_path(path, multipart=False)
    return _headers_to_config(gf.headers), gf.body


def parse_content(content: str) -> tuple[SheetConfig, str]:
    """Parse content string into config and data sections."""
    headers, data_str = gaxfile_parse(content)
    return _headers_to_config(headers), data_str


def _headers_to_config(headers: dict) -> SheetConfig:
    return SheetConfig(
        spreadsheet_id=headers["spreadsheet_id"],
        tab=headers["tab"],
        format=headers.get("format", "csv"),
        url=headers.get("url"),
        range=headers.get("range"),
        separator=headers.get("separator"),
    )


def write_file(path: Path, config: SheetConfig, data: str) -> None:
    """Write config and data to a .sheet.gax.md file."""
    content = format_content(config, data)
    path.write_text(content)


def format_content(config: SheetConfig, data: str) -> str:
    """Format config and data into file content."""
    headers = {
        "spreadsheet_id": config.spreadsheet_id,
        "tab": config.tab,
        "format": config.format,
    }
    if config.url:
        headers["url"] = config.url
    if config.range:
        headers["range"] = config.range
    if config.separator:
        headers["separator"] = config.separator

    return gaxfile_format(headers, data)
