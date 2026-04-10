"""End-to-end integration tests for gax.

These tests require authentication and use real Google Docs/Sheets.

Required environment variables:
    GAX_TEST_DOC   - Google Doc ID for testing
    GAX_TEST_SHEET - Google Sheet ID for testing

Example .envrc:
    export GAX_TEST_DOC="1DofO8emfHx8bhENkw23hQRj2T6pizH1X7Isq7uHH5f0"
    export GAX_TEST_SHEET="1NtUmXPsF5XBBSRO8kzbSnGJX4YdkHWFzlfqPYlJtdoo"

Run with: make test-e2e
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from googleapiclient.discovery import build

from gax.auth import get_authenticated_credentials, is_authenticated


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _get_test_doc_id() -> str:
    """Get test doc ID from environment."""
    doc_id = os.environ.get("GAX_TEST_DOC")
    if not doc_id:
        pytest.skip(
            "GAX_TEST_DOC not set. Add to .envrc:\n"
            '  export GAX_TEST_DOC="<your-test-doc-id>"'
        )
    return doc_id


def _get_test_sheet_id() -> str:
    """Get test sheet ID from environment."""
    sheet_id = os.environ.get("GAX_TEST_SHEET")
    if not sheet_id:
        pytest.skip(
            "GAX_TEST_SHEET not set. Add to .envrc:\n"
            '  export GAX_TEST_SHEET="<your-test-sheet-id>"'
        )
    return sheet_id


def _run_gax(*args: str) -> subprocess.CompletedProcess:
    """Run gax CLI command and return result."""
    return subprocess.run(
        ["gax", *args],
        capture_output=True,
        text=True,
    )


# =============================================================================
# Clear functions
# =============================================================================


def clear_doc_tabs(doc_id: str) -> list[str]:
    """Delete all tabs except the first one from a document.

    Returns list of deleted tab names.
    """
    creds = get_authenticated_credentials()
    service = build("docs", "v1", credentials=creds)

    doc = service.documents().get(
        documentId=doc_id,
        includeTabsContent=True,  # Required to get tabs list
    ).execute()

    tabs = doc.get("tabs", [])
    if len(tabs) <= 1:
        return []

    # Delete all tabs except the first one
    deleted = []
    for tab in tabs[1:]:
        props = tab.get("tabProperties", {})
        title = props.get("title", "")
        tab_id = props.get("tabId", "")
        if tab_id:
            try:
                service.documents().batchUpdate(
                    documentId=doc_id,
                    body={"requests": [{"deleteTab": {"tabId": tab_id}}]}
                ).execute()
                deleted.append(title)
            except Exception as e:
                print(f"Warning: Could not delete tab {title}: {e}")

    return deleted


def clear_sheet_tabs(sheet_id: str) -> list[str]:
    """Delete all sheets except the first one from a spreadsheet.

    Returns list of deleted sheet names.
    """
    creds = get_authenticated_credentials()
    service = build("sheets", "v4", credentials=creds)

    spreadsheet = service.spreadsheets().get(spreadsheetId=sheet_id).execute()

    sheets = spreadsheet.get("sheets", [])
    if len(sheets) <= 1:
        return []

    # Delete all sheets except the first one
    deleted = []
    for sheet in sheets[1:]:
        props = sheet.get("properties", {})
        title = props.get("title", "")
        sheet_tab_id = props.get("sheetId")
        if sheet_tab_id is not None:
            try:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=sheet_id,
                    body={"requests": [{"deleteSheet": {"sheetId": sheet_tab_id}}]}
                ).execute()
                deleted.append(title)
            except Exception as e:
                print(f"Warning: Could not delete sheet {title}: {e}")

    return deleted


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def check_auth():
    """Skip tests if not authenticated."""
    if not is_authenticated():
        pytest.skip("Not authenticated. Run 'gax auth login' first.")


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def test_doc():
    """Get test doc ID and URL, clear all extra tabs."""
    doc_id = _get_test_doc_id()
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    # Setup: clear all extra tabs
    clear_doc_tabs(doc_id)
    yield {"id": doc_id, "url": doc_url}
    # Teardown: clear all extra tabs
    clear_doc_tabs(doc_id)


@pytest.fixture
def test_sheet():
    """Get test sheet ID and URL, clear all extra sheets."""
    sheet_id = _get_test_sheet_id()
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    # Setup: clear all extra sheets
    clear_sheet_tabs(sheet_id)
    yield {"id": sheet_id, "url": sheet_url}
    # Teardown: clear all extra sheets
    clear_sheet_tabs(sheet_id)


# =============================================================================
# Doc E2E Tests
# =============================================================================


@pytest.mark.e2e
class TestDocE2E:
    """End-to-end tests for Google Docs operations."""

    def test_import_pull_cycle(self, check_auth, test_doc, temp_dir):
        """Test: import markdown -> pull -> verify content."""
        fixture_content = (FIXTURES_DIR / "e2e_test1.md").read_text()
        test_file = temp_dir / "doc1.md"
        test_file.write_text(fixture_content)

        # Step 1: Import as new tab
        tracking_file = temp_dir / "doc1.tab.gax"
        result = _run_gax(
            "doc", "tab", "import",
            test_doc["url"],
            str(test_file),
            "-o", str(tracking_file),
        )
        assert result.returncode == 0, f"Import failed: {result.stderr}"
        assert "Created tab" in result.stdout
        assert tracking_file.exists()

        # Step 2: Pull content back
        result = _run_gax("doc", "tab", "pull", str(tracking_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        # Step 3: Verify content contains expected elements
        pulled_content = tracking_file.read_text()
        assert "E2E Test Document" in pulled_content
        assert "test document" in pulled_content

    def test_import_update_cycle(self, check_auth, test_doc, temp_dir):
        """Test: import -> update content -> push -> pull -> verify."""
        fixture_content = (FIXTURES_DIR / "e2e_test2.md").read_text()
        test_file = temp_dir / "doc2.md"
        test_file.write_text(fixture_content)

        # Import
        tracking_file = temp_dir / "doc2.tab.gax"
        result = _run_gax(
            "doc", "tab", "import",
            test_doc["url"],
            str(test_file),
            "-o", str(tracking_file),
        )
        assert result.returncode == 0, f"Import failed: {result.stderr}"

        # Modify local content
        content = tracking_file.read_text()
        updated_content = content.replace("Second Test Tab", "Updated E2E Tab")
        tracking_file.write_text(updated_content)

        # Push changes
        result = _run_gax("doc", "tab", "push", str(tracking_file), "-y")
        assert result.returncode == 0, f"Push failed: {result.stderr}"

        # Pull and verify
        result = _run_gax("doc", "tab", "pull", str(tracking_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        final_content = tracking_file.read_text()
        assert "Updated E2E Tab" in final_content

    def test_table_push_pull_cycle(self, check_auth, test_doc, temp_dir):
        """Test: import markdown with table -> push -> pull -> verify cell content (#14)."""
        fixture_content = (FIXTURES_DIR / "e2e_table_test.md").read_text()
        test_file = temp_dir / "table_test.md"
        test_file.write_text(fixture_content)

        # Import as new tab
        tracking_file = temp_dir / "table_test.tab.gax"
        result = _run_gax(
            "doc", "tab", "import",
            test_doc["url"],
            str(test_file),
            "-o", str(tracking_file),
        )
        assert result.returncode == 0, f"Import failed: {result.stderr}"

        # Push
        result = _run_gax("doc", "tab", "push", str(tracking_file), "-y")
        assert result.returncode == 0, f"Push failed: {result.stderr}"

        # Pull back
        result = _run_gax("doc", "tab", "pull", str(tracking_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        # Verify table cell content survived the round-trip
        final_content = tracking_file.read_text()
        assert "Hello World" in final_content
        assert "Batch Training" in final_content
        assert "Some description" in final_content
        assert "Another description" in final_content

    def test_rich_formatting_round_trip(self, check_auth, test_doc, temp_dir):
        """Golden test: push markdown -> pull -> output must equal input.

        The fixture is crafted to be a fixed point of the push/pull cycle.
        If this fails, the diff shows exactly what formatting was lost.
        """
        fixture_path = FIXTURES_DIR / "e2e_rich_formatting.md"
        before = fixture_path.read_text()
        test_file = temp_dir / "rich_fmt.md"
        test_file.write_text(before)

        # Import as new tab
        tracking_file = temp_dir / "rich_fmt.tab.gax"
        result = _run_gax(
            "doc", "tab", "import",
            test_doc["url"],
            str(test_file),
            "-o", str(tracking_file),
        )
        assert result.returncode == 0, f"Import failed: {result.stderr}"

        # Push
        result = _run_gax("doc", "tab", "push", str(tracking_file), "-y")
        assert result.returncode == 0, f"Push failed: {result.stderr}"

        # Pull back
        result = _run_gax("doc", "tab", "pull", str(tracking_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        # Extract markdown body (skip YAML header)
        raw = tracking_file.read_text()
        parts = raw.split("---", 2)
        after = parts[2].strip() + "\n" if len(parts) >= 3 else raw

        # Golden comparison
        if before != after:
            import difflib
            diff = difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="before (fixture)",
                tofile="after (push/pull round-trip)",
            )
            diff_text = "".join(diff)
            assert False, f"Round-trip diff (push lost formatting):\n{diff_text}"


# =============================================================================
# Sheet E2E Tests
# =============================================================================


@pytest.mark.e2e
class TestSheetE2E:
    """End-to-end tests for Google Sheets operations."""

    def test_clone_pull_cycle(self, check_auth, test_sheet, temp_dir):
        """Test: create tab via API -> clone -> pull -> verify."""
        creds = get_authenticated_credentials()
        service = build("sheets", "v4", credentials=creds)
        sheet_id = test_sheet["id"]

        tab_name = "test_sheet1"

        # Add sheet
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
        ).execute()

        # Add some data
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A1:C3",
            valueInputOption="RAW",
            body={
                "values": [
                    ["id", "name", "status"],
                    ["1", "Test", "active"],
                    ["2", "Data", "pending"],
                ]
            }
        ).execute()

        # Clone the tab
        output_file = temp_dir / f"{tab_name}.sheet.gax"
        result = _run_gax(
            "sheet", "tab", "clone",
            test_sheet["url"],
            tab_name,
            "-o", str(output_file),
        )
        assert result.returncode == 0, f"Clone failed: {result.stderr}"
        assert output_file.exists()

        content = output_file.read_text()
        assert "id" in content
        assert "Test" in content
        assert "active" in content

        # Pull
        result = _run_gax("sheet", "tab", "pull", str(output_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

    def test_push_cycle(self, check_auth, test_sheet, temp_dir):
        """Test: create tab -> clone -> modify -> push -> verify."""
        creds = get_authenticated_credentials()
        service = build("sheets", "v4", credentials=creds)
        sheet_id = test_sheet["id"]

        tab_name = "test_sheet2"

        # Add sheet with initial data
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
        ).execute()

        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A1:B2",
            valueInputOption="RAW",
            body={
                "values": [
                    ["key", "value"],
                    ["original", "data"],
                ]
            }
        ).execute()

        # Clone
        output_file = temp_dir / f"{tab_name}.sheet.gax"
        result = _run_gax(
            "sheet", "tab", "clone",
            test_sheet["url"],
            tab_name,
            "-o", str(output_file),
        )
        assert result.returncode == 0, f"Clone failed: {result.stderr}"

        # Modify content
        content = output_file.read_text()
        updated_content = content.replace("original", "modified")
        output_file.write_text(updated_content)

        # Push
        result = _run_gax("sheet", "tab", "push", str(output_file), "-y")
        assert result.returncode == 0, f"Push failed: {result.stderr}"

        # Verify via API
        values = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab_name}!A1:B2",
        ).execute()

        assert values["values"][1][0] == "modified"


# =============================================================================
# Combined workflow tests
# =============================================================================


@pytest.mark.e2e
class TestCombinedE2E:
    """Tests that exercise both docs and sheets together."""

    def test_multi_tab_workflow(self, check_auth, test_doc, test_sheet, temp_dir):
        """Test importing multiple tabs to both doc and sheet."""
        # Import two tabs to doc
        for i, fixture in enumerate(["e2e_test1.md", "e2e_test2.md"], 1):
            fixture_content = (FIXTURES_DIR / fixture).read_text()
            test_file = temp_dir / f"multi{i}.md"
            test_file.write_text(fixture_content)

            result = _run_gax(
                "doc", "tab", "import",
                test_doc["url"],
                str(test_file),
                "-o", str(temp_dir / f"multi{i}.tab.gax"),
            )
            assert result.returncode == 0, f"Doc import {i} failed: {result.stderr}"

        # Create two tabs in sheet
        creds = get_authenticated_credentials()
        service = build("sheets", "v4", credentials=creds)
        sheet_id = test_sheet["id"]

        for i, fixture in enumerate(["e2e_sheet1.md", "e2e_sheet2.md"], 1):
            tab_name = f"multi_sheet{i}"

            # Add sheet
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]}
            ).execute()

            # Read fixture and parse markdown table
            fixture_content = (FIXTURES_DIR / fixture).read_text()
            lines = [
                ln for ln in fixture_content.strip().split("\n")
                if ln.strip() and not ln.startswith("|--")
            ]
            values = []
            for line in lines:
                cells = [c.strip() for c in line.split("|") if c.strip()]
                values.append(cells)

            # Write to sheet
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"{tab_name}!A1",
                valueInputOption="RAW",
                body={"values": values}
            ).execute()

            # Clone
            output_file = temp_dir / f"{tab_name}.sheet.gax"
            result = _run_gax(
                "sheet", "tab", "clone",
                test_sheet["url"],
                tab_name,
                "-o", str(output_file),
            )
            assert result.returncode == 0, f"Sheet clone {i} failed: {result.stderr}"

        # Verify we have 4 tracking files
        tracking_files = list(temp_dir.glob("*.gax"))
        assert len(tracking_files) == 4


# =============================================================================
# Image roundtrip tests
# =============================================================================


@pytest.mark.e2e
class TestImageE2E:
    """End-to-end tests for image extraction and inlining."""

    def test_image_extraction_and_inlining(self, check_auth, temp_dir):
        """Test image extraction from blob store and inlining back."""
        from gax.native_md import extract_images_to_store, inline_images_from_store
        import base64

        # Create a small test image (1x1 red PNG)
        red_pixel_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "nGP4z8DwHwAFAAH/q842AAAAAElFTkSuQmCC"
        )

        test_md = f"# Test\n\n![img](data:image/png;base64,{red_pixel_b64})\n"

        # Step 1: Extract image to blob store
        extracted = extract_images_to_store(test_md)

        # Verify base64 is gone
        assert "data:image" not in extracted, "Base64 should be extracted"
        assert "file://" in extracted, "Should have file:// URL"
        assert ".gax/store/blob/" in extracted, "Should reference blob store"

        # Step 2: Inline images back
        inlined = inline_images_from_store(extracted)

        # Verify base64 is back
        assert "data:image/png;base64," in inlined, "Should have base64 data URL"
        assert "file://" not in inlined, "file:// should be replaced"

        # Step 3: Verify the image data is correct
        # Extract the base64 from the result and compare
        import re
        match = re.search(r'base64,([A-Za-z0-9+/=]+)', inlined)
        assert match, "Should find base64 data"

        # Decode both and compare
        original_bytes = base64.b64decode(red_pixel_b64)
        roundtrip_bytes = base64.b64decode(match.group(1))
        assert original_bytes == roundtrip_bytes, "Image data should match after roundtrip"

    def test_image_pull_from_real_doc(self, check_auth, temp_dir):
        """Test pulling from a document that has images (Signals doc)."""
        # Use the Signals PC Briefing doc which has an image
        signals_doc_id = "1WhTCn_R7O2EavEedb9DWCmLH5QcrqP5tB-aaK3z8bm0"
        signals_url = f"https://docs.google.com/document/d/{signals_doc_id}/edit"

        # Clone the full doc
        output_file = temp_dir / "signals.doc.gax"
        result = _run_gax("doc", "clone", signals_url, "-o", str(output_file))
        assert result.returncode == 0, f"Clone failed: {result.stderr}"

        content = output_file.read_text()

        # Verify images were extracted (no base64, has file:// URLs)
        assert "data:image" not in content, "Base64 images should be extracted"

        # The Signals doc has at least one image
        if "file://" in content:
            assert ".gax/store/blob/" in content, "Should reference blob store"

            # Verify blob file exists
            import re
            urls = re.findall(r'file://([^\s\)>"]+)', content)
            for url in urls:
                from pathlib import Path
                assert Path(url).exists(), f"Blob file should exist: {url}"


# =============================================================================
# Calendar E2E Tests
# =============================================================================


def _check_calendar_access() -> bool:
    """Check if we have calendar API access (scope may need re-auth)."""
    try:
        from gax.gcal import list_calendars
        list_calendars()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def check_calendar_auth():
    """Skip tests if calendar access not available."""
    if not is_authenticated():
        pytest.skip("Not authenticated. Run 'gax auth login' first.")
    if not _check_calendar_access():
        pytest.skip(
            "Calendar access not available. "
            "Re-authenticate with: gax auth logout && gax auth login"
        )


@pytest.mark.e2e
class TestCalendarE2E:
    """End-to-end tests for Google Calendar operations."""

    def test_calendars_list(self, check_calendar_auth):
        """Test listing calendars."""
        result = _run_gax("cal", "calendars")
        assert result.returncode == 0, f"Failed: {result.stderr}"
        # Should have at least one calendar (primary)
        assert len(result.stdout.strip()) > 0

    def test_events_list_md(self, check_calendar_auth):
        """Test listing events in markdown format."""
        result = _run_gax("cal", "list", "--days", "7")
        assert result.returncode == 0, f"Failed: {result.stderr}"
        # Output should be markdown (starts with # or "No upcoming")
        output = result.stdout.strip()
        assert output.startswith("#") or "No upcoming" in output

    def test_events_list_tsv(self, check_calendar_auth):
        """Test listing events in TSV format."""
        result = _run_gax("cal", "list", "--days", "7", "--format", "tsv")
        assert result.returncode == 0, f"Failed: {result.stderr}"
        # TSV should have header row
        assert "calendar\tdate\tstart\tend\trsvp\ttitle" in result.stdout

    def test_event_create_push_delete_cycle(self, check_calendar_auth, temp_dir):
        """Test: create new event -> push -> pull -> delete."""
        # Step 1: Create a new event file
        event_file = temp_dir / "test_event.cal.gax"
        result = _run_gax("cal", "event", "new", "-o", str(event_file))
        assert result.returncode == 0, f"New failed: {result.stderr}"
        assert event_file.exists()

        # Step 2: Modify the event content
        content = event_file.read_text()
        # Update title to be unique and identifiable
        import time
        test_title = f"GAX_E2E_TEST_{int(time.time())}"
        updated_content = content.replace("New Event", test_title)
        event_file.write_text(updated_content)

        # Step 3: Push to create upstream
        result = _run_gax("cal", "event", "push", str(event_file), "-y")
        assert result.returncode == 0, f"Push failed: {result.stderr}"
        assert "Created event" in result.stdout

        # Verify the file was updated with ID
        content = event_file.read_text()
        assert "id:" in content
        # ID should not be empty
        import re
        id_match = re.search(r"^id:\s*(\S+)", content, re.MULTILINE)
        assert id_match, "Event should have an ID after push"
        event_id = id_match.group(1)
        assert event_id, "Event ID should not be empty"

        # Step 4: Pull to verify
        result = _run_gax("cal", "event", "pull", str(event_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        # Verify title is still correct
        content = event_file.read_text()
        assert test_title in content

        # Step 5: Delete the event
        result = _run_gax("cal", "event", "delete", str(event_file), "-y")
        assert result.returncode == 0, f"Delete failed: {result.stderr}"
        assert "Deleted event" in result.stdout

        # Verify file is also deleted
        assert not event_file.exists()

    def test_event_clone_existing(self, check_calendar_auth, temp_dir):
        """Test cloning an existing event.

        This test creates an event, clones it, then cleans up.
        """
        # First create an event to clone
        event_file = temp_dir / "original.cal.gax"
        result = _run_gax("cal", "event", "new", "-o", str(event_file))
        assert result.returncode == 0

        import time
        test_title = f"GAX_CLONE_TEST_{int(time.time())}"
        content = event_file.read_text()
        updated_content = content.replace("New Event", test_title)
        event_file.write_text(updated_content)

        # Push to create upstream
        result = _run_gax("cal", "event", "push", str(event_file), "-y")
        assert result.returncode == 0, f"Push failed: {result.stderr}"

        # Get the event ID
        content = event_file.read_text()
        import re
        id_match = re.search(r"^id:\s*(\S+)", content, re.MULTILINE)
        event_id = id_match.group(1)

        # Clone the event
        clone_file = temp_dir / "cloned.cal.gax"
        result = _run_gax("cal", "event", "clone", event_id, "-o", str(clone_file))
        assert result.returncode == 0, f"Clone failed: {result.stderr}"
        assert clone_file.exists()

        # Verify cloned content
        clone_content = clone_file.read_text()
        assert test_title in clone_content
        assert event_id in clone_content

        # Clean up: delete the original event (clone file is just local)
        result = _run_gax("cal", "event", "delete", str(event_file), "-y")
        assert result.returncode == 0
