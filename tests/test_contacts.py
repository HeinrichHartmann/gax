"""Unit tests for contacts module — no API calls needed."""

from unittest.mock import patch

import yaml

from gax.contacts import (
    Contacts,
    api_to_contact,
    contact_to_api,
    contact_to_yaml,
    yaml_to_contact,
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


# =============================================================================
# Individual contact YAML round-trip: contact_to_yaml <-> yaml_to_contact
# =============================================================================


class TestContactYamlRoundTrip:
    def test_round_trip(self):
        normalized = api_to_contact(SAMPLE_API_CONTACT, SAMPLE_GROUPS)
        yaml_content = contact_to_yaml(normalized)
        parsed = yaml_to_contact(yaml_content)

        assert parsed["resourceName"] == "people/c123"
        assert parsed["name"] == "Alice Smith"
        assert parsed["givenName"] == "Alice"
        assert parsed["familyName"] == "Smith"
        assert parsed["email"] == ["alice@example.com", "alice@work.com"]
        assert parsed["phone"] == ["+1-555-0100"]
        assert parsed["organization"] == "Acme Corp"
        assert parsed["title"] == "Engineer"
        assert parsed["birthday"] == "1990-03-15"
        assert parsed["labels"] == ["Friends"]

    def test_split_format(self):
        """Header should contain metadata, body should contain contact data."""
        contact = {"resourceName": "people/c1", "name": "Bob", "email": ["b@x.com"]}
        content = contact_to_yaml(contact)

        parts = content.split("---")
        assert len(parts) == 3

        header = yaml.safe_load(parts[1])
        assert header["type"] == "gax/contact"
        assert header["resourceName"] == "people/c1"
        assert "name" not in header

        body = yaml.safe_load(parts[2])
        assert body["name"] == "Bob"
        assert "type" not in body

    def test_empty_fields_omitted(self):
        """Empty strings and empty lists should not appear in body."""
        contact = {
            "resourceName": "people/c1",
            "name": "Bob",
            "email": [],
            "phone": [],
            "organization": "",
            "labels": [],
        }
        content = contact_to_yaml(contact)
        body = yaml.safe_load(content.split("---")[2])
        assert "email" not in body
        assert "phone" not in body
        assert "organization" not in body

    def test_invalid_format(self):
        import pytest

        with pytest.raises(ValueError, match="Expected YAML frontmatter"):
            yaml_to_contact("not yaml")


# =============================================================================
# Contacts.checkout
# =============================================================================


SAMPLE_NORMALIZED = [
    {
        "resourceName": "people/c1",
        "name": "Alice Smith",
        "givenName": "Alice",
        "familyName": "Smith",
        "email": ["alice@x.com"],
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
    },
    {
        "resourceName": "people/c2",
        "name": "Bob Jones",
        "givenName": "Bob",
        "familyName": "Jones",
        "email": ["bob@x.com"],
        "phone": ["+1-555-0200"],
        "organization": "Acme",
        "title": "",
        "department": "",
        "address": "",
        "birthday": "",
        "notes": "",
        "nickname": "",
        "website": "",
        "labels": [],
    },
]


class TestContactsCheckout:
    @patch("gax.contacts.fetch_contacts")
    @patch("gax.contacts.fetch_contact_groups")
    def test_checkout_creates_folder(self, mock_groups, mock_fetch, tmp_path):
        mock_groups.return_value = {}
        mock_fetch.return_value = (
            [
                {
                    "resourceName": c["resourceName"],
                    "names": [
                        {
                            "displayName": c["name"],
                            "givenName": c["givenName"],
                            "familyName": c["familyName"],
                        }
                    ],
                    "emailAddresses": [{"value": e} for e in c["email"]],
                }
                for c in SAMPLE_NORMALIZED
            ],
            {},
        )
        # Use pre-normalized contacts via direct mock
        with patch.object(
            Contacts, "_fetch_and_normalize", return_value=(SAMPLE_NORMALIZED, {})
        ):
            output = tmp_path / "contacts.contacts.gax.md.d"
            cloned, skipped = Contacts().checkout(output=output)

            assert output.exists()
            assert (output / ".gax.yaml").exists()
            assert cloned == 2
            assert skipped == 0

    @patch.object(
        Contacts, "_fetch_and_normalize", return_value=(SAMPLE_NORMALIZED, {})
    )
    def test_checkout_creates_contact_files(self, mock_fetch, tmp_path):
        output = tmp_path / "contacts.contacts.gax.md.d"
        Contacts().checkout(output=output)

        files = sorted(output.glob("*.contact.gax.yaml"))
        assert len(files) == 2

        # Files should round-trip cleanly
        for f in files:
            c = yaml_to_contact(f.read_text())
            assert c["resourceName"] in ("people/c1", "people/c2")

    @patch.object(
        Contacts, "_fetch_and_normalize", return_value=(SAMPLE_NORMALIZED, {})
    )
    def test_checkout_writes_metadata(self, mock_fetch, tmp_path):
        output = tmp_path / "contacts.contacts.gax.md.d"
        Contacts().checkout(output=output)

        meta = yaml.safe_load((output / ".gax.yaml").read_text())
        assert meta["type"] == "gax/contacts-checkout"
        assert "checked_out" in meta

    @patch.object(
        Contacts, "_fetch_and_normalize", return_value=(SAMPLE_NORMALIZED, {})
    )
    def test_checkout_skips_existing(self, mock_fetch, tmp_path):
        output = tmp_path / "contacts.contacts.gax.md.d"

        # First checkout
        Contacts().checkout(output=output)

        # Second checkout should skip all
        cloned, skipped = Contacts().checkout(output=output)
        assert cloned == 0
        assert skipped == 2


# =============================================================================
# Contacts.pull_checkout
# =============================================================================


class TestCheckoutPull:
    @patch.object(
        Contacts, "_fetch_and_normalize", return_value=(SAMPLE_NORMALIZED, {})
    )
    def test_pull_removes_stale_contacts(self, mock_fetch, tmp_path):
        output = tmp_path / "contacts.contacts.gax.md.d"

        # Checkout with 2 contacts
        Contacts().checkout(output=output)
        assert len(list(output.glob("*.contact.gax.yaml"))) == 2

        # Remote now has only one contact
        mock_fetch.return_value = (SAMPLE_NORMALIZED[:1], {})
        Contacts().pull_checkout(output)

        files = list(output.glob("*.contact.gax.yaml"))
        assert len(files) == 1

    @patch.object(
        Contacts, "_fetch_and_normalize", return_value=(SAMPLE_NORMALIZED, {})
    )
    def test_pull_adds_new_contacts(self, mock_fetch, tmp_path):
        output = tmp_path / "contacts.contacts.gax.md.d"

        # Checkout with 2 contacts
        Contacts().checkout(output=output)

        # Remote now has 3 contacts
        new_contact = {
            **SAMPLE_NORMALIZED[0],
            "resourceName": "people/c3",
            "name": "Charlie Brown",
        }
        mock_fetch.return_value = (SAMPLE_NORMALIZED + [new_contact], {})
        Contacts().pull_checkout(output)

        files = list(output.glob("*.contact.gax.yaml"))
        assert len(files) == 3

    @patch.object(
        Contacts, "_fetch_and_normalize", return_value=(SAMPLE_NORMALIZED, {})
    )
    def test_pull_updates_existing(self, mock_fetch, tmp_path):
        output = tmp_path / "contacts.contacts.gax.md.d"

        # Checkout
        Contacts().checkout(output=output)

        # Remote has updated name for c1
        updated = [
            {**SAMPLE_NORMALIZED[0], "name": "Alice Updated"},
            SAMPLE_NORMALIZED[1],
        ]
        mock_fetch.return_value = (updated, {})
        Contacts().pull_checkout(output)

        # Find the file for people/c1 and verify
        for f in output.glob("*.contact.gax.yaml"):
            c = yaml_to_contact(f.read_text())
            if c["resourceName"] == "people/c1":
                assert c["name"] == "Alice Updated"
                break
