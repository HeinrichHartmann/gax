"""Tests for Gmail sync functionality.

Uses mock service objects to test without hitting real Gmail API.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gax.mail import (
    MailSection,
    pull_thread,
    format_multipart,
    format_section,
    extract_thread_id,
)
from gax.mail.thread import Thread, _is_thread_id


# Load fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    """Load a fixture file as string."""
    return (FIXTURES_DIR / name).read_text()


def make_mock_service(thread_response: dict):
    """Create a mock Gmail service that returns the given thread."""
    service = MagicMock()
    service.users().threads().get().execute.return_value = thread_response
    return service


class TestExtractThreadId:
    """Tests for thread ID extraction from various URL formats."""

    def test_inbox_url(self):
        """Test extraction from standard inbox URL."""
        url = "https://mail.google.com/mail/u/0/#inbox/FMfcgzQXJWDsKmvPLCdfvxhHXqhSwBZV"
        assert extract_thread_id(url) == "FMfcgzQXJWDsKmvPLCdfvxhHXqhSwBZV"

    def test_popout_url_encoded(self):
        """Test extraction from popout URL with encoded thread-f."""
        url = "https://mail.google.com/mail/u/0/?tab=rm&ogbl#thread-f%3A1859907402038417535"
        assert extract_thread_id(url) == "1859907402038417535"

    def test_popout_url_decoded(self):
        """Test extraction from popout URL with decoded thread-f."""
        url = "https://mail.google.com/mail/u/0/#thread-f:1859907402038417535"
        assert extract_thread_id(url) == "1859907402038417535"

    def test_raw_alphanumeric_id(self):
        """Test raw alphanumeric thread ID."""
        thread_id = "FMfcgzQXJWDsKmvPLCdfvxhHXqhSwBZV"
        assert extract_thread_id(thread_id) == thread_id

    def test_raw_numeric_id(self):
        """Test raw numeric thread ID."""
        thread_id = "1859907402038417535"
        assert extract_thread_id(thread_id) == thread_id


class TestPullThread:
    """Tests for pull_thread function."""

    def test_two_message_thread(self):
        """Test pulling a thread with two messages."""
        thread_response = json.loads(load_fixture("sample_thread_response.json"))
        service = make_mock_service(thread_response)

        sections = pull_thread("thread-abc123", service=service)

        # Should have 2 sections (one per message)
        assert len(sections) == 2

        # Check first section (from Alice)
        assert sections[0].title == "Project Update"
        assert sections[0].thread_id == "thread-abc123"
        assert sections[0].section == 1
        assert "Alice Smith" in sections[0].section_title
        assert sections[0].from_addr == "Alice Smith <alice@example.com>"
        assert sections[0].to_addr == "Bob Jones <bob@example.com>"
        assert "project update" in sections[0].content.lower()

        # Check second section (from Bob)
        assert sections[1].title == "Project Update"
        assert sections[1].section == 2
        assert "Bob Jones" in sections[1].section_title
        assert sections[1].from_addr == "Bob Jones <bob@example.com>"
        assert "discuss tomorrow" in sections[1].content.lower()

    def test_single_message_thread(self):
        """Test pulling a thread with a single message."""
        thread_response = {
            "id": "single-thread",
            "messages": [
                {
                    "id": "msg-single",
                    "threadId": "single-thread",
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "From", "value": "sender@example.com"},
                            {"name": "To", "value": "recipient@example.com"},
                            {"name": "Subject", "value": "Hello"},
                            {
                                "name": "Date",
                                "value": "Tue, 11 Mar 2025 08:00:00 -0700",
                            },
                        ],
                        "body": {
                            "data": "SGVsbG8gV29ybGQh"  # "Hello World!"
                        },
                    },
                }
            ],
        }
        service = make_mock_service(thread_response)

        sections = pull_thread("single-thread", service=service)

        assert len(sections) == 1
        assert sections[0].title == "Hello"
        assert "Hello World!" in sections[0].content


class TestFormatMultipart:
    """Tests for multipart format output."""

    def test_format_thread_to_file(self, tmp_path):
        """Test formatting a thread and writing to file."""
        thread_response = json.loads(load_fixture("sample_thread_response.json"))
        service = make_mock_service(thread_response)

        sections = pull_thread("thread-abc123", service=service)
        content = format_multipart(sections)

        # Write to temp file
        output_file = tmp_path / "Project_Update.mail.gax.md"
        output_file.write_text(content)

        # Verify file contents
        written = output_file.read_text()

        # Should have two sections with YAML headers
        assert written.count("---\n") >= 4  # At least 2 sections x 2 delimiters
        assert "title: Project Update" in written
        assert "thread_id: thread-abc123" in written
        assert "section: 1" in written
        assert "section: 2" in written

        # Email headers should be present
        assert "from: Alice Smith" in written
        assert "from: Bob Jones" in written
        assert "to: Bob Jones" in written
        assert "to: Alice Smith" in written

        # Content should be present
        assert "project update" in written.lower()
        assert "discuss tomorrow" in written.lower()

    def test_sections_are_self_contained(self):
        """Test that each section can be extracted as standalone."""
        thread_response = json.loads(load_fixture("sample_thread_response.json"))
        service = make_mock_service(thread_response)

        sections = pull_thread("thread-abc123", service=service)

        # Each section should have full metadata
        for section in sections:
            assert section.title == "Project Update"
            assert "mail.google.com" in section.source
            assert section.time  # Should have timestamp
            assert section.thread_id
            assert section.from_addr
            assert section.to_addr

        # Format each section individually
        for i, section in enumerate(sections):
            single = format_section(section)
            assert single.startswith("---\n")
            assert "title: Project Update" in single
            assert f"section: {i + 1}" in single


class TestMultipartMimeTypes:
    """Tests for different MIME type handling."""

    def test_multipart_alternative(self):
        """Test extracting text from multipart/alternative."""
        thread_response = {
            "id": "multipart-thread",
            "messages": [
                {
                    "id": "msg-mp",
                    "threadId": "multipart-thread",
                    "payload": {
                        "mimeType": "multipart/alternative",
                        "headers": [
                            {"name": "From", "value": "sender@example.com"},
                            {"name": "To", "value": "recipient@example.com"},
                            {"name": "Subject", "value": "Multipart Test"},
                            {
                                "name": "Date",
                                "value": "Wed, 12 Mar 2025 14:00:00 -0700",
                            },
                        ],
                        "parts": [
                            {
                                "mimeType": "text/plain",
                                "body": {
                                    "data": "UGxhaW4gdGV4dCB2ZXJzaW9u"  # "Plain text version"
                                },
                            },
                            {
                                "mimeType": "text/html",
                                "body": {
                                    "data": "PHA+SFRNTCB2ZXJzaW9uPC9wPg=="  # "<p>HTML version</p>"
                                },
                            },
                        ],
                    },
                }
            ],
        }
        service = make_mock_service(thread_response)

        sections = pull_thread("multipart-thread", service=service)

        assert len(sections) == 1
        # Should prefer text/plain over text/html
        assert "Plain text version" in sections[0].content
        assert "HTML" not in sections[0].content


# 20+ alphanumeric chars pass _is_thread_id validation
THREAD_ID = "TestThread00000000001"
THREAD_ID_2 = "TestThread00000000002"


# =============================================================================
# Helper to build MailSection fixtures
# =============================================================================


def _make_section(
    thread_id=THREAD_ID,
    section_num=1,
    from_addr="Alice <alice@test.com>",
    to_addr="Bob <bob@test.com>",
    subject="Test Subject",
    date="2025-03-10T09:30:00Z",
    content="Hello there.",
):
    return MailSection(
        title=subject,
        source=f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
        time="2025-03-10T16:30:00Z",
        thread_id=thread_id,
        section=section_num,
        section_title=f"From {from_addr.split('<')[0].strip()}",
        from_addr=from_addr,
        to_addr=to_addr,
        date=date,
        content=content,
    )


# =============================================================================
# _is_thread_id tests
# =============================================================================


class TestIsThreadId:
    def test_gmail_url(self):
        assert _is_thread_id("https://mail.google.com/mail/u/0/#inbox/abc123") is True

    def test_hex_id(self):
        assert _is_thread_id("18f3a2b4c5d6e7f0") is True

    def test_alphanumeric_id(self):
        assert _is_thread_id("FMfcgzQXJWDsKmvPLCdfvx") is True

    def test_numeric_id(self):
        assert _is_thread_id("1859907402038417535") is True

    def test_search_query(self):
        assert _is_thread_id("from:alice subject:hello") is False

    def test_short_string(self):
        assert _is_thread_id("hello") is False


# =============================================================================
# Thread.clone tests
# =============================================================================


class TestThreadClone:
    def test_creates_file(self, tmp_path, monkeypatch):
        sections = [_make_section()]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)

        path = Thread().clone(THREAD_ID, output=tmp_path / "test.mail.gax.md")

        assert path.exists()
        content = path.read_text()
        assert f"thread_id: {THREAD_ID}" in content
        assert "Test Subject" in content
        assert "Hello there." in content

    def test_multi_message_thread(self, tmp_path, monkeypatch):
        sections = [
            _make_section(section_num=1, from_addr="Alice <alice@test.com>"),
            _make_section(
                section_num=2,
                from_addr="Bob <bob@test.com>",
                content="Got it, thanks!",
            ),
        ]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)

        path = Thread().clone(THREAD_ID, output=tmp_path / "test.mail.gax.md")
        content = path.read_text()

        assert "section: 1" in content
        assert "section: 2" in content
        assert "Hello there." in content
        assert "Got it, thanks!" in content

    def test_rejects_search_query(self):
        with pytest.raises(ValueError, match="not a valid"):
            Thread().clone("from:alice subject:hello")

    def test_existing_file_raises(self, tmp_path, monkeypatch):
        output = tmp_path / "test.mail.gax.md"
        output.write_text("existing content")
        sections = [_make_section()]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)

        with pytest.raises(ValueError, match="already exists"):
            Thread().clone(THREAD_ID, output=output)

    def test_default_filename(self, tmp_path, monkeypatch):
        sections = [_make_section(subject="Weekly Sync")]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)
        monkeypatch.chdir(tmp_path)

        path = Thread().clone(THREAD_ID)
        assert "Weekly_Sync" in path.name
        assert path.name.endswith(".mail.gax.md")


# =============================================================================
# Thread.pull tests
# =============================================================================


class TestThreadPull:
    def test_updates_single_file(self, tmp_path, monkeypatch):
        # Clone initial version
        sections = [_make_section(section_num=1, content="Original message")]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)
        path = Thread().clone(THREAD_ID, output=tmp_path / "test.mail.gax.md")

        # Pull with new reply
        updated = [
            _make_section(section_num=1, content="Original message"),
            _make_section(section_num=2, content="New reply"),
        ]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: updated)
        Thread().pull(path)

        content = path.read_text()
        assert "section: 2" in content
        assert "New reply" in content

    def test_pull_directory(self, tmp_path, monkeypatch):
        # Create two thread files
        s1 = [_make_section(thread_id=THREAD_ID, content="Thread one")]
        s2 = [_make_section(thread_id=THREAD_ID_2, content="Thread two")]

        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: s1)
        Thread().clone(THREAD_ID, output=tmp_path / "t1.mail.gax.md")

        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: s2)
        Thread().clone(THREAD_ID_2, output=tmp_path / "t2.mail.gax.md")

        # Pull whole directory (both get refreshed)
        def mock_pull(tid):
            if tid == THREAD_ID:
                return s1 + [
                    _make_section(
                        thread_id=THREAD_ID, section_num=2, content="T1 reply"
                    )
                ]
            return s2

        monkeypatch.setattr("gax.mail.thread.pull_thread", mock_pull)
        Thread().pull(tmp_path)

        assert "T1 reply" in (tmp_path / "t1.mail.gax.md").read_text()

    def test_pull_no_files_raises(self, tmp_path):
        with pytest.raises(ValueError, match="No .mail.gax.md files"):
            Thread().pull(tmp_path)


# =============================================================================
# Thread.diff tests
# =============================================================================


class TestThreadDiff:
    def test_no_changes(self, tmp_path, monkeypatch):
        sections = [_make_section(content="Hello there.")]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)
        path = Thread().clone(THREAD_ID, output=tmp_path / "test.mail.gax.md")

        # Same sections on remote
        result = Thread().diff(path)
        assert result is None

    def test_new_messages(self, tmp_path, monkeypatch):
        sections = [_make_section(section_num=1)]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)
        path = Thread().clone(THREAD_ID, output=tmp_path / "test.mail.gax.md")

        # Remote now has a second message
        updated = sections + [
            _make_section(
                section_num=2,
                from_addr="Bob <bob@test.com>",
                date="2025-03-10T10:15:00Z",
                content="Thanks for the update!",
            ),
        ]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: updated)

        result = Thread().diff(path)
        assert result is not None
        assert "1 -> 2" in result
        assert "Bob" in result
        assert "Thanks for the update!" in result

    def test_multiple_new_messages(self, tmp_path, monkeypatch):
        sections = [_make_section(section_num=1)]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)
        path = Thread().clone(THREAD_ID, output=tmp_path / "test.mail.gax.md")

        updated = sections + [
            _make_section(section_num=2, from_addr="Bob <bob@test.com>"),
            _make_section(section_num=3, from_addr="Carol <carol@test.com>"),
        ]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: updated)

        result = Thread().diff(path)
        assert "1 -> 3" in result
        assert "Bob" in result
        assert "Carol" in result

    def test_content_changed(self, tmp_path, monkeypatch):
        sections = [_make_section(content="Original text.")]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)
        path = Thread().clone(THREAD_ID, output=tmp_path / "test.mail.gax.md")

        # Same count but different content
        changed = [_make_section(content="Edited text.")]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: changed)

        result = Thread().diff(path)
        assert result is not None
        assert "content changed" in result

    def test_long_preview_truncated(self, tmp_path, monkeypatch):
        sections = [_make_section(section_num=1)]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: sections)
        path = Thread().clone(THREAD_ID, output=tmp_path / "test.mail.gax.md")

        long_body = "x" * 300
        updated = sections + [_make_section(section_num=2, content=long_body)]
        monkeypatch.setattr("gax.mail.thread.pull_thread", lambda tid: updated)

        result = Thread().diff(path)
        assert "..." in result
        assert len(result) < 400

    def test_missing_thread_id_raises(self, tmp_path):
        file = tmp_path / "bad.mail.gax.md"
        file.write_text("---\ntype: gax/mail\n---\nno thread id here\n")
        with pytest.raises(ValueError, match="No thread_id"):
            Thread().diff(file)
