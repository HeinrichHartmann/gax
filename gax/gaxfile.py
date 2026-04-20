"""Shared file format for .gax.md files.

Two formats:

  Single-section (contacts, labels, filters, forms, sheets, mailbox):

      # optional comments
      ---
      type: gax/something
      key: value
      ---
      body content

  Multipart (mail threads, drafts, docs):

      ---
      type: gax/mail
      thread_id: abc123
      ---
      first section body
      ---
      type: gax/mail
      from: alice@example.com
      ---
      second section body

Read path: GaxFile.from_path(path) -> .headers, .body, .sections
Write path: format(), format_section(), format_multipart()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# =============================================================================
# Data types
# =============================================================================


@dataclass
class Section:
    """A section of a multipart document."""

    headers: dict[str, Any] = field(default_factory=dict)
    content: str = ""


# =============================================================================
# GaxFile — read path
# =============================================================================


class GaxFile:
    """A parsed .gax.md file (single or multipart)."""

    def __init__(self, sections: list[Section]):
        self.sections = sections

    @classmethod
    def from_path(cls, path: Path) -> GaxFile:
        """Read and parse a .gax.md file.

        Raises ValueError if the file contains no valid sections.
        """
        content = path.read_text(encoding="utf-8")
        return cls(parse_multipart(content))

    @property
    def headers(self) -> dict[str, Any]:
        """Headers of the first (or only) section."""
        return self.sections[0].headers

    @property
    def body(self) -> str:
        """Body content of the first (or only) section."""
        return self.sections[0].content


# =============================================================================
# Single-section parsing (simple key: value frontmatter)
# =============================================================================


def parse(content: str) -> tuple[dict[str, str], str]:
    """Parse YAML frontmatter and body from a single-section .gax.md file.

    Handles optional leading comment lines (# ...) and CRLF line endings.

    All header values are returned as strings to preserve round-trip
    fidelity (yaml.safe_load would coerce timestamps into datetime objects).

    Returns:
        (headers_dict, body_str)

    Raises:
        ValueError: if no valid frontmatter found.
    """
    # Normalize CRLF to LF
    content = content.replace("\r\n", "\n")

    # Skip leading comment lines
    lines = content.split("\n")
    start = 0
    while start < len(lines) and lines[start].startswith("#"):
        start += 1

    rest = "\n".join(lines[start:])

    if not rest.startswith("---\n"):
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

    # Parse as simple key: value pairs to preserve string types.
    # yaml.safe_load would coerce "2026-01-01T00:00:00Z" into datetime.
    headers: dict[str, str] = {}
    for line in header_text.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()

    return headers, body


def format(headers: dict, body: str) -> str:
    """Format headers and body as a single-section .gax.md file.

    Uses simple key: value formatting (not yaml.dump) to preserve
    round-trip fidelity with parse().
    """
    lines = ["---"]
    for key, value in headers.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


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


# =============================================================================
# Multipart parsing (multiple sections with content-length support)
# =============================================================================


def needs_content_length(content: str) -> bool:
    """Check if content needs content-length header for safe parsing.

    Content containing '---' on its own line could be misinterpreted
    as a section boundary, so we use content-length for byte-accurate parsing.
    """
    return (
        "\n---\n" in content or content.startswith("---\n") or content.endswith("\n---")
    )


def format_section(headers: dict[str, Any], content: str) -> str:
    """Format a single section as YAML header + body.

    Args:
        headers: Dict of header key-value pairs (written in order)
        content: Markdown body content

    Returns:
        Formatted section string
    """
    lines = ["---"]

    for key, value in headers.items():
        if key == "content-length":
            continue  # We'll compute this ourselves if needed
        if isinstance(value, list):
            # Handle list values (e.g., attachments)
            lines.append(f"{key}:")
            for item in value:
                if isinstance(item, dict):
                    first = True
                    for k, v in item.items():
                        prefix = "  - " if first else "    "
                        lines.append(f"{prefix}{k}: {v}")
                        first = False
                else:
                    lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")

    # Ensure content ends with newline for proper section separation
    if content and not content.endswith("\n"):
        content = content + "\n"

    if needs_content_length(content):
        content_bytes = content.encode("utf-8")
        lines.append(f"content-length: {len(content_bytes)}")

    lines.append("---")
    return "\n".join(lines) + "\n" + content


def format_multipart(sections: list[Section]) -> str:
    """Assemble sections into multipart markdown string."""
    return "".join(format_section(s.headers, s.content) for s in sections)


def parse_header(text: str) -> dict[str, Any]:
    """Parse simple YAML-like header into dict.

    Handles:
    - Simple key: value pairs
    - content-length as int
    - Nested list items (attachments)
    """
    result: dict[str, Any] = {}
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        if ":" not in line:
            i += 1
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()

        if key == "content-length":
            result[key] = int(value)
        elif value == "" and i + 1 < len(lines) and lines[i + 1].startswith("  "):
            # List value - check next lines start with indent
            items = []
            i += 1
            while i < len(lines) and lines[i].startswith("  "):
                item_line = lines[i]
                if item_line.strip().startswith("- "):
                    # New list item
                    item_content = item_line.strip()[2:]  # Remove "- "
                    if ":" in item_content:
                        # Dict item
                        item_key, item_val = item_content.split(":", 1)
                        current_item = {item_key.strip(): item_val.strip()}
                        items.append(current_item)
                    else:
                        items.append(item_content)
                elif ":" in item_line and items and isinstance(items[-1], dict):
                    # Continuation of dict item
                    item_key, item_val = item_line.strip().split(":", 1)
                    items[-1][item_key.strip()] = item_val.strip()
                i += 1
            result[key] = items
            continue
        else:
            result[key] = value

        i += 1

    return result


def parse_multipart(text: str) -> list[Section]:
    """Parse multipart markdown into sections.

    Handles:
    - Multiple sections with YAML headers
    - content-length for precise byte counting
    - Sections without content-length (scan for next ---)

    Raises ValueError if no valid sections are found.
    """
    sections: list[Section] = []
    pos = 0
    text_bytes = text.encode("utf-8")

    while pos < len(text):
        # Find header start
        if not text[pos:].startswith("---\n"):
            # Skip any leading content before first ---
            next_header = text.find("\n---\n", pos)
            if next_header == -1:
                break
            pos = next_header + 1
            continue

        pos += 4  # skip '---\n'

        # Parse header until ---
        header_end = text.find("\n---\n", pos)
        if header_end == -1:
            break

        header_text = text[pos:header_end]
        headers = parse_header(header_text)
        pos = header_end + 5  # skip '\n---\n'

        # Read body
        content_length = headers.get("content-length")
        if content_length is not None:
            # Read exactly content_length bytes
            byte_pos = len(text[:pos].encode("utf-8"))
            content_bytes = text_bytes[byte_pos : byte_pos + content_length]
            content = content_bytes.decode("utf-8")
            pos += len(content)
        else:
            # Scan for next section or EOF
            next_section = text.find("\n---\n", pos)
            if next_section == -1:
                content = text[pos:]
                pos = len(text)
            else:
                content = text[pos : next_section + 1]  # include trailing \n
                pos = next_section + 1

        sections.append(
            Section(
                headers=headers,
                content=content.strip(),
            )
        )

    if not sections:
        raise ValueError("No valid sections found (expected ---\\nheaders\\n---\\nbody)")

    return sections
