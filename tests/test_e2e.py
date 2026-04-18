"""End-to-end integration tests for gax.

These tests require authentication and use a real Google account.

E2E test policy
===============

  - Tests run against the user's actual Google account. They must tolerate
    pre-existing content and only operate on test-specific resources.

  - All test resources are tagged with a unique prefix (E2E_PREFIX = "gaxe2e")
    so they can be identified and cleaned up reliably.

  - Each test class has a cleanup function that finds and deletes ALL resources
    matching the prefix — not just the ones created in the current run. This
    handles recovery from partial/crashed runs.

  - Cleanup runs in both setup (before) and teardown (finally) to ensure
    idempotent test runs.

  - Priority: never delete user data. Be precise about what you clean up.
    Prefer unique, unlikely-to-collide prefixes.

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

# Shared prefix for all e2e test resources. Used to tag and clean up.
E2E_PREFIX = "gaxe2e"


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

    doc = (
        service.documents()
        .get(
            documentId=doc_id,
            includeTabsContent=True,  # Required to get tabs list
        )
        .execute()
    )

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
                    body={"requests": [{"deleteTab": {"tabId": tab_id}}]},
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
                    body={"requests": [{"deleteSheet": {"sheetId": sheet_tab_id}}]},
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
        tracking_file = temp_dir / "doc1.tab.gax.md"
        result = _run_gax(
            "doc",
            "tab",
            "import",
            test_doc["url"],
            str(test_file),
            "-o",
            str(tracking_file),
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
        tracking_file = temp_dir / "doc2.tab.gax.md"
        result = _run_gax(
            "doc",
            "tab",
            "import",
            test_doc["url"],
            str(test_file),
            "-o",
            str(tracking_file),
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

    # NOTE: table_push_pull_cycle and rich_formatting_round_trip tests
    # moved to test_roundtrip.py (TestPushVerify + TestIdentityRoundTrip)


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
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
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
            },
        ).execute()

        # Clone the tab
        output_file = temp_dir / f"{tab_name}.sheet.gax.md"
        result = _run_gax(
            "sheet",
            "tab",
            "clone",
            test_sheet["url"],
            tab_name,
            "-o",
            str(output_file),
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
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
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
            },
        ).execute()

        # Clone
        output_file = temp_dir / f"{tab_name}.sheet.gax.md"
        result = _run_gax(
            "sheet",
            "tab",
            "clone",
            test_sheet["url"],
            tab_name,
            "-o",
            str(output_file),
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
        values = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=sheet_id,
                range=f"{tab_name}!A1:B2",
            )
            .execute()
        )

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
                "doc",
                "tab",
                "import",
                test_doc["url"],
                str(test_file),
                "-o",
                str(temp_dir / f"multi{i}.tab.gax.md"),
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
                body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
            ).execute()

            # Read fixture and parse markdown table
            fixture_content = (FIXTURES_DIR / fixture).read_text()
            lines = [
                ln
                for ln in fixture_content.strip().split("\n")
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
                body={"values": values},
            ).execute()

            # Clone
            output_file = temp_dir / f"{tab_name}.sheet.gax.md"
            result = _run_gax(
                "sheet",
                "tab",
                "clone",
                test_sheet["url"],
                tab_name,
                "-o",
                str(output_file),
            )
            assert result.returncode == 0, f"Sheet clone {i} failed: {result.stderr}"

        # Verify we have 4 tracking files
        tracking_files = list(temp_dir.glob("*.gax.md"))
        assert len(tracking_files) == 4


# =============================================================================
# Image roundtrip tests
# =============================================================================


@pytest.mark.e2e
class TestImageE2E:
    """End-to-end tests for image extraction and inlining."""

    def test_image_extraction_and_inlining(self, check_auth, temp_dir):
        """Test image extraction from blob store and inlining back."""
        from gax.gdoc.native_md import extract_images_to_store, inline_images_from_store
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

        match = re.search(r"base64,([A-Za-z0-9+/=]+)", inlined)
        assert match, "Should find base64 data"

        # Decode both and compare
        original_bytes = base64.b64decode(red_pixel_b64)
        roundtrip_bytes = base64.b64decode(match.group(1))
        assert original_bytes == roundtrip_bytes, (
            "Image data should match after roundtrip"
        )

    def test_image_pull_from_real_doc(self, check_auth, temp_dir):
        """Test pulling from a document that has images (Signals doc)."""
        # Use the Signals PC Briefing doc which has an image
        signals_doc_id = "1WhTCn_R7O2EavEedb9DWCmLH5QcrqP5tB-aaK3z8bm0"
        signals_url = f"https://docs.google.com/document/d/{signals_doc_id}/edit"

        # Clone the full doc
        output_file = temp_dir / "signals.doc.gax.md"
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
        event_file = temp_dir / "test_event.cal.gax.md"
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
        assert "Pushed event" in result.stdout

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
        event_file = temp_dir / "original.cal.gax.md"
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
        clone_file = temp_dir / "cloned.cal.gax.md"
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


# =============================================================================
# Draft E2E Tests
# =============================================================================


E2E_DRAFT_SUBJECT = f"{E2E_PREFIX}-draft"


def _cleanup_test_drafts():
    """Delete all drafts whose subject starts with the e2e prefix."""
    creds = get_authenticated_credentials()
    service = build("gmail", "v1", credentials=creds)
    result = service.users().drafts().list(userId="me", maxResults=100).execute()
    for draft_info in result.get("drafts", []):
        draft = (
            service.users()
            .drafts()
            .get(userId="me", id=draft_info["id"], format="metadata")
            .execute()
        )
        headers = draft.get("message", {}).get("payload", {}).get("headers", [])
        subject = ""
        for h in headers:
            if h["name"].lower() == "subject":
                subject = h["value"]
                break
        if subject.startswith(E2E_DRAFT_SUBJECT):
            service.users().drafts().delete(userId="me", id=draft_info["id"]).execute()


@pytest.mark.e2e
class TestDraftE2E:
    """End-to-end smoke tests for Gmail draft operations."""

    def test_new_push_pull_cycle(self, check_auth, temp_dir):
        """Test: new -> push -> modify -> push (diff) -> pull -> verify."""
        _cleanup_test_drafts()

        draft_file = temp_dir / "test.draft.gax.md"

        try:
            # Create new draft file
            result = _run_gax(
                "draft",
                "new",
                "--to",
                "test@example.com",
                "--subject",
                E2E_DRAFT_SUBJECT,
                "-o",
                str(draft_file),
            )
            assert result.returncode == 0, f"New failed: {result.stderr}"
            assert draft_file.exists()

            # Push to create in Gmail
            result = _run_gax("push", str(draft_file), "-y")
            assert result.returncode == 0, f"Push failed: {result.stderr}"

            # Verify draft_id was written back
            content = draft_file.read_text()
            assert "draft_id:" in content

            # Modify body
            draft_file.write_text(content.rstrip() + "\nHello from e2e test.\n")

            # Push update (with -y to skip confirm)
            result = _run_gax("push", str(draft_file), "-y")
            assert result.returncode == 0, f"Update push failed: {result.stderr}"

            # Pull back
            result = _run_gax("pull", str(draft_file))
            assert result.returncode == 0, f"Pull failed: {result.stderr}"

            pulled = draft_file.read_text()
            assert "Hello from e2e test." in pulled

            # Push with no changes should report no changes
            result = _run_gax("push", str(draft_file))
            assert "no changes" in result.stdout.lower() or result.returncode == 0

        finally:
            _cleanup_test_drafts()

    def test_list(self, check_auth, temp_dir):
        """Test: create draft -> list -> verify it appears in output."""
        _cleanup_test_drafts()

        draft_file = temp_dir / "list.draft.gax.md"

        try:
            # Create and push a draft so there's at least one
            result = _run_gax(
                "draft",
                "new",
                "--to",
                "test@example.com",
                "--subject",
                f"{E2E_DRAFT_SUBJECT}-list",
                "-o",
                str(draft_file),
            )
            assert result.returncode == 0, f"New failed: {result.stderr}"

            result = _run_gax("push", str(draft_file), "-y")
            assert result.returncode == 0, f"Push failed: {result.stderr}"

            # List drafts
            result = _run_gax("draft", "list", "--limit", "50")
            assert result.returncode == 0, f"List failed: {result.stderr}"

            # Should have TSV header
            assert "draft_id\t" in result.stdout
            assert "subject" in result.stdout

            # Our test draft should appear
            assert E2E_DRAFT_SUBJECT in result.stdout

        finally:
            _cleanup_test_drafts()

    def test_clone(self, check_auth, temp_dir):
        """Test: create draft -> push -> clone by ID -> verify content matches."""
        _cleanup_test_drafts()

        draft_file = temp_dir / "original.draft.gax.md"

        try:
            # Create and push a draft
            result = _run_gax(
                "draft",
                "new",
                "--to",
                "test@example.com",
                "--subject",
                f"{E2E_DRAFT_SUBJECT}-clone",
                "-o",
                str(draft_file),
            )
            assert result.returncode == 0, f"New failed: {result.stderr}"

            # Add body content before pushing
            content = draft_file.read_text()
            draft_file.write_text(content.rstrip() + "\nClone test body.\n")

            result = _run_gax("push", str(draft_file), "-y")
            assert result.returncode == 0, f"Push failed: {result.stderr}"

            # Extract draft_id from file
            import re

            content = draft_file.read_text()
            match = re.search(r"^draft_id:\s*(\S+)", content, re.MULTILINE)
            assert match, "draft_id not found after push"
            draft_id = match.group(1)

            # Clone by ID into a new file
            clone_file = temp_dir / "cloned.draft.gax.md"
            result = _run_gax(
                "draft",
                "clone",
                draft_id,
                "-o",
                str(clone_file),
            )
            assert result.returncode == 0, f"Clone failed: {result.stderr}"
            assert clone_file.exists()

            # Verify cloned content
            cloned = clone_file.read_text()
            assert f"{E2E_DRAFT_SUBJECT}-clone" in cloned
            assert "Clone test body." in cloned
            assert draft_id in cloned

        finally:
            _cleanup_test_drafts()

    def test_diff_shows_changes(self, check_auth, temp_dir):
        """Test: push draft -> modify locally -> diff shows changes."""
        _cleanup_test_drafts()

        draft_file = temp_dir / "diff.draft.gax.md"

        try:
            # Create and push
            result = _run_gax(
                "draft",
                "new",
                "--to",
                "test@example.com",
                "--subject",
                f"{E2E_DRAFT_SUBJECT}-diff",
                "-o",
                str(draft_file),
            )
            assert result.returncode == 0, f"New failed: {result.stderr}"

            result = _run_gax("push", str(draft_file), "-y")
            assert result.returncode == 0, f"Push failed: {result.stderr}"

            # Modify body locally
            content = draft_file.read_text()
            draft_file.write_text(content.rstrip() + "\nDiff test line.\n")

            # Push without -y: output should show the diff
            # (will auto-decline since no TTY, but we check stdout)
            result = _run_gax("draft", "push", str(draft_file))
            # Should show changes (diff output) or prompt
            assert "Diff test line" in result.stdout or result.returncode == 0

            # Use generic push with -y to confirm it works
            result = _run_gax("push", str(draft_file), "-y")
            assert result.returncode == 0, f"Push with changes failed: {result.stderr}"

            # Now push again — no changes
            result = _run_gax("push", str(draft_file))
            assert (
                "no changes" in result.stdout.lower()
                or "no diff" in result.stdout.lower()
            )

        finally:
            _cleanup_test_drafts()


# =============================================================================
# Contacts E2E Tests
# =============================================================================


def _delete_contact(resource_name: str):
    """Delete a contact via the People API directly."""
    creds = get_authenticated_credentials()
    service = build("people", "v1", credentials=creds)
    service.people().deleteContact(resourceName=resource_name).execute()


E2E_CONTACT_GIVEN = f"{E2E_PREFIX}contact"


def _find_contact_by_given_name(given_name: str) -> str | None:
    """Find a contact by givenName, return resourceName or None."""
    matches = _find_contacts_by_prefix(given_name)
    return matches[0] if matches else None


def _find_contacts_by_prefix(prefix: str) -> list[str]:
    """Find all contacts whose givenName starts with prefix. Returns resourceNames."""
    creds = get_authenticated_credentials()
    service = build("people", "v1", credentials=creds)
    matches = []
    page_token = None

    while True:
        result = (
            service.people()
            .connections()
            .list(
                resourceName="people/me",
                pageSize=500,
                personFields="names",
                pageToken=page_token,
            )
            .execute()
        )
        for c in result.get("connections", []):
            for n in c.get("names", []):
                if (n.get("givenName") or "").startswith(prefix):
                    matches.append(c["resourceName"])
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return matches


def _cleanup_test_contacts():
    """Delete all contacts matching the e2e test prefix."""
    for rn in _find_contacts_by_prefix(E2E_CONTACT_GIVEN):
        _delete_contact(rn)


@pytest.mark.e2e
class TestContactsE2E:
    """End-to-end smoke test for contacts: clone -> add -> push -> pull -> remove -> push -> pull."""

    def test_create_and_delete_contact(self, check_auth, temp_dir):
        """Test full create/delete cycle via push."""
        import time

        test_given = E2E_CONTACT_GIVEN
        test_family = "contact"
        contacts_file = temp_dir / "contacts.jsonl"

        # Clean up any leftover test contacts from previous runs
        _cleanup_test_contacts()

        try:
            # Clone contacts as JSONL
            result = _run_gax(
                "contacts",
                "clone",
                "-f",
                "jsonl",
                "-o",
                str(contacts_file),
            )
            assert result.returncode == 0, f"Clone failed: {result.stderr}"
            assert contacts_file.exists()

            # Read and verify structure
            content = contacts_file.read_text()
            assert "type: gax/contacts" in content
            assert "format: jsonl" in content

            # Count original contacts
            from gax.contacts import parse_contacts_file, parse_jsonl_body

            header, body = parse_contacts_file(contacts_file)
            original_contacts = parse_jsonl_body(body)
            original_count = len(original_contacts)

            # Add a new test contact
            new_contact = {
                "resourceName": "",
                "name": f"{test_given} {test_family}",
                "givenName": test_given,
                "familyName": test_family,
                "email": ["gax-e2e-test@example.com"],
                "phone": [],
                "organization": "",
                "title": "",
                "department": "",
                "address": "",
                "birthday": "",
                "notes": "",
                "nickname": "",
                "website": "",
                "labels": [],
            }
            original_contacts.append(new_contact)

            # Write back with new contact
            from gax.contacts import format_jsonl, format_contacts_file, ContactsHeader

            new_body = format_jsonl(original_contacts)
            new_header = ContactsHeader(
                format="jsonl",
                count=len(original_contacts),
                pulled=header.pulled,
            )
            contacts_file.write_text(
                format_contacts_file(new_header, new_body), encoding="utf-8"
            )

            # Push — should create the new contact
            result = _run_gax("contacts", "push", str(contacts_file), "-y")
            assert result.returncode == 0, f"Push (create) failed: {result.stderr}"

            # People API has propagation delay
            time.sleep(3)

            # Verify contact was created in Google
            resource_name = _find_contact_by_given_name(test_given)
            assert resource_name, "Contact was not created in Google"

            # Pull — should now include the contact with a resourceName
            result = _run_gax("contacts", "pull", str(contacts_file))
            assert result.returncode == 0, f"Pull failed: {result.stderr}"

            pulled_content = contacts_file.read_text()
            assert test_given in pulled_content

            # Parse pulled contacts, remove the test contact
            header, body = parse_contacts_file(contacts_file)
            pulled_contacts = parse_jsonl_body(body)
            filtered = [c for c in pulled_contacts if c.get("givenName") != test_given]
            assert len(filtered) == original_count, (
                "Test contact not found in pulled data"
            )

            # Write back without the test contact
            new_body = format_jsonl(filtered)
            new_header = ContactsHeader(
                format="jsonl",
                count=len(filtered),
                pulled=header.pulled,
            )
            contacts_file.write_text(
                format_contacts_file(new_header, new_body), encoding="utf-8"
            )

            # Push — should delete the test contact
            result = _run_gax("contacts", "push", str(contacts_file), "-y")
            assert result.returncode == 0, f"Push (delete) failed: {result.stderr}"

            # People API has propagation delay
            time.sleep(3)

            # Verify contact was deleted from Google
            resource_name = _find_contact_by_given_name(test_given)
            assert resource_name is None, "Contact was not deleted from Google"

        finally:
            # Safety cleanup — catches any leftovers from this or stale runs
            _cleanup_test_contacts()


# =============================================================================
# Label E2E Tests
# =============================================================================

E2E_LABEL_PREFIX = f"{E2E_PREFIX}-label"


def _find_test_labels() -> list[dict]:
    """Find all Gmail labels whose name starts with the e2e prefix."""
    creds = get_authenticated_credentials()
    service = build("gmail", "v1", credentials=creds)
    result = service.users().labels().list(userId="me").execute()
    return [
        lbl
        for lbl in result.get("labels", [])
        if lbl.get("name", "").startswith(E2E_LABEL_PREFIX)
    ]


def _cleanup_test_labels():
    """Delete all labels matching the e2e prefix."""
    creds = get_authenticated_credentials()
    service = build("gmail", "v1", credentials=creds)
    for lbl in _find_test_labels():
        try:
            service.users().labels().delete(userId="me", id=lbl["id"]).execute()
        except Exception:
            pass  # May already be deleted


@pytest.mark.e2e
class TestLabelE2E:
    """End-to-end smoke test for labels: clone -> add -> apply -> pull -> remove -> apply."""

    def test_create_and_delete_label(self, check_auth, temp_dir):
        """Test full create/delete cycle via push."""
        test_label_name = f"{E2E_LABEL_PREFIX}-test"
        labels_file = temp_dir / "labels.gax.md"

        # Clean up any leftover test labels
        _cleanup_test_labels()

        try:
            # Clone labels
            result = _run_gax(
                "mail-label",
                "clone",
                "-o",
                str(labels_file),
            )
            assert result.returncode == 0, f"Clone failed: {result.stderr}"
            assert labels_file.exists()

            # Read and parse
            from gax.label import parse_labels_file, format_labels_file

            header, labels = parse_labels_file(labels_file)
            original_count = len(labels)

            # Add a test label
            labels.append({"name": test_label_name})
            content = format_labels_file(header, labels)
            labels_file.write_text(content, encoding="utf-8")

            # Apply (push) — should create the label
            result = _run_gax("mail-label", "apply", str(labels_file), "-y")
            assert result.returncode == 0, f"Apply (create) failed: {result.stderr}"

            # Verify label was created
            test_labels = _find_test_labels()
            assert len(test_labels) == 1, (
                f"Expected 1 test label, found {len(test_labels)}"
            )
            assert test_labels[0]["name"] == test_label_name

            # Pull — should include the new label
            result = _run_gax("mail-label", "pull", str(labels_file))
            assert result.returncode == 0, f"Pull failed: {result.stderr}"

            pulled_content = labels_file.read_text()
            assert test_label_name in pulled_content

            # Remove the test label from file
            header, labels = parse_labels_file(labels_file)
            filtered = [lbl for lbl in labels if lbl.get("name") != test_label_name]
            assert len(filtered) == original_count, (
                "Test label not found in pulled data"
            )

            content = format_labels_file(header, filtered)
            labels_file.write_text(content, encoding="utf-8")

            # Apply with --delete — should delete the label
            result = _run_gax("mail-label", "apply", str(labels_file), "-y", "--delete")
            assert result.returncode == 0, f"Apply (delete) failed: {result.stderr}"

            # Verify label was deleted
            test_labels = _find_test_labels()
            assert len(test_labels) == 0, "Test label was not deleted"

        finally:
            _cleanup_test_labels()


# =============================================================================
# Filter E2E Tests
# =============================================================================

E2E_FILTER_QUERY = f"{E2E_PREFIX}-filter-query"


def _find_test_filters() -> list[dict]:
    """Find all Gmail filters whose query contains the e2e prefix."""
    creds = get_authenticated_credentials()
    service = build("gmail", "v1", credentials=creds)
    result = service.users().settings().filters().list(userId="me").execute()
    return [
        f
        for f in result.get("filter", [])
        if E2E_FILTER_QUERY in f.get("criteria", {}).get("query", "")
    ]


def _cleanup_test_filters():
    """Delete all filters matching the e2e query prefix."""
    creds = get_authenticated_credentials()
    service = build("gmail", "v1", credentials=creds)
    for f in _find_test_filters():
        try:
            service.users().settings().filters().delete(
                userId="me", id=f["id"]
            ).execute()
        except Exception:
            pass


@pytest.mark.e2e
class TestFilterE2E:
    """End-to-end smoke test for filters: clone -> add -> apply -> pull -> remove -> apply."""

    def test_create_and_delete_filter(self, check_auth, temp_dir):
        """Test full create/delete cycle via apply."""
        filters_file = temp_dir / "filters.gax.md"

        _cleanup_test_filters()

        try:
            # Clone filters
            result = _run_gax(
                "mail-filter",
                "clone",
                "-o",
                str(filters_file),
            )
            assert result.returncode == 0, f"Clone failed: {result.stderr}"
            assert filters_file.exists()

            # Read and parse
            from gax.filter import parse_filters_file, format_filters_file

            header, filters = parse_filters_file(filters_file)
            original_count = len(filters)

            # Add a test filter
            filters.append(
                {
                    "name": f"{E2E_PREFIX}-filter",
                    "criteria": {"query": E2E_FILTER_QUERY},
                    "action": {"archive": True},
                }
            )
            content = format_filters_file(header, filters)
            filters_file.write_text(content, encoding="utf-8")

            # Plan — should show one create
            result = _run_gax("mail-filter", "plan", str(filters_file))
            assert result.returncode == 0, f"Plan failed: {result.stderr}"
            assert "Create: 1" in result.stdout

            # Apply — should create the filter
            result = _run_gax("mail-filter", "apply", str(filters_file), "-y")
            assert result.returncode == 0, f"Apply (create) failed: {result.stderr}"

            # Verify filter was created via API
            test_filters = _find_test_filters()
            assert len(test_filters) == 1, (
                f"Expected 1 test filter, found {len(test_filters)}"
            )

            # Pull — should include the new filter
            result = _run_gax("mail-filter", "pull", str(filters_file))
            assert result.returncode == 0, f"Pull failed: {result.stderr}"

            pulled_content = filters_file.read_text()
            assert E2E_FILTER_QUERY in pulled_content

            # Plan after pull — should show no changes
            result = _run_gax("mail-filter", "plan", str(filters_file))
            assert result.returncode == 0, f"Plan (no changes) failed: {result.stderr}"
            assert "no changes" in result.stdout.lower()

            # Remove the test filter from file
            header, filters = parse_filters_file(filters_file)
            filtered = [
                f
                for f in filters
                if E2E_FILTER_QUERY not in f.get("criteria", {}).get("query", "")
            ]
            assert len(filtered) == original_count, (
                "Test filter not found in pulled data"
            )

            content = format_filters_file(header, filtered)
            filters_file.write_text(content, encoding="utf-8")

            # Apply — should delete the filter
            result = _run_gax("mail-filter", "apply", str(filters_file), "-y")
            assert result.returncode == 0, f"Apply (delete) failed: {result.stderr}"

            # Verify filter was deleted
            test_filters = _find_test_filters()
            assert len(test_filters) == 0, "Test filter was not deleted"

        finally:
            _cleanup_test_filters()

    def test_list(self, check_auth):
        """Test: list filters as TSV."""
        result = _run_gax("mail-filter", "list")
        assert result.returncode == 0, f"List failed: {result.stderr}"
        # Should have TSV header
        assert "id\t" in result.stdout
        assert "query" in result.stdout


# =============================================================================
# Form E2E Tests
# =============================================================================


def _get_test_form_id() -> str:
    """Get test form ID from environment."""
    form_id = os.environ.get("GAX_TEST_FORM")
    if not form_id:
        pytest.skip(
            "GAX_TEST_FORM not set. Add to .envrc:\n"
            '  export GAX_TEST_FORM="<your-test-form-id>"'
        )
    return form_id


@pytest.mark.e2e
class TestFormE2E:
    """End-to-end tests for Google Forms operations."""

    def test_clone_md(self, check_auth, temp_dir):
        """Test: clone form as markdown -> verify content."""
        form_id = _get_test_form_id()
        form_url = f"https://docs.google.com/forms/d/{form_id}/edit"

        output_file = temp_dir / "test.form.gax.md"
        result = _run_gax("form", "clone", form_url, "-o", str(output_file))
        assert result.returncode == 0, f"Clone failed: {result.stderr}"
        assert output_file.exists()

        content = output_file.read_text()
        assert "type: gax/form" in content
        assert "content-type: text/markdown" in content
        assert form_id in content

    def test_clone_yaml(self, check_auth, temp_dir):
        """Test: clone form as yaml -> verify round-trip format."""
        form_id = _get_test_form_id()
        form_url = f"https://docs.google.com/forms/d/{form_id}/edit"

        output_file = temp_dir / "test.form.gax.md"
        result = _run_gax(
            "form", "clone", form_url, "-f", "yaml", "-o", str(output_file)
        )
        assert result.returncode == 0, f"Clone failed: {result.stderr}"
        assert output_file.exists()

        content = output_file.read_text()
        assert "content-type: application/yaml" in content
        assert "items:" in content

    def test_clone_pull_cycle(self, check_auth, temp_dir):
        """Test: clone form -> pull -> verify content updated."""
        form_id = _get_test_form_id()
        form_url = f"https://docs.google.com/forms/d/{form_id}/edit"

        output_file = temp_dir / "test.form.gax.md"
        result = _run_gax(
            "form", "clone", form_url, "-f", "yaml", "-o", str(output_file)
        )
        assert result.returncode == 0, f"Clone failed: {result.stderr}"

        # Pull should succeed and update synced timestamp
        result = _run_gax("form", "pull", str(output_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        content = output_file.read_text()
        assert "type: gax/form" in content
        assert form_id in content

    def test_plan_no_changes(self, check_auth, temp_dir):
        """Test: clone yaml -> plan -> should report no changes."""
        form_id = _get_test_form_id()
        form_url = f"https://docs.google.com/forms/d/{form_id}/edit"

        output_file = temp_dir / "test.form.gax.md"
        result = _run_gax(
            "form", "clone", form_url, "-f", "yaml", "-o", str(output_file)
        )
        assert result.returncode == 0, f"Clone failed: {result.stderr}"

        # Plan should show no changes on freshly cloned form
        result = _run_gax("form", "plan", str(output_file))
        assert result.returncode == 0, f"Plan failed: {result.stderr}"
        assert "no changes" in result.stdout.lower()

    def test_unified_clone(self, check_auth, temp_dir):
        """Test: unified clone command dispatches to form clone."""
        form_id = _get_test_form_id()
        form_url = f"https://docs.google.com/forms/d/{form_id}/edit"

        output_file = temp_dir / "test.form.gax.md"
        result = _run_gax("clone", form_url, "-o", str(output_file))
        assert result.returncode == 0, f"Unified clone failed: {result.stderr}"
        assert output_file.exists()

        content = output_file.read_text()
        assert "type: gax/form" in content

    def test_unified_pull(self, check_auth, temp_dir):
        """Test: unified pull command works on form files."""
        form_id = _get_test_form_id()
        form_url = f"https://docs.google.com/forms/d/{form_id}/edit"

        output_file = temp_dir / "test.form.gax.md"
        result = _run_gax("form", "clone", form_url, "-o", str(output_file))
        assert result.returncode == 0, f"Clone failed: {result.stderr}"

        result = _run_gax("pull", str(output_file))
        assert result.returncode == 0, f"Unified pull failed: {result.stderr}"


# =============================================================================
# Mail Thread E2E Tests
# =============================================================================


def _find_a_thread_id() -> str:
    """Find a thread ID from the user's inbox for testing."""
    creds = get_authenticated_credentials()
    service = build("gmail", "v1", credentials=creds)
    result = service.users().threads().list(userId="me", maxResults=1).execute()
    threads = result.get("threads", [])
    if not threads:
        pytest.skip("No threads found in inbox")
    return threads[0]["id"]


@pytest.mark.e2e
class TestMailThreadE2E:
    """End-to-end tests for mail thread operations (clone, pull)."""

    def test_clone_pull_cycle(self, check_auth, temp_dir):
        """Test: find thread -> clone -> verify file -> pull -> verify update."""
        thread_id = _find_a_thread_id()

        # Clone thread
        output_file = temp_dir / "thread.mail.gax.md"
        result = _run_gax("mail", "clone", thread_id, "-o", str(output_file))
        assert result.returncode == 0, f"Clone failed: {result.stderr}"
        assert output_file.exists()

        content = output_file.read_text()
        assert "type: gax/mail" in content
        assert f"thread_id: {thread_id}" in content
        assert "section: 1" in content

        # Pull to refresh
        result = _run_gax("mail", "pull", str(output_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        # Content should still be valid after pull
        pulled = output_file.read_text()
        assert "type: gax/mail" in pulled
        assert f"thread_id: {thread_id}" in pulled

    def test_clone_auto_filename(self, check_auth, temp_dir):
        """Test: clone without -o generates filename from subject."""
        thread_id = _find_a_thread_id()

        # Clone without explicit output — runs in temp_dir
        result = subprocess.run(
            ["gax", "mail", "clone", thread_id],
            capture_output=True,
            text=True,
            cwd=str(temp_dir),
        )
        assert result.returncode == 0, f"Clone failed: {result.stderr}"

        # Should have created a .mail.gax.md file
        files = list(temp_dir.glob("*.mail.gax.md"))
        assert len(files) == 1, f"Expected 1 file, found: {files}"

    def test_pull_folder(self, check_auth, temp_dir):
        """Test: clone 2 threads -> pull folder -> both updated."""
        creds = get_authenticated_credentials()
        service = build("gmail", "v1", credentials=creds)
        result = service.users().threads().list(userId="me", maxResults=2).execute()
        threads = result.get("threads", [])
        if len(threads) < 2:
            pytest.skip("Need at least 2 threads")

        # Clone two threads into temp_dir
        for i, t in enumerate(threads):
            out = temp_dir / f"thread{i}.mail.gax.md"
            r = _run_gax("mail", "clone", t["id"], "-o", str(out))
            assert r.returncode == 0, f"Clone {i} failed: {r.stderr}"

        # Pull the folder
        result = _run_gax("mail", "pull", str(temp_dir))
        assert result.returncode == 0, f"Pull folder failed: {result.stderr}"

    def test_reply_creates_draft(self, check_auth, temp_dir):
        """Test: clone thread -> reply -> verify draft file created."""
        thread_id = _find_a_thread_id()

        # Clone thread first
        thread_file = temp_dir / "thread.mail.gax.md"
        result = _run_gax("mail", "clone", thread_id, "-o", str(thread_file))
        assert result.returncode == 0, f"Clone failed: {result.stderr}"

        # Reply from the file
        reply_file = temp_dir / "reply.draft.gax.md"
        result = _run_gax("mail", "reply", str(thread_file), "-o", str(reply_file))
        assert result.returncode == 0, f"Reply failed: {result.stderr}"
        assert reply_file.exists()

        reply_content = reply_file.read_text()
        assert "type: gax/draft" in reply_content
        assert "Re:" in reply_content or "re:" in reply_content.lower()


# =============================================================================
# Mailbox E2E Tests
# =============================================================================


@pytest.mark.e2e
class TestMailboxE2E:
    """End-to-end tests for mailbox operations (list, clone, pull)."""

    def test_list_threads(self, check_auth):
        """Test: mailbox lists threads as TSV."""
        result = _run_gax("mailbox", "--limit", "5")
        assert result.returncode == 0, f"List failed: {result.stderr}"

        # Should have TSV header
        lines = result.stdout.strip().split("\n")
        assert len(lines) >= 2, "Expected header + at least 1 thread"
        assert "thread_id\t" in lines[0]

    def test_clone_pull_cycle(self, check_auth, temp_dir):
        """Test: mailbox clone -> verify file -> pull -> verify update."""
        output_file = temp_dir / "test.gax.md"

        # Clone with small limit
        result = _run_gax(
            "mailbox",
            "clone",
            "-o",
            str(output_file),
            "-q",
            "in:inbox",
            "--limit",
            "5",
        )
        assert result.returncode == 0, f"Clone failed: {result.stderr}"
        assert output_file.exists()

        content = output_file.read_text()
        assert "type: gax/list" in content
        assert "query: in:inbox" in content
        assert "id\tfrom\t" in content  # TSV header in body

        # Pull to refresh
        result = _run_gax("mailbox", "pull", str(output_file))
        assert result.returncode == 0, f"Pull failed: {result.stderr}"

        pulled = output_file.read_text()
        assert "type: gax/list" in pulled

    def test_fetch_threads(self, check_auth, temp_dir):
        """Test: mailbox fetch downloads threads into folder."""
        output_dir = temp_dir / "fetched"

        result = _run_gax(
            "mailbox",
            "fetch",
            "-o",
            str(output_dir),
            "-q",
            "in:inbox",
            "--limit",
            "2",
        )
        assert result.returncode == 0, f"Fetch failed: {result.stderr}"
        assert output_dir.exists()

        # Should have created .mail.gax.md files
        files = list(output_dir.glob("*.mail.gax.md"))
        assert len(files) >= 1, f"Expected at least 1 file, found: {files}"


# =============================================================================
# Drive File E2E Tests
# =============================================================================


def _upload_test_file(content: str, name: str) -> str:
    """Upload a small text file to Google Drive. Returns file ID."""
    import io

    from googleapiclient.http import MediaIoBaseUpload

    creds = get_authenticated_credentials()
    service = build("drive", "v3", credentials=creds)

    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")),
        mimetype="text/plain",
    )
    file = (
        service.files()
        .create(
            body={"name": name},
            media_body=media,
            fields="id",
        )
        .execute()
    )
    return file["id"]


def _delete_drive_file(file_id: str) -> None:
    """Delete a file from Google Drive."""
    creds = get_authenticated_credentials()
    service = build("drive", "v3", credentials=creds)
    try:
        service.files().delete(fileId=file_id).execute()
    except Exception:
        pass


@pytest.mark.e2e
class TestDriveFileE2E:
    """End-to-end tests for Google Drive file operations (clone, pull)."""

    def test_clone_pull_cycle(self, check_auth, temp_dir):
        """Test: upload file -> clone -> verify -> pull -> verify."""
        file_id = _upload_test_file(
            f"{E2E_PREFIX} test content\nline 2\n",
            f"{E2E_PREFIX}_test.txt",
        )
        try:
            # Clone
            output = temp_dir / f"{E2E_PREFIX}_test.txt"
            result = _run_gax("file", "clone", file_id, "-o", str(output))
            assert result.returncode == 0, f"Clone failed: {result.stderr}"
            assert output.exists()

            content = output.read_text()
            assert f"{E2E_PREFIX} test content" in content
            assert "line 2" in content

            # Sidecar tracking file should exist
            tracking = Path(str(output) + ".gax.md")
            assert tracking.exists(), f"Missing tracking file: {tracking}"
            tracking_content = tracking.read_text()
            assert file_id in tracking_content

            # Pull to refresh
            result = _run_gax("file", "pull", str(output))
            assert result.returncode == 0, f"Pull failed: {result.stderr}"

            # Content should still be valid after pull
            pulled = output.read_text()
            assert f"{E2E_PREFIX} test content" in pulled

        finally:
            _delete_drive_file(file_id)

    def test_unified_push(self, check_auth, temp_dir):
        """Test: clone -> modify -> unified push -> verify sidecar path handling."""
        file_id = _upload_test_file(
            f"{E2E_PREFIX} original\n",
            f"{E2E_PREFIX}_push_test.txt",
        )
        try:
            # Clone
            output = temp_dir / f"{E2E_PREFIX}_push_test.txt"
            result = _run_gax("file", "clone", file_id, "-o", str(output))
            assert result.returncode == 0, f"Clone failed: {result.stderr}"

            # Modify local file
            output.write_text(f"{E2E_PREFIX} modified\n")

            # Push via unified command (exercises cli.py sidecar path stripping)
            tracking = Path(str(output) + ".gax.md")
            result = _run_gax("push", str(tracking), "-y")
            assert result.returncode == 0, f"Push failed: {result.stderr}"

            # Pull back and verify the update made it
            result = _run_gax("file", "pull", str(output))
            assert result.returncode == 0, f"Pull failed: {result.stderr}"
            assert f"{E2E_PREFIX} modified" in output.read_text()

        finally:
            _delete_drive_file(file_id)
