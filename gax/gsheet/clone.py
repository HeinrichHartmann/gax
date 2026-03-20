"""Clone command - fetch all tabs from Google Sheets to multipart file"""

from pathlib import Path
from ..multipart import Section, format_multipart, parse_multipart
from ..formats import get_format
from .client import GSheetClient


def clone_all(
    spreadsheet_id: str,
    url: str,
    fmt: str = "csv",
    client: GSheetClient | None = None,
) -> tuple[str, list[Section]]:
    """
    Clone all tabs from a spreadsheet.

    Returns:
        Tuple of (title, list of Section objects)
    """
    if client is None:
        client = GSheetClient()

    formatter = get_format(fmt)
    info = client.get_spreadsheet_info(spreadsheet_id)
    title = info["title"]
    sections = []

    for idx, tab_info in enumerate(info["tabs"], start=1):
        tab_name = tab_info["title"]
        df = client.read(spreadsheet_id, tab_name)
        data = formatter.write(df)

        section = Section(
            headers={
                "title": title,
                "source": url,
                "section": idx,
                "tab": tab_name,
                "format": fmt,
            },
            content=data,
        )
        sections.append(section)

    return title, sections


def pull_all(
    file_path: Path,
    client: GSheetClient | None = None,
) -> int:
    """
    Pull all tabs from a multipart sheet file.

    Returns number of total rows pulled across all tabs.
    """
    if client is None:
        client = GSheetClient()

    content = file_path.read_text(encoding="utf-8")
    sections = parse_multipart(content)

    if not sections:
        raise ValueError(f"No sections found in {file_path}")

    # Get common info from first section
    first = sections[0]
    source = first.headers.get("source", "")

    # Extract spreadsheet ID from source URL
    import re

    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", source)
    if not match:
        raise ValueError(f"Could not extract spreadsheet ID from source: {source}")
    spreadsheet_id = match.group(1)

    # Pull each tab
    total_rows = 0
    updated_sections = []

    for section in sections:
        tab_name = section.headers.get("tab")
        fmt = section.headers.get("format", "csv")

        if not tab_name:
            raise ValueError(f"Section missing 'tab' header in {file_path}")

        df = client.read(spreadsheet_id, tab_name)
        formatter = get_format(fmt)
        data = formatter.write(df)

        updated_section = Section(
            headers=section.headers,
            content=data,
        )
        updated_sections.append(updated_section)
        total_rows += len(df)

    # Write back to file
    output = format_multipart(updated_sections)
    file_path.write_text(output, encoding="utf-8")

    return total_rows
