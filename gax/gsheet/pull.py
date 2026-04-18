"""Pull command - fetch from Google Sheets to local file"""

from pathlib import Path
from .frontmatter import parse_file, write_file
from ..formats import get_format
from .client import GSheetClient


def pull(file_path: Path, client: GSheetClient | None = None) -> int:
    """
    Pull data from Google Sheets to local file.

    Returns number of rows pulled.
    """
    if client is None:
        client = GSheetClient()

    # Parse existing file for config
    config, _ = parse_file(file_path)

    # Fetch from Google Sheets
    df = client.read(config.spreadsheet_id, config.tab, config.range)

    # Convert to format
    fmt = get_format(config.format)
    data = fmt.write(df)

    # Write back to file
    write_file(file_path, config, data)

    return len(df)
