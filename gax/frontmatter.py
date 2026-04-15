"""YAML frontmatter parsing for .sheet.gax.md files"""

import yaml
from dataclasses import dataclass
from pathlib import Path


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
    if not content.startswith("---"):
        raise ValueError("File must start with YAML frontmatter (---)")

    # Find the end of frontmatter
    end_idx = content.find("\n---", 3)
    if end_idx == -1:
        raise ValueError("No closing --- found for frontmatter")

    frontmatter_str = content[4:end_idx]  # Skip initial ---
    data_str = content[end_idx + 4 :].lstrip("\n")  # Skip closing --- and newline

    # Parse YAML
    frontmatter = yaml.safe_load(frontmatter_str)

    config = SheetConfig(
        spreadsheet_id=frontmatter["spreadsheet_id"],
        tab=frontmatter["tab"],
        format=frontmatter.get("format", "csv"),
        url=frontmatter.get("url"),
        range=frontmatter.get("range"),
        separator=frontmatter.get("separator"),
    )

    return config, data_str


def write_file(path: Path, config: SheetConfig, data: str) -> None:
    """Write config and data to a .sheet.gax.md file."""
    content = format_content(config, data)
    path.write_text(content)


def format_content(config: SheetConfig, data: str) -> str:
    """Format config and data into file content."""
    frontmatter = {
        "spreadsheet_id": config.spreadsheet_id,
        "tab": config.tab,
        "format": config.format,
    }
    if config.url:
        frontmatter["url"] = config.url
    if config.range:
        frontmatter["range"] = config.range
    if config.separator:
        frontmatter["separator"] = config.separator

    yaml_str = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)
    return f"---\n{yaml_str}---\n{data}"
