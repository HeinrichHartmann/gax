"""Multipart YAML-markdown format parser and formatter.

Implements the multipart format from ADR 002:
- Multiple sections separated by YAML headers
- Each section has metadata header + markdown body
- Optional content-length for bodies containing '---'
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Section:
    """A section of a multipart document."""

    headers: dict[str, Any] = field(default_factory=dict)
    content: str = ""


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
    """
    sections = []
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

    return sections
