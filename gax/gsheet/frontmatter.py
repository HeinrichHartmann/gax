"""YAML frontmatter parsing for .sheet.gax.md files"""

from dataclasses import dataclass
from pathlib import Path

from .. import gaxfile


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
    content = path.read_text()
    return parse_content(content)


def parse_content(content: str) -> tuple[SheetConfig, str]:
    """Parse content string into config and data sections."""
    headers, data_str = gaxfile.parse(content)

    config = SheetConfig(
        spreadsheet_id=headers["spreadsheet_id"],
        tab=headers["tab"],
        format=headers.get("format", "csv"),
        url=headers.get("url"),
        range=headers.get("range"),
        separator=headers.get("separator"),
    )

    return config, data_str


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

    return gaxfile.format(headers, data)
