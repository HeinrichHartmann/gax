"""Tests for Resource.from_url() and Resource.from_file() dispatch."""

import pytest

from gax.contacts import Contacts
from gax.draft import Draft
from gax.filter import Filter
from gax.form import Form
from gax.gcal import Cal, Event
from gax.gdoc import Doc, Tab
from gax.gdrive import File
from gax.gsheet import Sheet, SheetTab
from gax.gslides import Presentation, Slide
from gax.gtask import Task, TaskList
from gax.label import Label
from gax.mail import Mailbox, Thread
from gax.resource import Resource

# Import CLI to trigger all resource module registration via __init_subclass__
import gax.cli  # noqa: F401


# =============================================================================
# Resource.from_url — URL dispatch
# =============================================================================


class TestFromUrl:
    """Resource.from_url(url) dispatches to correct subclass."""

    def test_google_docs(self):
        r = Resource.from_url("https://docs.google.com/document/d/abc123/edit")
        assert r.__class__.__name__ == "Tab"

    def test_google_sheets(self):
        r = Resource.from_url("https://docs.google.com/spreadsheets/d/abc123/edit")
        assert r.__class__.__name__ == "SheetTab"

    def test_google_forms(self):
        r = Resource.from_url("https://docs.google.com/forms/d/abc123/edit")
        assert r.__class__.__name__ == "Form"

    def test_google_slides(self):
        r = Resource.from_url(
            "https://docs.google.com/presentation/d/abc123/edit"
        )
        assert r.__class__.__name__ == "Presentation"

    def test_google_drive(self):
        r = Resource.from_url("https://drive.google.com/file/d/abc123/view")
        assert r.__class__.__name__ == "File"

    def test_gmail_draft(self):
        r = Resource.from_url(
            "https://mail.google.com/mail/u/0/#drafts/r-1234567890"
        )
        assert r.__class__.__name__ == "Draft"

    def test_gmail_thread(self):
        r = Resource.from_url(
            "https://mail.google.com/mail/u/0/#inbox/18abc123"
        )
        assert r.__class__.__name__ == "Thread"

    def test_calendar(self):
        r = Resource.from_url(
            "https://calendar.google.com/calendar/event?eid=abc123"
        )
        assert r.__class__.__name__ in ("Event", "Cal")

    def test_draft_before_thread(self):
        """Draft URL must NOT match Thread (both match mail.google.com)."""
        r = Resource.from_url(
            "https://mail.google.com/mail/u/0/#drafts/r-9999"
        )
        assert r.__class__.__name__ == "Draft"

    def test_unrecognized_url(self):
        with pytest.raises(ValueError, match="Unrecognized URL"):
            Resource.from_url("https://example.com/foo")

    def test_url_stored_on_instance(self):
        r = Resource.from_url("https://docs.google.com/document/d/abc123/edit")
        assert r.url == "https://docs.google.com/document/d/abc123/edit"


# =============================================================================
# Resource.from_file — file dispatch
# =============================================================================


class TestFromFile:
    """Resource.from_file(path) dispatches to correct subclass."""

    def test_doc_by_extension(self, tmp_path):
        f = tmp_path / "test.doc.gax.md"
        f.write_text("---\ntype: gax/doc\n---\n# Hello\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Tab"

    def test_tab_by_extension(self, tmp_path):
        f = tmp_path / "test.tab.gax.md"
        f.write_text("---\ntype: gax/doc\n---\n# Hello\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Tab"

    def test_sheet_tab_by_extension(self, tmp_path):
        f = tmp_path / "test.sheet.gax.md"
        f.write_text("---\nspreadsheet_id: abc\ntab: Sheet1\n---\ndata\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "SheetTab"

    def test_draft_by_extension(self, tmp_path):
        f = tmp_path / "test.draft.gax.md"
        f.write_text("---\ntype: gax/draft\n---\nBody\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Draft"

    def test_mail_by_extension(self, tmp_path):
        f = tmp_path / "test.mail.gax.md"
        f.write_text("---\ntype: gax/mail\nthread_id: abc\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Thread"

    def test_cal_by_extension(self, tmp_path):
        f = tmp_path / "test.cal.gax.md"
        f.write_text("---\ntype: gax/cal\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Event"

    def test_form_by_extension(self, tmp_path):
        f = tmp_path / "test.form.gax.md"
        f.write_text("---\ntype: gax/form\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Form"

    def test_slides_by_extension(self, tmp_path):
        f = tmp_path / "test.slides.gax.md"
        f.write_text("---\ntype: gax/slides\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Slide"

    def test_task_by_extension(self, tmp_path):
        f = tmp_path / "test.task.gax.yaml"
        f.write_text("id: abc\ntasklist: TL1\ntitle: Test\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Task"

    def test_task_list_by_extension(self, tmp_path):
        f = tmp_path / "test.tasks.gax.yaml"
        f.write_text("---\ntype: gax/task-list\nid: TL1\ntitle: My List\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "TaskList"

    def test_contacts_by_extension(self, tmp_path):
        f = tmp_path / "all.contacts.gax.md"
        f.write_text("---\ntype: gax/contacts\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Contacts"

    def test_calendar_list_by_type(self, tmp_path):
        f = tmp_path / "calendar.cal.gax.md"
        f.write_text("---\ntype: gax/cal-list\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Cal"

    def test_filter_by_type(self, tmp_path):
        f = tmp_path / "filters.gax.md"
        f.write_text("---\ntype: gax/filters\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Filter"

    def test_label_by_type(self, tmp_path):
        f = tmp_path / "labels.gax.md"
        f.write_text("---\ntype: gax/labels\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Label"

    def test_mailbox_by_query_header(self, tmp_path):
        f = tmp_path / "inbox.mailbox.gax.md"
        f.write_text("---\nquery: in:inbox\n---\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "Mailbox"

    def test_file_by_sidecar(self, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_text("data")
        tracking = tmp_path / "report.pdf.gax.md"
        tracking.write_text("type: gax/file\nfile_id: abc123\n")
        r = Resource.from_file(f)
        assert r.__class__.__name__ == "File"
        assert r.path == f

    def test_doc_checkout_folder(self, tmp_path):
        folder = tmp_path / "doc.doc.gax.md.d"
        folder.mkdir()
        (folder / ".gax.yaml").write_text("type: gax/doc-checkout\n")
        r = Resource.from_file(folder)
        assert r.__class__.__name__ == "Doc"

    def test_sheet_checkout_folder(self, tmp_path):
        folder = tmp_path / "sheet.sheet.gax.md.d"
        folder.mkdir()
        (folder / ".gax.yaml").write_text("type: gax/sheet-checkout\n")
        r = Resource.from_file(folder)
        assert r.__class__.__name__ == "Sheet"

    def test_presentation_checkout_folder(self, tmp_path):
        folder = tmp_path / "deck.slides.gax.md.d"
        folder.mkdir()
        (folder / ".gax.yaml").write_text("type: gax/slides-checkout\n")
        r = Resource.from_file(folder)
        assert r.__class__.__name__ == "Presentation"

    def test_path_stored_on_instance(self, tmp_path):
        f = tmp_path / "test.draft.gax.md"
        f.write_text("---\ntype: gax/draft\n---\nBody\n")
        r = Resource.from_file(f)
        assert r.path == f

    def test_unknown_file_raises(self, tmp_path):
        f = tmp_path / "unknown.txt"
        f.write_text("just a text file")
        with pytest.raises(ValueError, match="Unknown file type"):
            Resource.from_file(f)

    def test_nonexistent_file_raises(self, tmp_path):
        with pytest.raises(ValueError):
            Resource.from_file(tmp_path / "does_not_exist.gax.md")


@pytest.mark.parametrize(
    ("cls", "raw_id"),
    [
        (Draft, "r-123456"),
        (Event, "evt_123"),
        (File, "drive_123"),
        (Form, "form123"),
        (Tab, "doc123"),
        (Doc, "doc123"),
        (SheetTab, "sheet123"),
        (Sheet, "sheet123"),
        (Presentation, "pres123"),
    ],
)
def test_subclass_from_url_accepts_raw_ids(cls, raw_id):
    r = cls.from_url(raw_id)
    assert isinstance(r, cls)
    assert r.url == raw_id


@pytest.mark.parametrize(
    ("cls", "folder_type"),
    [
        (Doc, "gax/doc-checkout"),
        (Sheet, "gax/sheet-checkout"),
        (Presentation, "gax/slides-checkout"),
    ],
)
def test_subclass_from_file_accepts_checkout_dirs(cls, folder_type, tmp_path):
    folder = tmp_path / "checkout.gax.md.d"
    folder.mkdir()
    (folder / ".gax.yaml").write_text(f"type: {folder_type}\n")

    r = cls.from_file(folder)
    assert isinstance(r, cls)
    assert r.path == folder


def test_registry_covers_all_resource_subclasses():
    expected = {
        Contacts,
        Draft,
        Cal,
        Event,
        Filter,
        File,
        Form,
        Label,
        Mailbox,
        Thread,
        Task,
        TaskList,
        Tab,
        Doc,
        SheetTab,
        Sheet,
        Slide,
        Presentation,
    }
    assert expected.issubset(set(Resource._subclasses))
