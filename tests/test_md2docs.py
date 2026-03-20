"""Tests for markdown to Google Docs conversion."""

from gax.md2docs import parse_markdown, parse_inline, Heading, Paragraph, Table


class TestParseInline:
    def test_plain_text(self):
        result = parse_inline("hello world")
        assert len(result) == 1
        assert result[0].text == "hello world"
        assert not result[0].bold
        assert not result[0].italic

    def test_bold(self):
        result = parse_inline("hello **world**")
        assert len(result) == 2
        assert result[0].text == "hello "
        assert result[1].text == "world"
        assert result[1].bold

    def test_italic(self):
        result = parse_inline("hello *world*")
        assert len(result) == 2
        assert result[1].text == "world"
        assert result[1].italic

    def test_bold_and_italic(self):
        result = parse_inline("***both***")
        assert result[0].bold
        assert result[0].italic


class TestParseMarkdown:
    def test_heading(self):
        nodes = parse_markdown("# Title")
        assert len(nodes) == 1
        assert isinstance(nodes[0], Heading)
        assert nodes[0].level == 1
        assert nodes[0].text == "Title"

    def test_multiple_headings(self):
        nodes = parse_markdown("# H1\n## H2\n### H3")
        assert len(nodes) == 3
        assert nodes[0].level == 1
        assert nodes[1].level == 2
        assert nodes[2].level == 3

    def test_paragraph(self):
        nodes = parse_markdown("Hello world")
        assert len(nodes) == 1
        assert isinstance(nodes[0], Paragraph)
        assert nodes[0].children[0].text == "Hello world"

    def test_table(self):
        md = """| A | B |
|---|---|
| 1 | 2 |
| 3 | 4 |"""
        nodes = parse_markdown(md)
        assert len(nodes) == 1
        assert isinstance(nodes[0], Table)
        assert nodes[0].rows == [["A", "B"], ["1", "2"], ["3", "4"]]

    def test_mixed_content(self):
        md = """# Title

Some **bold** text.

| Col1 | Col2 |
|------|------|
| a    | b    |
"""
        nodes = parse_markdown(md)
        assert isinstance(nodes[0], Heading)
        assert isinstance(nodes[1], Paragraph)
        assert isinstance(nodes[2], Table)
