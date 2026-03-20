"""JSON and JSONL format handlers"""

import json
import pandas as pd
from .base import Format


class JSONFormat(Format):
    def read(self, content: str) -> pd.DataFrame:
        if not content.strip():
            return pd.DataFrame()
        data = json.loads(content)
        return pd.DataFrame(data)

    def write(self, df: pd.DataFrame) -> str:
        return df.to_json(orient="records", indent=2)


class JSONLFormat(Format):
    def read(self, content: str) -> pd.DataFrame:
        if not content.strip():
            return pd.DataFrame()
        records = [json.loads(line) for line in content.strip().split("\n") if line]
        return pd.DataFrame(records)

    def write(self, df: pd.DataFrame) -> str:
        lines = [json.dumps(record) for record in df.to_dict("records")]
        return "\n".join(lines) + "\n"
