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

# MIME type mappings (HTTP-style content-type)
MIME_TYPES: dict[str, str] = {
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "psv": "text/csv",  # No standard MIME for PSV, use CSV
    "json": "application/json",
    "jsonl": "application/json",
    "markdown": "text/markdown",
    "md": "text/markdown",
    "yaml": "application/yaml",
}


def get_format(name: str) -> Format:
    """Get a format by name."""
    if name not in FORMATS:
        raise ValueError(f"Unknown format: {name}. Available: {list(FORMATS.keys())}")
    return FORMATS[name]


def get_content_type(format_name: str) -> str:
    """Get MIME content-type for a format name."""
    return MIME_TYPES.get(format_name, "text/plain")
