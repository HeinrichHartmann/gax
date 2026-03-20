"""Tests for Gmail sync functionality.

Uses mock service objects to test without hitting real Gmail API.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from gax.mail import (
    pull_thread,
    format_multipart,
    format_section,
    extract_thread_id,
)


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
                            {"name": "Date", "value": "Tue, 11 Mar 2025 08:00:00 -0700"},
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
        output_file = tmp_path / "Project_Update.mail.gax"
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
                            {"name": "Date", "value": "Wed, 12 Mar 2025 14:00:00 -0700"},
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
