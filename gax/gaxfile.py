"""Shared YAML-frontmatter parser for .gax.md files.

All .gax.md files share a common structure:

    # optional comment lines
    ---
    type: gax/something
    key: value
    ---
    body content here

This module provides parse/format for that structure. For multi-section
documents (mail threads, docs with tabs), see multipart.py instead.
"""

from pathlib import Path

import yaml


def parse(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter and body from a .gax.md file.

    Handles optional leading comment lines (# ...).

    Returns:
        (headers_dict, body_str) where headers are parsed via yaml.safe_load.

    Raises:
        ValueError: if no valid frontmatter found.
    """
    # Skip leading comment lines
    lines = content.split("\n")
    start = 0
    while start < len(lines) and lines[start].startswith("#"):
        start += 1

    rest = "\n".join(lines[start:])

    if not rest.startswith("---\n") and not rest.startswith("---\r\n"):
        raise ValueError("File must start with YAML frontmatter (---)")

    # Find closing ---
    end = rest.find("\n---\n", 4)
    if end == -1:
        # Check if --- is at end of file
        if rest.endswith("\n---"):
            end = len(rest) - 3
        else:
            raise ValueError("No closing --- found for frontmatter")

    header_text = rest[4:end]
    body = rest[end + 5:]  # skip \n---\n

    headers = yaml.safe_load(header_text) or {}

    return headers, body


def format(headers: dict, body: str) -> str:
    """Format headers and body as a .gax.md file.

    Produces:
        ---
        key: value
        ---
        body
    """
    yaml_str = yaml.dump(
        headers, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    return f"---\n{yaml_str}---\n{body}"


def read_type(path: Path) -> str | None:
    """Fast extraction of the type: field without full YAML parse.

    Used by Resource.from_file() dispatch. Scans the first 20 lines
    for a type: field, handling both simple YAML and --- delimited formats.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    for line in content.split("\n")[:20]:
        if line == "---":
            continue
        if line.startswith("type:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("#"):
            continue
        # Stop at empty line or second --- (end of header)
        if not line or (line.startswith("---") and line.strip() == "---"):
            break

    return None
