"""Markdown table format handler"""

import re
import pandas as pd
from .base import Format


class MarkdownFormat(Format):
    def read(self, content: str) -> pd.DataFrame:
        if not content.strip():
            return pd.DataFrame()

        lines = content.strip().split("\n")
        if len(lines) < 2:
            return pd.DataFrame()

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
        return df

    def write(self, df: pd.DataFrame) -> str:
        lines = []

        # Header row
        headers = [str(c) for c in df.columns]
        lines.append("| " + " | ".join(headers) + " |")

        # Separator row
        lines.append("| " + " | ".join(["---"] * len(headers)) + " |")

        # Data rows
        for _, row in df.iterrows():
            cells = [str(v) for v in row.values]
            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines) + "\n"
