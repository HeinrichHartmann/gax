"""Unit tests for contacts module — no API calls needed."""

from gax.contacts import (
    api_to_contact,
    contact_to_api,
    contact_diff,
    compare_contacts,
    format_jsonl,
    parse_jsonl_body,
    parse_contacts_file,
    format_contacts_file,
    ContactsHeader,
)


# =============================================================================
# Serialize/deserialize round-trip: api_to_contact <-> contact_to_api
# =============================================================================


SAMPLE_API_CONTACT = {
    "resourceName": "people/c123",
    "names": [
        {"displayName": "Alice Smith", "givenName": "Alice", "familyName": "Smith"}
    ],
    "emailAddresses": [{"value": "alice@example.com"}, {"value": "alice@work.com"}],
    "phoneNumbers": [{"value": "+1-555-0100"}],
    "organizations": [{"name": "Acme Corp", "title": "Engineer", "department": "R&D"}],
    "addresses": [{"formattedValue": "123 Main St"}],
    "birthdays": [{"date": {"year": 1990, "month": 3, "day": 15}}],
    "biographies": [{"value": "Likes cats"}],
    "nicknames": [{"value": "Ali"}],
    "urls": [{"value": "https://alice.dev"}],
    "memberships": [
        {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/abc"}},
    ],
}

SAMPLE_GROUPS = {"contactGroups/abc": "Friends"}


class TestApiRoundTrip:
    """api_to_contact and contact_to_api should round-trip cleanly."""

    def test_api_to_contact_basic_fields(self):
        normalized = api_to_contact(SAMPLE_API_CONTACT, SAMPLE_GROUPS)
        assert normalized["name"] == "Alice Smith"
        assert normalized["givenName"] == "Alice"
        assert normalized["familyName"] == "Smith"
        assert normalized["email"] == ["alice@example.com", "alice@work.com"]
        assert normalized["phone"] == ["+1-555-0100"]
        assert normalized["organization"] == "Acme Corp"
        assert normalized["title"] == "Engineer"
        assert normalized["department"] == "R&D"
        assert normalized["address"] == "123 Main St"
        assert normalized["birthday"] == "1990-03-15"
        assert normalized["notes"] == "Likes cats"
        assert normalized["nickname"] == "Ali"
        assert normalized["website"] == "https://alice.dev"
        assert normalized["labels"] == ["Friends"]
        assert normalized["resourceName"] == "people/c123"

    def test_round_trip_preserves_fields(self):
        """contact_to_api(api_to_contact(x)) should produce equivalent API data."""
        normalized = api_to_contact(SAMPLE_API_CONTACT, SAMPLE_GROUPS)
        groups_by_name = {v: k for k, v in SAMPLE_GROUPS.items()}
        rebuilt = contact_to_api(normalized, groups_by_name)

        # Names (displayName is derived, so only given/family survive)
        assert rebuilt["names"][0]["givenName"] == "Alice"
        assert rebuilt["names"][0]["familyName"] == "Smith"

        # Lists
        assert [e["value"] for e in rebuilt["emailAddresses"]] == [
            "alice@example.com",
            "alice@work.com",
        ]
        assert [p["value"] for p in rebuilt["phoneNumbers"]] == ["+1-555-0100"]

        # Organization
        assert rebuilt["organizations"][0]["name"] == "Acme Corp"
        assert rebuilt["organizations"][0]["title"] == "Engineer"

        # Scalars
        assert rebuilt["addresses"][0]["formattedValue"] == "123 Main St"
        assert rebuilt["birthdays"][0]["date"] == {"year": 1990, "month": 3, "day": 15}
        assert rebuilt["biographies"][0]["value"] == "Likes cats"
        assert rebuilt["nicknames"][0]["value"] == "Ali"
        assert rebuilt["urls"][0]["value"] == "https://alice.dev"

        # Labels
        assert (
            rebuilt["memberships"][0]["contactGroupMembership"][
                "contactGroupResourceName"
            ]
            == "contactGroups/abc"
        )

    def test_empty_contact_round_trip(self):
        """Empty API contact should round-trip without errors."""
        normalized = api_to_contact({}, {})
        rebuilt = contact_to_api(normalized, {})
        # Empty contact produces no API fields
        assert rebuilt == {}

    def test_birthday_month_day_only(self):
        """Birthday without year should use -- prefix."""
        api = {"birthdays": [{"date": {"month": 12, "day": 25}}]}
        normalized = api_to_contact(api, {})
        assert normalized["birthday"] == "--12-25"

        rebuilt = contact_to_api(normalized, {})
        assert rebuilt["birthdays"][0]["date"] == {"month": 12, "day": 25}


# =============================================================================
# Comparison / diff
# =============================================================================


class TestContactDiff:
    def test_no_diff_identical(self):
        c = {"name": "Alice", "email": ["a@b.com"], "phone": [], "labels": []}
        assert contact_diff(c, c) == {}

    def test_diff_scalar_change(self):
        local = {"name": "Alice B", "email": [], "phone": [], "labels": []}
        remote = {"name": "Alice A", "email": [], "phone": [], "labels": []}
        d = contact_diff(local, remote)
        assert "name" in d
        assert d["name"]["from"] == "Alice A"
        assert d["name"]["to"] == "Alice B"

    def test_diff_list_order_independent(self):
        local = {"name": "", "email": ["b@x.com", "a@x.com"], "phone": [], "labels": []}
        remote = {
            "name": "",
            "email": ["a@x.com", "b@x.com"],
            "phone": [],
            "labels": [],
        }
        assert contact_diff(local, remote) == {}

    def test_compare_creates_updates_deletes(self):
        local = [
            {"resourceName": "people/1", "name": "Updated"},
            {"resourceName": "", "name": "New Contact"},
        ]
        remote = [
            {"resourceName": "people/1", "name": "Original"},
            {"resourceName": "people/2", "name": "Deleted"},
        ]
        creates, updates, deletes = compare_contacts(local, remote)
        assert len(creates) == 1
        assert creates[0]["name"] == "New Contact"
        assert len(updates) == 1
        assert updates[0][0]["name"] == "Updated"
        assert len(deletes) == 1
        assert deletes[0]["name"] == "Deleted"


# =============================================================================
# File format
# =============================================================================


class TestFileFormat:
    def test_jsonl_round_trip(self):
        contacts = [
            {"name": "Alice", "email": ["a@b.com"]},
            {"name": "Bob", "email": []},
        ]
        body = format_jsonl(contacts)
        parsed = parse_jsonl_body(body)
        assert len(parsed) == 2
        assert parsed[0]["name"] == "Alice"
        assert parsed[1]["name"] == "Bob"

    def test_header_round_trip(self, tmp_path):
        header = ContactsHeader(format="jsonl", count=42, pulled="2026-01-01T00:00:00Z")
        body = "line1\nline2"
        content = format_contacts_file(header, body)

        path = tmp_path / "test.contacts.gax.md"
        path.write_text(content, encoding="utf-8")

        parsed_header, parsed_body = parse_contacts_file(path)
        assert parsed_header.format == "jsonl"
        assert parsed_header.count == 42
        assert parsed_header.pulled == "2026-01-01T00:00:00Z"
        assert parsed_body.strip() == body
