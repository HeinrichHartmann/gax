"""Tests for Resource.from_url() and Resource.from_file() dispatch."""

import pytest

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
