"""Tests for markdown to Google Docs conversion."""

from gax.md2docs import parse_markdown, check_unsupported


class TestCheckUnsupported:
    def test_clean_doc_no_warnings(self):
        nodes = parse_markdown("# Title\n\nSome **bold** text.\n\n- Item 1\n- Item 2\n")
        assert check_unsupported(nodes) == []

    def test_nested_lists_warn(self):
        nodes = parse_markdown("- Top\n    - Nested\n- Other\n")
        warnings = check_unsupported(nodes)
        assert len(warnings) == 1
        assert warnings[0].feature == "nested lists"
        assert warnings[0].reason == "api_limitation"

    def test_code_block_warns(self):
        nodes = parse_markdown("# Title\n\n```\ncode\n```\n")
        warnings = check_unsupported(nodes)
        assert len(warnings) == 1
        assert warnings[0].feature == "code blocks"
        assert warnings[0].reason == "workaround"

    def test_both_warnings_deduplicated(self):
        nodes = parse_markdown("- Top\n    - Nested\n    - Another nested\n\n```\ncode\n```\n\n```\nmore code\n```\n")
        warnings = check_unsupported(nodes)
        features = [w.feature for w in warnings]
        assert features == ["nested lists", "code blocks"]

    def test_flat_list_no_warning(self):
        nodes = parse_markdown("- A\n- B\n- C\n")
        assert check_unsupported(nodes) == []
