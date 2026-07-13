"""Google Sheets API client using gspread"""

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import gspread
import pandas as pd
from gspread.utils import DateTimeOption, ValueRenderOption

from ..auth import get_authenticated_credentials

logger = logging.getLogger(__name__)

# Render options for reading cell values (see ADR 034):
#   FORMULA render option implements the three-type contract:
#     - formula cells  -> formula text ("=SUM(A1:B1)")
#     - number cells   -> raw number, no display formatting ("82000", not "$82,500")
#     - string cells   -> the string
#   FORMATTED_STRING keeps date/time cells as displayed strings instead of
#   serial numbers (which FORMULA rendering would otherwise return).
_READ_OPTS = {
    "value_render_option": ValueRenderOption.formula,
    "date_time_render_option": DateTimeOption.formatted_string,
}

_PROFILE = os.environ.get("GAX_PROFILE", "")


def _tlog(msg: str) -> None:
    """Print a timestamped profiling line to stderr when GAX_PROFILE=1."""
    if _PROFILE:
        print(f"[profile] {time.perf_counter():.3f}  {msg}", file=sys.stderr)


class GSheetClient:
    def __init__(self, gc: gspread.Client | None = None):
        """Initialize client with optional gspread client for testing."""
        self._gc = gc
        self._spreadsheet_cache: dict[str, gspread.Spreadsheet] = {}
        self._worksheet_cache: dict[tuple[str, str], gspread.Worksheet] = {}

    @property
    def gc(self) -> gspread.Client:
        if self._gc is None:
            t0 = time.perf_counter()
            creds = get_authenticated_credentials()
            _tlog(f"get_authenticated_credentials: {time.perf_counter() - t0:.3f}s")
            t0 = time.perf_counter()
            self._gc = gspread.authorize(creds)
            _tlog(f"gspread.authorize: {time.perf_counter() - t0:.3f}s")
        return self._gc

    def _open(self, spreadsheet_id: str) -> gspread.Spreadsheet:
        """Open a spreadsheet by ID, returning cached object if available."""
        if spreadsheet_id not in self._spreadsheet_cache:
            t0 = time.perf_counter()
            self._spreadsheet_cache[spreadsheet_id] = self.gc.open_by_key(spreadsheet_id)
            _tlog(f"open_by_key '{spreadsheet_id}': {time.perf_counter() - t0:.3f}s")
        return self._spreadsheet_cache[spreadsheet_id]

    def _get_worksheet(self, spreadsheet_id: str, tab: str) -> gspread.Worksheet:
        """Get a worksheet by title, using cache if available."""
        key = (spreadsheet_id, tab)
        if key not in self._worksheet_cache:
            sh = self._open(spreadsheet_id)
            t0 = time.perf_counter()
            self._worksheet_cache[key] = sh.worksheet(tab)
            _tlog(f"worksheet('{tab}'): {time.perf_counter() - t0:.3f}s")
        return self._worksheet_cache[key]

    def get_spreadsheet_info(self, spreadsheet_id: str) -> dict:
        """Get spreadsheet title and tab list.

        Also pre-populates the worksheet cache to avoid redundant API calls.
        """
        sh = self._open(spreadsheet_id)
        t0 = time.perf_counter()
        worksheets = sh.worksheets()
        _tlog(f"worksheets(): {time.perf_counter() - t0:.3f}s")
        # Pre-populate worksheet cache
        for ws in worksheets:
            self._worksheet_cache[(spreadsheet_id, ws.title)] = ws
        return {
            "title": sh.title,
            "tabs": [
                {"id": ws.id, "title": ws.title, "index": ws.index}
                for ws in worksheets
            ],
        }

    def read(
        self, spreadsheet_id: str, tab: str, range: str | None = None
    ) -> pd.DataFrame:
        """Read data from a Google Sheet tab into a DataFrame."""
        ws = self._get_worksheet(spreadsheet_id, tab)

        t0 = time.perf_counter()
        if range:
            data = ws.get(range, pad_values=True, **_READ_OPTS)
            _tlog(f"ws.get(range) '{tab}': {time.perf_counter() - t0:.3f}s")
            if not data:
                return pd.DataFrame()
            headers = data[0]
            rows = data[1:] if len(data) > 1 else []
            return pd.DataFrame(rows, columns=headers)
        else:
            # Use get_all_values to handle empty/duplicate headers
            data = ws.get_all_values(**_READ_OPTS)
            _tlog(f"get_all_values '{tab}': {time.perf_counter() - t0:.3f}s")
            if not data:
                return pd.DataFrame()
            headers = data[0]
            rows = data[1:] if len(data) > 1 else []
            return pd.DataFrame(rows, columns=headers)

    def read_all(
        self, spreadsheet_id: str, tab_names: list[str]
    ) -> dict[str, pd.DataFrame]:
        """Read multiple tabs concurrently. Returns {tab_name: DataFrame}.

        Requires get_spreadsheet_info() to have been called first
        (to pre-populate the worksheet cache).
        """
        t0 = time.perf_counter()

        def _fetch(tab: str) -> tuple[str, pd.DataFrame]:
            ws = self._get_worksheet(spreadsheet_id, tab)
            data = ws.get_all_values(**_READ_OPTS)
            if not data:
                return tab, pd.DataFrame()
            return tab, pd.DataFrame(data[1:], columns=data[0])

        with ThreadPoolExecutor(max_workers=len(tab_names)) as pool:
            results = dict(pool.map(_fetch, tab_names))

        _tlog(f"read_all ({len(tab_names)} tabs): {time.perf_counter() - t0:.3f}s")
        return results

    def write(
        self,
        spreadsheet_id: str,
        tab: str,
        df: pd.DataFrame,
        values: bool = False,
        create_if_missing: bool = False,
    ) -> int:
        """Write DataFrame to a Google Sheet tab. Returns number of rows written.

        Clears the sheet first to ensure deleted rows are removed.

        Args:
            spreadsheet_id: The spreadsheet ID
            tab: Tab name
            df: DataFrame to write
            values: If True, write as literal strings (RAW mode, no formula
                 interpretation). Default False uses USER_ENTERED which
                 interprets formulas and preserves number formatting.
            create_if_missing: Create the tab if it doesn't exist

        Returns:
            Number of rows written
        """
        sh = self._open(spreadsheet_id)

        # Try to get worksheet, create if missing and requested
        try:
            ws = self._get_worksheet(spreadsheet_id, tab)
        except gspread.exceptions.WorksheetNotFound:
            if create_if_missing:
                # Create new worksheet with 1000 rows, 26 columns
                ws = sh.add_worksheet(title=tab, rows=1000, cols=26)
                self._worksheet_cache[(spreadsheet_id, tab)] = ws
            else:
                raise

        # Clear the entire sheet first to remove any stale data
        ws.clear()

        # Fill NaN with empty string and convert to list of lists
        df = df.fillna("")
        rows = [df.columns.tolist()] + df.astype(str).values.tolist()

        # Update starting from A1
        # USER_ENTERED interprets formulas and numbers, RAW writes literals
        value_input_option = "RAW" if values else "USER_ENTERED"
        ws.update(range_name="A1", values=rows, value_input_option=value_input_option)

        return len(df)

    def delete_worksheet(self, spreadsheet_id: str, tab: str) -> None:
        """Delete a worksheet from a spreadsheet.

        Args:
            spreadsheet_id: The spreadsheet ID
            tab: Tab name to delete
        """
        sh = self._open(spreadsheet_id)
        ws = self._get_worksheet(spreadsheet_id, tab)
        sh.del_worksheet(ws)
        # Invalidate cache for this worksheet
        self._worksheet_cache.pop((spreadsheet_id, tab), None)
