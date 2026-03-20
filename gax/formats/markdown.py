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

        # Parse header row
        headers = [c.strip() for c in lines[0].split("|")]
        # Remove empty strings from leading/trailing pipes
        if headers and headers[0] == "":
            headers = headers[1:]
        if headers and headers[-1] == "":
            headers = headers[:-1]

        # Skip separator row (|---|---|)
        data_start = 1
        if len(lines) > 1 and re.match(r"^\|?[\s\-:|]+\|?$", lines[1]):
            data_start = 2

        # Parse data rows
        rows = []
        for line in lines[data_start:]:
            if not line.strip():
                continue
            cells = [c.strip() for c in line.split("|")]
            if cells and cells[0] == "":
                cells = cells[1:]
            if cells and cells[-1] == "":
                cells = cells[:-1]
            # Pad row to match header length
            while len(cells) < len(headers):
                cells.append("")
            rows.append(cells[: len(headers)])

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
