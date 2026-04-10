"""Tests for markdown to Google Docs conversion."""

from gax.md2docs import parse_markdown, parse_inline, extract_tables, Heading, Paragraph, Table, ListItem, CodeBlock


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

    def test_table_separator_minimal_dashes(self):
        """Issue #14: |---|---| separator should be skipped."""
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        nodes = parse_markdown(md)
        assert len(nodes) == 1
        assert isinstance(nodes[0], Table)
        assert nodes[0].rows == [["A", "B"], ["1", "2"]]

    def test_table_separator_colon_dashes(self):
        """Issue #14: | :---- | separator should be skipped."""
        md = "| A | B |\n| :---- | :---- |\n| 1 | 2 |"
        nodes = parse_markdown(md)
        assert len(nodes) == 1
        assert isinstance(nodes[0], Table)
        assert nodes[0].rows == [["A", "B"], ["1", "2"]]

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


    def test_unordered_list(self):
        md = "- First item\n- Second item\n- Third item"
        nodes = parse_markdown(md)
        assert len(nodes) == 3
        assert all(isinstance(n, ListItem) for n in nodes)
        assert not nodes[0].ordered
        assert nodes[0].children[0].text == "First item"

    def test_ordered_list(self):
        md = "1. First\n2. Second\n3. Third"
        nodes = parse_markdown(md)
        assert len(nodes) == 3
        assert all(isinstance(n, ListItem) for n in nodes)
        assert nodes[0].ordered
        assert nodes[0].children[0].text == "First"

    def test_list_with_inline_formatting(self):
        md = "- **Bold item** with text\n- *Italic item*"
        nodes = parse_markdown(md)
        assert len(nodes) == 2
        assert nodes[0].children[0].text == "Bold item"
        assert nodes[0].children[0].bold

    def test_code_block(self):
        md = "```\nsome code\nmore code\n```"
        nodes = parse_markdown(md)
        assert len(nodes) == 1
        assert isinstance(nodes[0], CodeBlock)
        assert nodes[0].text == "some code\nmore code"

    def test_code_block_with_language(self):
        md = "```python\nprint('hello')\n```"
        nodes = parse_markdown(md)
        assert len(nodes) == 1
        assert isinstance(nodes[0], CodeBlock)
        assert nodes[0].text == "print('hello')"


class TestExtractTables:
    def test_no_tables(self):
        assert extract_tables("# Just a heading\nSome text") == []

    def test_single_table(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        tables = extract_tables(md)
        assert len(tables) == 1
        assert tables[0] == [["A", "B"], ["1", "2"]]

    def test_multiple_tables(self):
        md = "| A |\n|---|\n| 1 |\n\n# Break\n\n| X |\n|---|\n| Y |"
        tables = extract_tables(md)
        assert len(tables) == 2
        assert tables[0] == [["A"], ["1"]]
        assert tables[1] == [["X"], ["Y"]]
