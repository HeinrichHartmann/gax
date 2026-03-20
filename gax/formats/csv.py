"""CSV, TSV, PSV format handlers"""

import io
import pandas as pd
from .base import Format


class CSVFormat(Format):
    def __init__(self, separator: str = ","):
        self.separator = separator

    def read(self, content: str) -> pd.DataFrame:
        if not content.strip():
            return pd.DataFrame()
        df = pd.read_csv(io.StringIO(content), sep=self.separator, skipinitialspace=True)
        # Replace "Unnamed: X" column names with empty string
        df.columns = [(c.strip() if isinstance(c, str) else c) for c in df.columns]
        df.columns = [("" if str(c).startswith("Unnamed:") else c) for c in df.columns]
        # Replace NaN with empty string, strip whitespace from string values
        df = df.fillna("")
        df = df.apply(lambda col: col.str.strip() if col.dtype == "object" else col)
        return df

    def write(self, df: pd.DataFrame) -> str:
        return df.to_csv(index=False, sep=self.separator)


class TSVFormat(CSVFormat):
    def __init__(self):
        super().__init__(separator="\t")


class PSVFormat(CSVFormat):
    def __init__(self):
        super().__init__(separator="|")
