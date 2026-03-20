"""Format readers and writers"""

from .base import Format
from .csv import CSVFormat, TSVFormat, PSVFormat
from .json import JSONFormat, JSONLFormat
from .markdown import MarkdownFormat

FORMATS: dict[str, Format] = {
    "csv": CSVFormat(),
    "tsv": TSVFormat(),
    "psv": PSVFormat(),
    "json": JSONFormat(),
    "jsonl": JSONLFormat(),
    "markdown": MarkdownFormat(),
    "md": MarkdownFormat(),
}


def get_format(name: str) -> Format:
    """Get a format by name."""
    if name not in FORMATS:
        raise ValueError(f"Unknown format: {name}. Available: {list(FORMATS.keys())}")
    return FORMATS[name]
