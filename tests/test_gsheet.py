"""Tests for Google Sheets sync functionality.

Uses mock gspread client to test without hitting real Google APIs.
"""

from unittest.mock import MagicMock

import pandas as pd

from gax.gsheet.client import GSheetClient
from gax.gsheet.sheet import pull_single_tab as pull, push_single_tab as push, clone_all, pull_all
from gax.gsheet.frontmatter import SheetConfig, format_content
from gax.multipart import parse_multipart, format_multipart


def make_mock_gc(sheet_data: list[list[str]]):
    """Create a mock gspread client that returns the given sheet data.

    Args:
        sheet_data: List of rows, where each row is a list of cell values.
                   First row is headers.
    """
    gc = MagicMock()
    worksheet = MagicMock()
    spreadsheet = MagicMock()

    # Setup the chain: gc.open_by_key() -> spreadsheet.worksheet() -> worksheet
    gc.open_by_key.return_value = spreadsheet
    spreadsheet.worksheet.return_value = worksheet

    # worksheet.get_all_values() returns the data
    worksheet.get_all_values.return_value = sheet_data

    # worksheet.get(range) also returns data
    worksheet.get.return_value = sheet_data

    return gc, worksheet


def make_mock_gc_multi_tab(
    title: str, tabs: dict[str, list[list[str]]]
) -> tuple[MagicMock, dict[str, MagicMock]]:
    """Create a mock gspread client with multiple tabs.

    Args:
        title: Spreadsheet title
        tabs: Dict mapping tab name to sheet data

    Returns:
        Tuple of (gc mock, dict of worksheet mocks by name)
    """
    gc = MagicMock()
    spreadsheet = MagicMock()
    spreadsheet.title = title

    worksheets = {}
    worksheet_list = []

    for idx, (tab_name, data) in enumerate(tabs.items()):
        ws = MagicMock()
        ws.id = idx
        ws.title = tab_name
        ws.index = idx
        ws.get_all_values.return_value = data
        ws.get.return_value = data
        worksheets[tab_name] = ws
        worksheet_list.append(ws)

    spreadsheet.worksheets.return_value = worksheet_list
    spreadsheet.worksheet.side_effect = lambda name: worksheets[name]

    gc.open_by_key.return_value = spreadsheet

    return gc, worksheets


class TestGSheetClientRead:
    """Tests for GSheetClient.read()."""

    def test_read_simple_sheet(self):
        """Test reading a simple sheet with headers and data."""
        sheet_data = [
            ["Name", "Age", "City"],
            ["Alice", "30", "NYC"],
            ["Bob", "25", "LA"],
            ["Carol", "35", "Chicago"],
        ]
        gc, _ = make_mock_gc(sheet_data)

        client = GSheetClient(gc=gc)
        df = client.read("spreadsheet-123", "Sheet1")

        # Verify API calls
        gc.open_by_key.assert_called_once_with("spreadsheet-123")

        # Verify DataFrame
        assert len(df) == 3
        assert list(df.columns) == ["Name", "Age", "City"]
        assert df.iloc[0]["Name"] == "Alice"
        assert df.iloc[1]["Age"] == "25"
        assert df.iloc[2]["City"] == "Chicago"

    def test_read_empty_sheet(self):
        """Test reading an empty sheet."""
        gc, _ = make_mock_gc([])

        client = GSheetClient(gc=gc)
        df = client.read("spreadsheet-123", "Empty")

        assert len(df) == 0

    def test_read_headers_only(self):
        """Test reading a sheet with only headers, no data."""
        sheet_data = [["Col1", "Col2", "Col3"]]
        gc, _ = make_mock_gc(sheet_data)

        client = GSheetClient(gc=gc)
        df = client.read("spreadsheet-123", "HeadersOnly")

        assert len(df) == 0
        assert list(df.columns) == ["Col1", "Col2", "Col3"]

    def test_read_with_range(self):
        """Test reading with a specific range."""
        sheet_data = [
            ["Name", "Score"],
            ["Alice", "100"],
        ]
        gc, worksheet = make_mock_gc(sheet_data)

        client = GSheetClient(gc=gc)
        df = client.read("spreadsheet-123", "Sheet1", range="A1:B2")

        # Should use get() instead of get_all_values() for range
        worksheet.get.assert_called_once_with("A1:B2")
        assert len(df) == 1
        assert df.iloc[0]["Name"] == "Alice"


class TestGSheetClientWrite:
    """Tests for GSheetClient.write()."""

    def test_write_simple_data(self):
        """Test writing a simple DataFrame."""
        gc, worksheet = make_mock_gc([])

        df = pd.DataFrame(
            {
                "Name": ["Alice", "Bob"],
                "Score": [100, 95],
            }
        )

        client = GSheetClient(gc=gc)
        rows = client.write("spreadsheet-123", "Sheet1", df)

        assert rows == 2

        # Verify update was called with correct data
        worksheet.update.assert_called_once()
        call_kwargs = worksheet.update.call_args[1]
        assert call_kwargs["range_name"] == "A1"
        assert call_kwargs["value_input_option"] == "RAW"

        # Check the values passed
        values = call_kwargs["values"]
        assert values[0] == ["Name", "Score"]  # Headers
        assert values[1] == ["Alice", "100"]
        assert values[2] == ["Bob", "95"]

    def test_write_with_formulas(self):
        """Test writing with formula interpretation enabled."""
        gc, worksheet = make_mock_gc([])

        df = pd.DataFrame(
            {
                "Value": [10, 20],
                "Formula": ["=A2*2", "=A3*2"],
            }
        )

        client = GSheetClient(gc=gc)
        client.write("spreadsheet-123", "Sheet1", df, with_formulas=True)

        call_kwargs = worksheet.update.call_args[1]
        assert call_kwargs["value_input_option"] == "USER_ENTERED"


class TestPullPush:
    """Tests for pull and push commands."""

    def test_pull_updates_file(self, tmp_path):
        """Test pulling data updates the local file."""
        # Create initial file
        config = SheetConfig(
            spreadsheet_id="test-sheet-123",
            tab="Data",
            format="csv",
            url="https://docs.google.com/spreadsheets/d/test-sheet-123",
        )
        initial_data = "Name,Age\nOld,0\n"
        file_path = tmp_path / "test.sheet.gax.md"
        file_path.write_text(format_content(config, initial_data))

        # Mock sheet with new data
        sheet_data = [
            ["Name", "Age"],
            ["Alice", "30"],
            ["Bob", "25"],
        ]
        gc, _ = make_mock_gc(sheet_data)
        client = GSheetClient(gc=gc)

        # Pull
        rows = pull(file_path, client=client)

        assert rows == 2

        # Verify file was updated
        content = file_path.read_text()
        assert "Alice" in content
        assert "Bob" in content
        assert "Old" not in content

    def test_push_sends_data(self, tmp_path):
        """Test pushing data sends it to Google Sheets."""
        # Create file with data
        config = SheetConfig(
            spreadsheet_id="test-sheet-456",
            tab="Upload",
            format="csv",
        )
        data = "Product,Price\nWidget,9.99\nGadget,19.99\n"
        file_path = tmp_path / "products.sheet.gax.md"
        file_path.write_text(format_content(config, data))

        # Mock sheet
        gc, worksheet = make_mock_gc([])
        client = GSheetClient(gc=gc)

        # Push
        rows = push(file_path, client=client)

        assert rows == 2

        # Verify correct spreadsheet/tab was accessed
        gc.open_by_key.assert_called_with("test-sheet-456")

        # Verify data was sent
        call_kwargs = worksheet.update.call_args[1]
        values = call_kwargs["values"]
        assert values[0] == ["Product", "Price"]
        assert values[1] == ["Widget", "9.99"]
        assert values[2] == ["Gadget", "19.99"]


class TestRoundTrip:
    """Tests for pull -> modify -> push round-trip."""

    def test_pull_modify_push(self, tmp_path):
        """Test pulling, modifying locally, and pushing back."""
        # Setup initial file
        config = SheetConfig(
            spreadsheet_id="roundtrip-sheet",
            tab="Data",
            format="csv",
        )
        file_path = tmp_path / "roundtrip.sheet.gax.md"
        file_path.write_text(format_content(config, "Name,Value\n"))

        # Mock for pull - returns server data
        pull_data = [
            ["Name", "Value"],
            ["Item1", "100"],
            ["Item2", "200"],
        ]
        pull_gc, _ = make_mock_gc(pull_data)
        pull_client = GSheetClient(gc=pull_gc)

        # Pull
        pull(file_path, client=pull_client)

        # Verify data was pulled
        content = file_path.read_text()
        assert "Item1" in content
        assert "100" in content

        # Modify the file locally (simulate user edit)
        content = content.replace("100", "999")
        file_path.write_text(content)

        # Mock for push
        push_gc, push_worksheet = make_mock_gc([])
        push_client = GSheetClient(gc=push_gc)

        # Push
        push(file_path, client=push_client)

        # Verify modified data was pushed
        values = push_worksheet.update.call_args[1]["values"]
        assert ["Item1", "999"] in values


class TestCloneAll:
    """Tests for clone_all() - cloning entire spreadsheets."""

    def test_clone_all_tabs(self):
        """Test cloning a spreadsheet with multiple tabs."""
        tabs = {
            "Revenue": [
                ["Month", "Amount"],
                ["Jan", "10000"],
                ["Feb", "12000"],
            ],
            "Expenses": [
                ["Month", "Amount"],
                ["Jan", "8000"],
                ["Feb", "9000"],
            ],
        }
        gc, _ = make_mock_gc_multi_tab("Q1 Report", tabs)
        client = GSheetClient(gc=gc)

        title, sections = clone_all(
            "test-sheet-id",
            "https://docs.google.com/spreadsheets/d/test-sheet-id",
            fmt="csv",
            client=client,
        )

        assert title == "Q1 Report"
        assert len(sections) == 2

        # Check first section
        assert sections[0].headers["title"] == "Q1 Report"
        assert sections[0].headers["tab"] == "Revenue"
        assert sections[0].headers["section"] == 1
        assert "Jan" in sections[0].content
        assert "10000" in sections[0].content

        # Check second section
        assert sections[1].headers["tab"] == "Expenses"
        assert sections[1].headers["section"] == 2
        assert "8000" in sections[1].content

    def test_clone_single_tab(self):
        """Test cloning a spreadsheet with a single tab."""
        tabs = {
            "Sheet1": [
                ["Name", "Value"],
                ["Test", "123"],
            ],
        }
        gc, _ = make_mock_gc_multi_tab("Simple Sheet", tabs)
        client = GSheetClient(gc=gc)

        title, sections = clone_all(
            "simple-id",
            "https://docs.google.com/spreadsheets/d/simple-id",
            client=client,
        )

        assert title == "Simple Sheet"
        assert len(sections) == 1
        assert sections[0].headers["tab"] == "Sheet1"

    def test_clone_preserves_source_url(self):
        """Test that source URL is preserved in headers."""
        tabs = {"Data": [["A"], ["1"]]}
        gc, _ = make_mock_gc_multi_tab("Test", tabs)
        client = GSheetClient(gc=gc)

        url = "https://docs.google.com/spreadsheets/d/abc123/edit#gid=0"
        _, sections = clone_all("abc123", url, client=client)

        assert sections[0].headers["source"] == url


class TestPullAll:
    """Tests for pull_all() - pulling all tabs in multipart file."""

    def test_pull_all_tabs(self, tmp_path):
        """Test pulling updates for all tabs in a multipart file."""
        # Create initial multipart file
        from gax.multipart import Section

        sections = [
            Section(
                headers={
                    "title": "Test Sheet",
                    "source": "https://docs.google.com/spreadsheets/d/test-id",
                    "section": 1,
                    "tab": "Revenue",
                    "format": "csv",
                },
                content="Month,Amount\nOld,0\n",
            ),
            Section(
                headers={
                    "title": "Test Sheet",
                    "source": "https://docs.google.com/spreadsheets/d/test-id",
                    "section": 2,
                    "tab": "Expenses",
                    "format": "csv",
                },
                content="Month,Amount\nOld,0\n",
            ),
        ]
        file_path = tmp_path / "test.sheet.gax.md"
        file_path.write_text(format_multipart(sections))

        # Mock with updated data
        tabs = {
            "Revenue": [
                ["Month", "Amount"],
                ["Jan", "10000"],
            ],
            "Expenses": [
                ["Month", "Amount"],
                ["Jan", "8000"],
            ],
        }
        gc, _ = make_mock_gc_multi_tab("Test Sheet", tabs)
        client = GSheetClient(gc=gc)

        # Pull all
        rows = pull_all(file_path, client=client)

        assert rows == 2  # 1 row per tab

        # Verify file was updated
        content = file_path.read_text()
        updated = parse_multipart(content)

        assert len(updated) == 2
        assert "10000" in updated[0].content
        assert "Old" not in updated[0].content
        assert "8000" in updated[1].content

    def test_pull_all_roundtrip(self, tmp_path):
        """Test clone -> modify -> pull round-trip."""
        # Clone initial
        tabs = {
            "Data": [
                ["Key", "Value"],
                ["A", "1"],
                ["B", "2"],
            ],
        }
        gc, _ = make_mock_gc_multi_tab("Roundtrip Test", tabs)
        client = GSheetClient(gc=gc)

        title, sections = clone_all(
            "roundtrip-id",
            "https://docs.google.com/spreadsheets/d/roundtrip-id",
            client=client,
        )

        file_path = tmp_path / "roundtrip.sheet.gax.md"
        file_path.write_text(format_multipart(sections))

        # Modify on "server" (new mock)
        updated_tabs = {
            "Data": [
                ["Key", "Value"],
                ["A", "100"],
                ["B", "200"],
                ["C", "300"],
            ],
        }
        pull_gc, _ = make_mock_gc_multi_tab("Roundtrip Test", updated_tabs)
        pull_client = GSheetClient(gc=pull_gc)

        # Pull
        rows = pull_all(file_path, client=pull_client)

        assert rows == 3

        # Verify updated content
        content = file_path.read_text()
        assert "100" in content
        assert "200" in content
        assert "300" in content
