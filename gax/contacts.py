"""Google Contacts management for gax.

Resource module — follows the draft.py reference pattern.

Supports two formats:
  md    — human-readable markdown (view-only)
  jsonl — JSON Lines (editable, scriptable, required for push)

Module structure
================

  ContactsHeader       — dataclass for file frontmatter
  File format          — parse/format contacts files
  API helpers          — fetch contacts, serialize/deserialize API format
  Formatting           — render normalized contacts as JSONL or markdown
  Comparison helpers   — diff logic for local vs remote
  Contacts(Resource)   — resource class (the public interface for cli.py)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).
Additional notes specific to contacts:

  Two formats: md is read-only (for human viewing), jsonl is editable.
  push/diff only work on jsonl files — md files can only be cloned/pulled.

  diff() replaces the old plan/apply workflow. No intermediate plan file.
  The diff string shows creates/updates/deletes. push() applies them.

  api_to_contact / contact_to_api are a serialize/deserialize pair for
  the Google People API format. They should be kept in sync — changes
  to one likely require changes to the other.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from .resource import Resource

logger = logging.getLogger(__name__)

ALL_PERSON_FIELDS = (
    "names,emailAddresses,phoneNumbers,organizations,"
    "addresses,birthdays,biographies,nicknames,urls,memberships"
)


# =============================================================================
# Data class — shared between file format functions and the resource class.
# =============================================================================


@dataclass
class ContactsHeader:
    """Frontmatter of a contacts file."""

    format: str = "md"
    count: int = 0
    pulled: str = ""


# =============================================================================
# File format — parse/format contacts files.
# =============================================================================


def parse_contacts_file(path: Path) -> tuple[ContactsHeader, str]:
    """Parse a contacts file into header and body."""
    content = path.read_text(encoding="utf-8")

    if not content.startswith("---"):
        raise ValueError("File must start with YAML header (---)")

    end = content.find("\n---\n", 4)
    if end == -1:
        raise ValueError("Could not find end of YAML header")

    header_text = content[4:end]
    fields = {}
    for line in header_text.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()

    header = ContactsHeader(
        format=fields.get("format", "md"),
        count=int(fields.get("count", "0")),
        pulled=fields.get("pulled", ""),
    )

    body = content[end + 5:]  # Skip closing --- and newline
    return header, body


def format_contacts_file(header: ContactsHeader, body: str) -> str:
    """Format a contacts header and body as file content."""
    lines = [
        "---",
        "type: gax/contacts",
        f"format: {header.format}",
        f"pulled: {header.pulled}",
        f"count: {header.count}",
        "---",
    ]
    return "\n".join(lines) + "\n" + body + "\n"


def parse_jsonl_body(body: str) -> list[dict]:
    """Parse JSONL body into list of contact dicts."""
    contacts = []
    for line in body.strip().split("\n"):
        line = line.strip()
        if line:
            contacts.append(json.loads(line))
    return contacts


# =============================================================================
# API helpers — fetch contacts, serialize/deserialize API format.
#
# api_to_contact and contact_to_api are an inverse pair:
#   api_to_contact: Google People API dict → flat normalized dict
#   contact_to_api: flat normalized dict → Google People API dict
# Keep them in sync — changes to one likely require changes to the other.
# =============================================================================


def get_service():
    """Get authenticated People API service."""
    creds = get_authenticated_credentials()
    return build("people", "v1", credentials=creds)


def fetch_contact_groups(*, service=None) -> dict[str, str]:
    """Fetch contact groups. Returns resourceName -> name mapping."""
    service = service or get_service()
    groups = {}
    page_token = None

    while True:
        result = (
            service.contactGroups().list(pageSize=1000, pageToken=page_token).execute()
        )
        for group in result.get("contactGroups", []):
            if group.get("groupType") == "USER_CONTACT_GROUP":
                groups[group["resourceName"]] = group.get("name", "")
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return groups


def fetch_contacts(*, service=None) -> tuple[list[dict], dict[str, str]]:
    """Fetch all contacts and groups. Returns (raw_contacts, groups)."""
    service = service or get_service()
    groups = fetch_contact_groups(service=service)

    contacts = []
    page_token = None

    while True:
        result = (
            service.people()
            .connections()
            .list(
                resourceName="people/me",
                pageSize=1000,
                personFields=ALL_PERSON_FIELDS,
                pageToken=page_token,
            )
            .execute()
        )
        contacts.extend(result.get("connections", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return contacts, groups


def api_to_contact(api_contact: dict, groups: dict[str, str]) -> dict:
    """Normalize API contact to flat structure.

    Inverse of contact_to_api.
    """
    names = api_contact.get("names") or [{}]
    orgs = api_contact.get("organizations") or [{}]
    addresses = api_contact.get("addresses") or [{}]
    birthdays = api_contact.get("birthdays") or []
    bios = api_contact.get("biographies") or [{}]
    nicknames = api_contact.get("nicknames") or [{}]
    urls = api_contact.get("urls") or [{}]
    memberships = api_contact.get("memberships") or []

    birthday = ""
    if birthdays:
        bday = birthdays[0].get("date", {})
        if bday:
            year = bday.get("year", "")
            month = bday.get("month", 0)
            day = bday.get("day", 0)
            if year:
                birthday = f"{year:04d}-{month:02d}-{day:02d}"
            elif month and day:
                birthday = f"--{month:02d}-{day:02d}"

    labels = []
    for m in memberships:
        group_ref = m.get("contactGroupMembership", {}).get(
            "contactGroupResourceName", ""
        )
        if group_ref in groups:
            labels.append(groups[group_ref])

    return {
        "resourceName": api_contact.get("resourceName", ""),
        "name": names[0].get("displayName", ""),
        "givenName": names[0].get("givenName", ""),
        "familyName": names[0].get("familyName", ""),
        "email": [e["value"] for e in api_contact.get("emailAddresses", [])],
        "phone": [p["value"] for p in api_contact.get("phoneNumbers", [])],
        "organization": orgs[0].get("name", ""),
        "title": orgs[0].get("title", ""),
        "department": orgs[0].get("department", ""),
        "address": addresses[0].get("formattedValue", ""),
        "birthday": birthday,
        "notes": bios[0].get("value", ""),
        "nickname": nicknames[0].get("value", ""),
        "website": urls[0].get("value", ""),
        "labels": labels,
    }


def contact_to_api(contact: dict, groups_by_name: dict[str, str]) -> dict:
    """Convert flat normalized contact to API format for create/update.

    Inverse of api_to_contact.
    """
    api = {}

    if contact.get("name") or contact.get("givenName") or contact.get("familyName"):
        api["names"] = [
            {
                "givenName": contact.get("givenName", ""),
                "familyName": contact.get("familyName", ""),
            }
        ]

    if contact.get("email"):
        api["emailAddresses"] = [{"value": e} for e in contact["email"]]
    if contact.get("phone"):
        api["phoneNumbers"] = [{"value": p} for p in contact["phone"]]

    if contact.get("organization") or contact.get("title") or contact.get("department"):
        api["organizations"] = [
            {
                "name": contact.get("organization", ""),
                "title": contact.get("title", ""),
                "department": contact.get("department", ""),
            }
        ]

    if contact.get("address"):
        api["addresses"] = [{"formattedValue": contact["address"]}]

    if contact.get("birthday"):
        bday = contact["birthday"]
        date_obj = {}
        if bday.startswith("--"):
            parts = bday[2:].split("-")
            if len(parts) == 2:
                date_obj = {"month": int(parts[0]), "day": int(parts[1])}
        else:
            parts = bday.split("-")
            if len(parts) == 3:
                date_obj = {
                    "year": int(parts[0]),
                    "month": int(parts[1]),
                    "day": int(parts[2]),
                }
        if date_obj:
            api["birthdays"] = [{"date": date_obj}]

    if contact.get("notes"):
        api["biographies"] = [{"value": contact["notes"]}]
    if contact.get("nickname"):
        api["nicknames"] = [{"value": contact["nickname"]}]
    if contact.get("website"):
        api["urls"] = [{"value": contact["website"]}]

    if contact.get("labels"):
        memberships = []
        for label in contact["labels"]:
            if label in groups_by_name:
                memberships.append(
                    {
                        "contactGroupMembership": {
                            "contactGroupResourceName": groups_by_name[label]
                        }
                    }
                )
        if memberships:
            api["memberships"] = memberships

    return api


# =============================================================================
# Formatting — render normalized contacts as JSONL or markdown.
# These take already-normalized contacts (not raw API format).
# =============================================================================


def format_jsonl(contacts: list[dict]) -> str:
    """Format normalized contacts as JSONL body."""
    sorted_contacts = sorted(contacts, key=lambda c: c.get("name", "").lower())
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in sorted_contacts)


def format_markdown(contacts: list[dict]) -> str:
    """Format normalized contacts as markdown body."""
    sorted_contacts = sorted(contacts, key=lambda c: c.get("name", "").lower())

    entries = []
    for c in sorted_contacts:
        lines = [f"- {c['name'] or '(unnamed)'}"]
        if c["email"]:
            lines.append(f"  - email: {', '.join(c['email'])}")
        if c["phone"]:
            lines.append(f"  - phone: {', '.join(c['phone'])}")
        if c["organization"] or c["title"]:
            org = c["organization"] or ""
            if c["title"]:
                org = f"{org} ({c['title']})" if org else c["title"]
            lines.append(f"  - organization: {org}")
        if c["address"]:
            addr = c["address"].replace("\n", "\n    ")
            lines.append(f"  - address: {addr}")
        if c["birthday"]:
            lines.append(f"  - birthday: {c['birthday']}")
        if c["website"]:
            lines.append(f"  - website: {c['website']}")
        if c["notes"]:
            notes = c["notes"]
            if len(notes) > 100:
                notes = notes[:100] + "..."
            notes = notes.replace("\n", "\n    ")
            lines.append(f"  - notes: {notes}")
        if c["labels"]:
            lines.append(f"  - labels: {', '.join(c['labels'])}")
        lines.append(f"  - id: {c['resourceName']}")
        entries.append("\n".join(lines))

    return "\n".join(entries)


# =============================================================================
# Comparison helpers — diff logic for local vs remote contacts.
# =============================================================================

COMPARABLE_FIELDS = [
    "name", "givenName", "familyName", "email", "phone",
    "organization", "title", "department", "address",
    "birthday", "notes", "nickname", "website", "labels",
]

LIST_FIELDS = {"email", "phone", "labels"}

# Mapping from normalized field names to API personFields for update masks
FIELD_TO_API = {
    "name": "names", "givenName": "names", "familyName": "names",
    "email": "emailAddresses", "phone": "phoneNumbers",
    "organization": "organizations", "title": "organizations",
    "department": "organizations",
    "address": "addresses", "birthday": "birthdays",
    "notes": "biographies", "nickname": "nicknames",
    "website": "urls", "labels": "memberships",
}


def contact_diff(local: dict, remote: dict) -> dict[str, dict]:
    """Get fields that differ between local and remote contact.

    Returns dict of field -> {"from": remote_val, "to": local_val}.
    """
    diff = {}
    for field in COMPARABLE_FIELDS:
        default = [] if field in LIST_FIELDS else ""
        local_val = local.get(field, default)
        remote_val = remote.get(field, default)
        if isinstance(local_val, list) and isinstance(remote_val, list):
            if sorted(local_val) != sorted(remote_val):
                diff[field] = {"from": remote_val, "to": local_val}
        elif local_val != remote_val:
            diff[field] = {"from": remote_val, "to": local_val}
    return diff


def compare_contacts(
    local: list[dict], remote: list[dict]
) -> tuple[list[dict], list[tuple[dict, dict]], list[dict]]:
    """Compare local and remote contacts.

    Returns:
        (creates, updates, deletes) where:
        - creates: local contacts with no resourceName
        - updates: list of (local_contact, diff_dict) pairs
        - deletes: remote contacts not present locally
    """
    remote_by_id = {c["resourceName"]: c for c in remote}
    local_by_id = {c["resourceName"]: c for c in local if c.get("resourceName")}

    creates = []
    updates = []
    deletes = []

    for contact in local:
        resource_name = contact.get("resourceName", "")
        if not resource_name:
            creates.append(contact)
        elif resource_name in remote_by_id:
            diff = contact_diff(contact, remote_by_id[resource_name])
            if diff:
                updates.append((contact, diff))

    for resource_name, remote_contact in remote_by_id.items():
        if resource_name not in local_by_id:
            deletes.append(remote_contact)

    return creates, updates, deletes


def format_diff_summary(
    creates: list[dict],
    updates: list[tuple[dict, dict]],
    deletes: list[dict],
) -> str:
    """Format a human-readable diff summary."""
    if not creates and not updates and not deletes:
        return ""

    lines = [
        f"  Create: {len(creates)}, Update: {len(updates)}, Delete: {len(deletes)}",
        "",
    ]

    for contact in creates:
        name = contact.get("name", "(unnamed)")
        emails = ", ".join(contact.get("email", []))
        lines.append(f'  + Create: "{name}" <{emails}>')

    for contact, diff in updates:
        name = contact.get("name", "(unnamed)")
        fields = ", ".join(diff.keys())
        lines.append(f'  ~ Update: "{name}" — {fields}')

    for contact in deletes:
        name = contact.get("name", "(unnamed)")
        lines.append(f'  - Delete: "{name}"')

    return "\n".join(lines)


# =============================================================================
# Resource class — the public interface for cli.py.
# =============================================================================


class Contacts(Resource):
    """Google Contacts resource."""

    name = "contacts"

    def _fetch_and_normalize(self, *, service=None):
        """Fetch contacts from API and normalize. Returns (normalized, groups)."""
        raw_contacts, groups = fetch_contacts(service=service)
        normalized = [api_to_contact(c, groups) for c in raw_contacts]
        return normalized, groups

    def clone(self, url: str = "", output: Path | None = None, *,
              fmt: str = "md", **kw) -> Path:
        """Clone all contacts to a local file."""
        logger.info("Fetching contacts...")
        normalized, _ = self._fetch_and_normalize()

        if fmt == "jsonl":
            body = format_jsonl(normalized)
            default_name = "contacts.jsonl"
        else:
            body = format_markdown(normalized)
            default_name = "contacts.md"

        header = ContactsHeader(
            format=fmt,
            count=len(normalized),
            pulled=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        content = format_contacts_file(header, body)

        file_path = output or Path(default_name)
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        file_path.write_text(content, encoding="utf-8")
        logger.info(f"Contacts: {len(normalized)}")
        return file_path

    def pull(self, path: Path, **kw) -> None:
        """Pull latest contacts from Google."""
        header, _ = parse_contacts_file(path)

        logger.info("Fetching contacts...")
        normalized, _ = self._fetch_and_normalize()

        if header.format == "jsonl":
            body = format_jsonl(normalized)
        else:
            body = format_markdown(normalized)

        new_header = ContactsHeader(
            format=header.format,
            count=len(normalized),
            pulled=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        content = format_contacts_file(new_header, body)
        path.write_text(content, encoding="utf-8")
        logger.info(f"Contacts: {len(normalized)}")

    def diff(self, path: Path, **kw) -> str | None:
        """Preview changes between local JSONL and remote contacts."""
        header, body = parse_contacts_file(path)
        if header.format != "jsonl":
            raise ValueError("diff/push only works with JSONL format")

        local_contacts = parse_jsonl_body(body)

        logger.info("Fetching remote contacts...")
        remote_contacts, _ = self._fetch_and_normalize()

        creates, updates, deletes = compare_contacts(local_contacts, remote_contacts)
        return format_diff_summary(creates, updates, deletes) or None

    def push(self, path: Path, **kw) -> None:
        """Push local JSONL contacts to Google. Unconditional."""
        header, body = parse_contacts_file(path)
        if header.format != "jsonl":
            raise ValueError("push only works with JSONL format")

        local_contacts = parse_jsonl_body(body)

        logger.info("Fetching remote contacts...")
        service = get_service()
        raw_remote, groups = fetch_contacts(service=service)
        remote_contacts = [api_to_contact(c, groups) for c in raw_remote]

        creates, updates, deletes = compare_contacts(local_contacts, remote_contacts)

        if not creates and not updates and not deletes:
            logger.info("No changes to apply")
            return

        groups_by_name = {v: k for k, v in groups.items()}

        for contact in creates:
            logger.info(f"Creating: {contact.get('name', '(unnamed)')}")
            api_contact = contact_to_api(contact, groups_by_name)
            service.people().createContact(body=api_contact).execute()

        for contact, diff in updates:
            logger.info(f"Updating: {contact.get('name', '(unnamed)')}")
            resource_name = contact["resourceName"]
            api_contact = contact_to_api(contact, groups_by_name)

            current = (
                service.people()
                .get(resourceName=resource_name, personFields=ALL_PERSON_FIELDS)
                .execute()
            )
            api_contact["etag"] = current.get("etag", "")

            update_fields = []
            for field in diff:
                api_field = FIELD_TO_API.get(field)
                if api_field and api_field not in update_fields:
                    update_fields.append(api_field)

            service.people().updateContact(
                resourceName=resource_name,
                body=api_contact,
                updatePersonFields=",".join(update_fields),
            ).execute()

        for contact in deletes:
            logger.info(f"Deleting: {contact.get('name', '(unnamed)')}")
            service.people().deleteContact(
                resourceName=contact["resourceName"],
            ).execute()

        logger.info(
            f"Applied: {len(creates)} created, {len(updates)} updated, "
            f"{len(deletes)} deleted"
        )
