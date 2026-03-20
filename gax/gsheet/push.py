"""Push command - write local file to Google Sheets"""

from pathlib import Path
from ..frontmatter import parse_file
from ..formats import get_format
from .client import GSheetClient


def push(file_path: Path, client: GSheetClient | None = None, with_formulas: bool = False) -> int:
    """
    Push data from local file to Google Sheets.

    Returns number of rows pushed.
    """
    if client is None:
        client = GSheetClient()

    # Parse file
    config, data = parse_file(file_path)

    # Parse data to DataFrame
    fmt = get_format(config.format)
    df = fmt.read(data)

    # Write to Google Sheets
    rows = client.write(config.spreadsheet_id, config.tab, df, with_formulas=with_formulas)

    return rows
