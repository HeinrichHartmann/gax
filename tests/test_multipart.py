"""Tests for multipart YAML-markdown format parsing and formatting."""

from gax.multipart import (
    Section,
    needs_content_length,
    format_section,
    format_multipart,
    parse_header,
    parse_multipart,
)


class TestNeedsContentLength:
    """Tests for content-length detection."""

    def test_plain_content(self):
        """Plain content without --- doesn't need content-length."""
        assert needs_content_length("Hello world") is False
        assert needs_content_length("Line 1\nLine 2\nLine 3") is False

    def test_dashes_in_middle(self):
        """Content with --- in middle needs content-length."""
        assert needs_content_length("Before\n---\nAfter") is True

    def test_dashes_at_start(self):
        """Content starting with --- needs content-length."""
        assert needs_content_length("---\nSome content") is True

    def test_dashes_at_end(self):
        """Content ending with --- needs content-length."""
        assert needs_content_length("Some content\n---") is True

    def test_dashes_inline(self):
        """Inline dashes (not on own line) don't need content-length."""
        assert needs_content_length("Use --- for emphasis") is False
        assert needs_content_length("a---b") is False

    def test_yaml_frontmatter_in_content(self):
        """YAML frontmatter embedded in content needs content-length."""
        content = """Here's an example:
---
title: Example
---
And more text."""
        assert needs_content_length(content) is True


class TestParseHeader:
    """Tests for YAML header parsing."""

    def test_simple_headers(self):
        """Parse simple key: value pairs."""
        text = "title: My Document\nsource: https://example.com\nsection: 1"
        result = parse_header(text)

        assert result["title"] == "My Document"
        assert result["source"] == "https://example.com"
        assert result["section"] == "1"

    def test_content_length_as_int(self):
        """content-length should be parsed as integer."""
        text = "title: Test\ncontent-length: 42"
        result = parse_header(text)

        assert result["content-length"] == 42
        assert isinstance(result["content-length"], int)

    def test_colon_in_value(self):
        """Values containing colons should be preserved."""
        text = "source: https://docs.google.com/document/d/abc123/edit\ntime: 2025-03-10T09:30:00Z"
        result = parse_header(text)

        assert result["source"] == "https://docs.google.com/document/d/abc123/edit"
        assert result["time"] == "2025-03-10T09:30:00Z"

    def test_list_values(self):
        """Parse list values (like attachments)."""
        text = """title: Test
attachments:
  - name: file.pdf
    size: 1024
    url: file:///path/to/file
  - name: image.png
    size: 2048
    url: file:///path/to/image"""
        result = parse_header(text)

        assert "attachments" in result
        assert len(result["attachments"]) == 2
        assert result["attachments"][0]["name"] == "file.pdf"
        assert result["attachments"][0]["size"] == "1024"
        assert result["attachments"][1]["name"] == "image.png"

    def test_empty_value(self):
        """Empty values should be empty string."""
        text = "title: \nsource: https://example.com"
        result = parse_header(text)

        assert result["title"] == ""
        assert result["source"] == "https://example.com"

    def test_whitespace_handling(self):
        """Whitespace should be stripped from keys and values."""
        text = "  title  :  My Title  \n  source  :  https://example.com  "
        result = parse_header(text)

        assert result["title"] == "My Title"
        assert result["source"] == "https://example.com"


class TestFormatSection:
    """Tests for section formatting."""

    def test_simple_section(self):
        """Format a simple section."""
        headers = {"title": "Test", "section": 1}
        content = "Hello world"

        result = format_section(headers, content)

        assert result.startswith("---\n")
        assert "title: Test" in result
        assert "section: 1" in result
        assert result.endswith("Hello world\n")  # Trailing newline added
        assert "content-length" not in result

    def test_section_with_dashes_in_content(self):
        """Section with --- in content gets content-length."""
        headers = {"title": "Test"}
        content = "Before\n---\nAfter"

        result = format_section(headers, content)

        assert "content-length:" in result
        # Content-length includes the trailing newline added by format_section
        expected_length = len((content + "\n").encode("utf-8"))
        assert f"content-length: {expected_length}" in result

    def test_section_with_list_headers(self):
        """Format section with list values in headers."""
        headers = {
            "title": "Test",
            "attachments": [
                {"name": "file.pdf", "size": 1024},
                {"name": "image.png", "size": 2048},
            ],
        }
        content = "Content here"

        result = format_section(headers, content)

        assert "attachments:" in result
        assert "  - name: file.pdf" in result
        assert "    size: 1024" in result
        assert "  - name: image.png" in result

    def test_header_order_preserved(self):
        """Headers should be written in dict order."""
        headers = {"title": "First", "source": "Second", "time": "Third"}
        content = "Content"

        result = format_section(headers, content)
        lines = result.split("\n")

        title_idx = next(i for i, line in enumerate(lines) if "title:" in line)
        source_idx = next(i for i, line in enumerate(lines) if "source:" in line)
        time_idx = next(i for i, line in enumerate(lines) if "time:" in line)

        assert title_idx < source_idx < time_idx

    def test_unicode_content(self):
        """Unicode content should be handled correctly."""
        headers = {"title": "Test"}
        content = "Hello 世界 🌍"

        result = format_section(headers, content)

        assert "Hello 世界 🌍" in result


class TestFormatMultipart:
    """Tests for multipart formatting."""

    def test_single_section(self):
        """Format document with single section."""
        sections = [Section(headers={"title": "Test", "section": 1}, content="Hello")]

        result = format_multipart(sections)

        assert result.count("---\n") == 2  # One section = 2 delimiters

    def test_multiple_sections(self):
        """Format document with multiple sections."""
        sections = [
            Section(headers={"title": "Doc", "section": 1}, content="First"),
            Section(headers={"title": "Doc", "section": 2}, content="Second"),
            Section(headers={"title": "Doc", "section": 3}, content="Third"),
        ]

        result = format_multipart(sections)

        assert result.count("section: 1") == 1
        assert result.count("section: 2") == 1
        assert result.count("section: 3") == 1
        assert "First" in result
        assert "Second" in result
        assert "Third" in result


class TestParseMultipart:
    """Tests for multipart parsing."""

    def test_single_section(self):
        """Parse document with single section."""
        text = """---
title: Test Document
section: 1
---
Hello world"""

        sections = parse_multipart(text)

        assert len(sections) == 1
        assert sections[0].headers["title"] == "Test Document"
        assert sections[0].headers["section"] == "1"
        assert sections[0].content == "Hello world"

    def test_multiple_sections(self):
        """Parse document with multiple sections."""
        text = """---
title: Doc
section: 1
---
First section content
---
title: Doc
section: 2
---
Second section content"""

        sections = parse_multipart(text)

        assert len(sections) == 2
        assert sections[0].headers["section"] == "1"
        assert "First section" in sections[0].content
        assert sections[1].headers["section"] == "2"
        assert "Second section" in sections[1].content

    def test_content_length_parsing(self):
        """Parse section with content-length for exact byte reading."""
        content_with_dashes = "Before\n---\nAfter"
        content_bytes = len(content_with_dashes.encode("utf-8"))

        text = f"""---
title: Test
content-length: {content_bytes}
---
{content_with_dashes}"""

        sections = parse_multipart(text)

        assert len(sections) == 1
        assert sections[0].content == content_with_dashes

    def test_content_length_with_following_section(self):
        """Content-length allows --- in content without breaking parsing."""
        dangerous_content = "---\nThis looks like YAML\n---"
        content_bytes = len(dangerous_content.encode("utf-8"))

        text = f"""---
title: First
content-length: {content_bytes}
---
{dangerous_content}
---
title: Second
section: 2
---
Normal content"""

        sections = parse_multipart(text)

        assert len(sections) == 2
        assert sections[0].content == dangerous_content
        assert sections[1].headers["title"] == "Second"

    def test_unicode_content_length(self):
        """Content-length works correctly with multi-byte UTF-8."""
        # 世界 is 6 bytes in UTF-8, 🌍 is 4 bytes
        unicode_content = "Hello 世界 🌍\n---\nMore"
        content_bytes = len(unicode_content.encode("utf-8"))

        text = f"""---
title: Unicode Test
content-length: {content_bytes}
---
{unicode_content}"""

        sections = parse_multipart(text)

        assert len(sections) == 1
        assert sections[0].content == unicode_content

    def test_empty_content(self):
        """Parse section with empty content."""
        text = """---
title: Empty
section: 1
---
"""

        sections = parse_multipart(text)

        assert len(sections) == 1
        assert sections[0].content == ""

    def test_trailing_newlines(self):
        """Trailing newlines should be stripped from content."""
        text = """---
title: Test
---
Content here


"""

        sections = parse_multipart(text)

        assert sections[0].content == "Content here"


class TestRoundTrip:
    """Tests for format -> parse round-trip consistency."""

    def test_simple_roundtrip(self):
        """Simple content should round-trip correctly."""
        original = [
            Section(
                headers={
                    "title": "Test",
                    "source": "https://example.com",
                    "section": 1,
                },
                content="Hello world",
            )
        ]

        formatted = format_multipart(original)
        parsed = parse_multipart(formatted)

        assert len(parsed) == 1
        assert parsed[0].headers["title"] == "Test"
        assert parsed[0].headers["source"] == "https://example.com"
        assert parsed[0].content == "Hello world"

    def test_dangerous_content_roundtrip(self):
        """Content with --- should round-trip correctly."""
        dangerous = "Start\n---\nMiddle\n---\nEnd"
        original = [Section(headers={"title": "Dangerous"}, content=dangerous)]

        formatted = format_multipart(original)
        parsed = parse_multipart(formatted)

        assert len(parsed) == 1
        assert parsed[0].content == dangerous

    def test_multi_section_roundtrip(self):
        """Multiple sections should round-trip correctly."""
        original = [
            Section(headers={"title": "Doc", "section": 1}, content="First"),
            Section(
                headers={"title": "Doc", "section": 2}, content="Second\n---\nDanger"
            ),
            Section(headers={"title": "Doc", "section": 3}, content="Third"),
        ]

        formatted = format_multipart(original)
        parsed = parse_multipart(formatted)

        assert len(parsed) == 3
        assert parsed[0].content == "First"
        assert parsed[1].content == "Second\n---\nDanger"
        assert parsed[2].content == "Third"

    def test_unicode_roundtrip(self):
        """Unicode content should round-trip correctly."""
        original = [
            Section(
                headers={"title": "日本語タイトル"},
                content="Content: 你好世界 🎉\n---\nMore unicode: café",
            )
        ]

        formatted = format_multipart(original)
        parsed = parse_multipart(formatted)

        assert len(parsed) == 1
        assert "你好世界" in parsed[0].content
        assert "café" in parsed[0].content

    def test_attachments_roundtrip(self):
        """Sections with attachment lists should round-trip."""
        original = [
            Section(
                headers={
                    "title": "Email",
                    "attachments": [
                        {"name": "doc.pdf", "size": "1024", "url": "file:///path"},
                    ],
                },
                content="Email body",
            )
        ]

        formatted = format_multipart(original)
        parsed = parse_multipart(formatted)

        assert len(parsed) == 1
        assert "attachments" in parsed[0].headers
        assert parsed[0].headers["attachments"][0]["name"] == "doc.pdf"
