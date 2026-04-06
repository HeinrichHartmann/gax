"""Google Sheets API client using gspread"""

import gspread
import pandas as pd
from ..auth import get_authenticated_credentials


class GSheetClient:
    def __init__(self, gc: gspread.Client | None = None):
        """Initialize client with optional gspread client for testing."""
        self._gc = gc

    @property
    def gc(self) -> gspread.Client:
        if self._gc is None:
            creds = get_authenticated_credentials()
            self._gc = gspread.authorize(creds)
        return self._gc

    def get_spreadsheet_info(self, spreadsheet_id: str) -> dict:
        """Get spreadsheet title and tab list."""
        sh = self.gc.open_by_key(spreadsheet_id)
        return {
            "title": sh.title,
            "tabs": [
                {"id": ws.id, "title": ws.title, "index": ws.index}
                for ws in sh.worksheets()
            ],
        }

    def read(
        self, spreadsheet_id: str, tab: str, range: str | None = None
    ) -> pd.DataFrame:
        """Read data from a Google Sheet tab into a DataFrame."""
        sh = self.gc.open_by_key(spreadsheet_id)
        ws = sh.worksheet(tab)

        if range:
            data = ws.get(range)
            if not data:
                return pd.DataFrame()
            headers = data[0]
            rows = data[1:] if len(data) > 1 else []
            return pd.DataFrame(rows, columns=headers)
        else:
            # Use get_all_values to handle empty/duplicate headers
            data = ws.get_all_values()
            if not data:
                return pd.DataFrame()
            headers = data[0]
            rows = data[1:] if len(data) > 1 else []
            return pd.DataFrame(rows, columns=headers)

    def write(
        self,
        spreadsheet_id: str,
        tab: str,
        df: pd.DataFrame,
        with_formulas: bool = False,
        create_if_missing: bool = False,
    ) -> int:
        """Write DataFrame to a Google Sheet tab. Returns number of rows written.

        Clears the sheet first to ensure deleted rows are removed.

        Args:
            spreadsheet_id: The spreadsheet ID
            tab: Tab name
            df: DataFrame to write
            with_formulas: Whether to interpret formulas
            create_if_missing: Create the tab if it doesn't exist

        Returns:
            Number of rows written
        """
        sh = self.gc.open_by_key(spreadsheet_id)

        # Try to get worksheet, create if missing and requested
        try:
            ws = sh.worksheet(tab)
        except gspread.exceptions.WorksheetNotFound:
            if create_if_missing:
                # Create new worksheet with 1000 rows, 26 columns
                ws = sh.add_worksheet(title=tab, rows=1000, cols=26)
            else:
                raise

        # Clear the entire sheet first to remove any stale data
        ws.clear()

        # Fill NaN with empty string and convert to list of lists
        df = df.fillna("")
        values = [df.columns.tolist()] + df.astype(str).values.tolist()

        # Update starting from A1
        # USER_ENTERED interprets formulas, RAW writes literals
        value_input_option = "USER_ENTERED" if with_formulas else "RAW"
        ws.update(range_name="A1", values=values, value_input_option=value_input_option)

        return len(df)

    def delete_worksheet(self, spreadsheet_id: str, tab: str) -> None:
        """Delete a worksheet from a spreadsheet.

        Args:
            spreadsheet_id: The spreadsheet ID
            tab: Tab name to delete
        """
        sh = self.gc.open_by_key(spreadsheet_id)
        ws = sh.worksheet(tab)
        sh.del_worksheet(ws)
