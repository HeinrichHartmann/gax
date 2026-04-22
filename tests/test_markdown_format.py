"""Tests for markdown format handler.

Regression tests for issue #8: sheet push drops columns beyond 5th.
"""

from gax.formats.markdown import MarkdownFormat


class TestMarkdownFormat:
    """Tests for MarkdownFormat read/write."""

    def test_simple_table(self):
        """Test parsing a simple markdown table."""
        content = """| Name | Age | City |
| --- | --- | --- |
| Alice | 30 | NYC |
| Bob | 25 | LA |
"""
        fmt = MarkdownFormat()
        df = fmt.read(content)

        assert df.shape == (2, 3)
        assert list(df.columns) == ["Name", "Age", "City"]
        assert df.iloc[0]["Name"] == "Alice"
        assert df.iloc[1]["City"] == "LA"

    def test_simple_8_column_table(self):
        """Test parsing an 8-column table (issue #8 regression)."""
        content = """| Col1 | Col2 | Col3 | Col4 | Col5 | Col6 | Col7 | Col8 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | B | C | D | E | F | G | H |
| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
"""
        fmt = MarkdownFormat()
        df = fmt.read(content)

        assert df.shape == (2, 8), f"Expected (2, 8), got {df.shape}"
        assert list(df.columns) == [
            "Col1",
            "Col2",
            "Col3",
            "Col4",
            "Col5",
            "Col6",
            "Col7",
            "Col8",
        ]

        # Check first data row
        assert list(df.iloc[0]) == ["A", "B", "C", "D", "E", "F", "G", "H"]

        # Check second data row
        assert list(df.iloc[1]) == ["1", "2", "3", "4", "5", "6", "7", "8"]

    def test_variable_width_tables(self):
        """Test parsing file with tables of different widths (issue #8).

        Simulates Bitcoin tab: 5-column summary table followed by 8-column
        transaction table. The parser should use the maximum column count.
        """
        content = """| ZUSAMMENFASSUNG |  |  |  |  |
| --- | --- | --- | --- | --- |
| **Steuerstatus 2025** | **€0 Steuer** | ✅ | Info |  |
| Verkaufserlöse gesamt | 14.259,77 € | 9 Transaktionen | Jan + Sep 2025 |  |
| TRANSAKTIONSHISTORIE |  |  |  |  |  |  |  |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Datum | Zeit | Typ | Asset | Menge | EUR | Preis (EUR) | TX-ID |
| 2021-04-23 | 14:10:42 | BUY | ETH | 0.10232110 | 200.00 | 1954.63 | Tc9a41cb |
| 2021-04-23 | 14:11:40 | BUY | DOGE | 971.30748137 | 200.00 | 0.21 | Tad6b7a3 |
"""

        fmt = MarkdownFormat()
        df = fmt.read(content)

        # Should use maximum column count (8) from the wider table
        assert df.shape[1] == 8, f"Expected 8 columns, got {df.shape[1]}"

        # Check that all columns from 8-column rows are preserved
        # The last row should have all 8 values
        last_row = df.iloc[-1]
        assert last_row.iloc[0] == "2021-04-23"  # Datum
        assert last_row.iloc[1] == "14:11:40"  # Zeit
        assert last_row.iloc[2] == "BUY"  # Typ
        assert last_row.iloc[3] == "DOGE"  # Asset
        assert last_row.iloc[4] == "971.30748137"  # Menge
        assert last_row.iloc[5] == "200.00"  # EUR
        assert last_row.iloc[6] == "0.21"  # Preis (EUR)
        assert last_row.iloc[7] == "Tad6b7a3"  # TX-ID

    def test_write_roundtrip(self):
        """Test that write -> read preserves data."""
        import pandas as pd

        df = pd.DataFrame(
            {
                "Col1": ["A", "B"],
                "Col2": ["C", "D"],
                "Col3": ["E", "F"],
            }
        )

        fmt = MarkdownFormat()
        content = fmt.write(df)
        df2 = fmt.read(content)

        assert df.shape == df2.shape
        assert list(df.columns) == list(df2.columns)
        assert list(df.iloc[0]) == list(df2.iloc[0])

    def test_multiline_cell_roundtrip(self):
        """Test that cells with newlines survive write -> read roundtrip via <br>."""
        import pandas as pd

        df = pd.DataFrame(
            {
                "company": ["Dash0", "OllyGarden"],
                "contacts": ["Mirko Novakovic\nSonja Bata", "Juraci Kröhling"],
                "amount": ["10000", "5000"],
            }
        )

        fmt = MarkdownFormat()
        content = fmt.write(df)

        # Written form should use <br>, not literal newlines in cells
        assert "<br>" in content
        assert "Mirko Novakovic<br>Sonja Bata" in content

        # Read back should restore newlines
        df2 = fmt.read(content)
        assert df2.shape == df.shape
        assert df2.iloc[0]["contacts"] == "Mirko Novakovic\nSonja Bata"
        assert df2.iloc[1]["contacts"] == "Juraci Kröhling"

    def test_corrupted_multiline_raises(self):
        """Test that unencoded newlines in cells are detected as corruption."""
        import pytest

        # Simulate what happens when a cell has a literal newline:
        # the row splits into two lines with different column counts
        content = (
            "| company | contacts | amount |\n"
            "| --- | --- | --- |\n"
            "| Dash0 | Mirko\n"
            "Sonja | 10000 |\n"
            "| OllyGarden | Juraci | 5000 |\n"
        )

        fmt = MarkdownFormat()
        with pytest.raises(ValueError, match="inconsistent column counts"):
            fmt.read(content)

    def test_literal_br_in_source_raises(self):
        """Test that literal <br> in source data raises an error."""
        import pandas as pd
        import pytest

        df = pd.DataFrame(
            {
                "name": ["Alice"],
                "notes": ["some<br>thing"],
            }
        )

        fmt = MarkdownFormat()
        with pytest.raises(ValueError, match="literal '<br>'"):
            fmt.write(df)

    def test_empty_content(self):
        """Test handling empty content."""
        fmt = MarkdownFormat()
        df = fmt.read("")

        assert len(df) == 0
        assert len(df.columns) == 0
