"""Markdown table format handler.

Newlines in cells are encoded as <br> on write and decoded back on read.
This allows lossless roundtrips for Google Sheets cells that contain newlines.
"""

import logging
import re
import pandas as pd
from .base import Format

logger = logging.getLogger(__name__)

BR_TAG = "<br>"


def _count_columns(line: str) -> int:
    """Count pipe-delimited columns in a markdown table row."""
    cells = [c.strip() for c in line.split("|")]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return len(cells)


class MarkdownFormat(Format):
    def read(self, content: str) -> pd.DataFrame:
        if not content.strip():
            return pd.DataFrame()

        lines = content.strip().split("\n")
        if len(lines) < 2:
            return pd.DataFrame()

        # Parse header to get expected column count
        header_line = None
        data_lines = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            if re.match(r"^\|?[\s\-:|]+\|?$", line):
                continue
            if header_line is None:
                header_line = line
            else:
                data_lines.append((i + 1, line))  # 1-based line number

        if header_line is None:
            return pd.DataFrame()

        header_cols = _count_columns(header_line)

        # Validate that no data row has fewer columns than the header.
        # Fewer columns indicates corrupted multiline cells (newlines in cells
        # that were not encoded as <br>, causing row splits).
        # More columns are allowed (stacked sub-tables with different widths).
        bad_lines = []
        for lineno, line in data_lines:
            ncols = _count_columns(line)
            if ncols < header_cols:
                bad_lines.append((lineno, ncols, line))

        if bad_lines:
            msg_parts = [
                f"Markdown table has inconsistent column counts "
                f"(header has {header_cols} columns):",
            ]
            for lineno, ncols, line in bad_lines[:5]:
                preview = line[:80] + ("..." if len(line) > 80 else "")
                msg_parts.append(f"  line {lineno}: {ncols} columns: {preview}")
            if len(bad_lines) > 5:
                msg_parts.append(f"  ... and {len(bad_lines) - 5} more")
            msg_parts.append(
                "This usually means cells contain newlines that were not "
                "encoded as <br>. Fix the file or re-pull from the source."
            )
            raise ValueError("\n".join(msg_parts))

        # First pass: parse all rows to find maximum column count
        # This handles files with multiple tables of different widths
        all_rows = []
        max_cols = 0

        for line in lines:
            if not line.strip():
                continue
            # Skip separator rows
            if re.match(r"^\|?[\s\-:|]+\|?$", line):
                continue

            cells = [c.strip() for c in line.split("|")]
            # Remove empty strings from leading/trailing pipes
            if cells and cells[0] == "":
                cells = cells[1:]
            if cells and cells[-1] == "":
                cells = cells[:-1]

            if cells:  # Only add non-empty rows
                all_rows.append(cells)
                max_cols = max(max_cols, len(cells))

        if not all_rows or max_cols == 0:
            return pd.DataFrame()

        # Use first row as headers
        headers = all_rows[0]

        # If first row has fewer columns than max, pad headers
        while len(headers) < max_cols:
            headers.append("")

        # Process data rows (skip first row which is headers)
        rows = []
        for cells in all_rows[1:]:
            # Pad row to match max column count
            while len(cells) < max_cols:
                cells.append("")
            rows.append(cells[:max_cols])

        df = pd.DataFrame(rows, columns=headers)
        df = df.fillna("")

        # Decode <br> back to newlines
        has_br = False
        for i in range(len(df.columns)):
            col_data = df.iloc[:, i]
            if col_data.str.contains(BR_TAG, regex=False).any():
                has_br = True
                df.iloc[:, i] = col_data.str.replace(BR_TAG, "\n", regex=False)
        if has_br:
            logger.info("Decoded <br> tags back to newlines in cell values")

        return df

    def write(self, df: pd.DataFrame) -> str:
        # Check for literal <br> in source data — would be ambiguous after encoding
        for col in df.columns:
            if df[col].astype(str).str.contains(BR_TAG, regex=False).any():
                raise ValueError(
                    f"Cell in column '{col}' contains a literal '<br>' which "
                    "conflicts with the newline encoding used by markdown format. "
                    "Use a different format (csv, tsv, json) for this data."
                )

        # Encode newlines as <br>
        has_newlines = False
        encoded = df.copy()
        for col in encoded.columns:
            col_str = encoded[col].astype(str)
            if col_str.str.contains("\n", regex=False).any():
                has_newlines = True
                encoded[col] = col_str.str.replace("\n", BR_TAG, regex=False)

        if has_newlines:
            logger.warning(
                "Cell values contain newlines — encoded as <br> in markdown output"
            )

        lines = []

        # Header row
        headers = [str(c) for c in encoded.columns]
        lines.append("| " + " | ".join(headers) + " |")

        # Separator row
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

        # Data rows
        for _, row in encoded.iterrows():
            cells = [str(v) for v in row.values]
            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines) + "\n"
