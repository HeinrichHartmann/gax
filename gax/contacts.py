"""Google Contacts support for gax.

Formats:
- MD: Human-readable view (default, view-only)
- JSONL: Full data, scriptable, editable
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials

ALL_PERSON_FIELDS = (
    "names,emailAddresses,phoneNumbers,organizations,"
    "addresses,birthdays,biographies,nicknames,urls,memberships"
)


def get_contacts_service():
    """Get authenticated People API service."""
    creds = get_authenticated_credentials()
    return build("people", "v1", credentials=creds)


def get_contact_groups(*, service=None) -> dict[str, str]:
    """Fetch contact groups and return mapping of resourceName -> name."""
    service = service or get_contacts_service()
    groups = {}
    page_token = None

    while True:
        result = (
            service.contactGroups()
            .list(pageSize=1000, pageToken=page_token)
            .execute()
        )
        for group in result.get("contactGroups", []):
            # Skip system groups like "myContacts", "starred"
            if group.get("groupType") == "USER_CONTACT_GROUP":
                groups[group["resourceName"]] = group.get("name", "")
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return groups


def list_contacts(*, service=None) -> tuple[list[dict], dict[str, str]]:
    """Fetch all contacts with all fields and contact groups."""
    service = service or get_contacts_service()

    # Fetch contact groups first
    groups = get_contact_groups(service=service)

    # Fetch contacts
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


def normalize_contact(api_contact: dict, groups: dict[str, str]) -> dict:
    """Normalize API contact to flat structure for JSONL."""
    names = api_contact.get("names") or [{}]
    orgs = api_contact.get("organizations") or [{}]
    addresses = api_contact.get("addresses") or [{}]
    birthdays = api_contact.get("birthdays") or []
    bios = api_contact.get("biographies") or [{}]
    nicknames = api_contact.get("nicknames") or [{}]
    urls = api_contact.get("urls") or [{}]
    memberships = api_contact.get("memberships") or []

    # Format birthday
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

    # Extract labels from memberships (user-defined groups only)
    labels = []
    for m in memberships:
        group_ref = m.get("contactGroupMembership", {}).get("contactGroupResourceName", "")
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


def contacts_to_jsonl(contacts: list[dict], groups: dict[str, str]) -> str:
    """Format contacts as JSONL body (without header)."""
    normalized = [normalize_contact(c, groups) for c in contacts]
    # Sort by name for consistent output
    normalized.sort(key=lambda c: c.get("name", "").lower())
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in normalized)


def contacts_to_markdown(contacts: list[dict], groups: dict[str, str]) -> str:
    """Format contacts as markdown body (without header)."""
    normalized = [normalize_contact(c, groups) for c in contacts]
    # Sort by name
    normalized.sort(key=lambda c: c.get("name", "").lower())

    entries = []
    for c in normalized:
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
            # Indent multiline addresses
            addr = c["address"].replace("\n", "\n    ")
            lines.append(f"  - address: {addr}")
        if c["birthday"]:
            lines.append(f"  - birthday: {c['birthday']}")
        if c["website"]:
            lines.append(f"  - website: {c['website']}")
        if c["notes"]:
            # Truncate long notes, indent multiline
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


def format_header(fmt: str, count: int) -> str:
    """Format YAML header for contacts file."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "---",
        "type: gax/contacts",
        f"format: {fmt}",
        f"pulled: {now}",
        f"count: {count}",
        "---",
    ]
    return "\n".join(lines)


def parse_contacts_file(file_path: Path) -> dict:
    """Parse contacts file header."""
    content = file_path.read_text(encoding="utf-8")

    if not content.startswith("---"):
        raise ValueError("File must start with YAML header (---)")

    # Find end of header
    end = content.find("\n---\n", 4)
    if end == -1:
        raise ValueError("Could not find end of YAML header")

    header_text = content[4:end]
    header = {}
    for line in header_text.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            header[key.strip()] = value.strip()

    return header


def parse_contacts_jsonl(file_path: Path) -> list[dict]:
    """Parse contacts from JSONL file."""
    content = file_path.read_text(encoding="utf-8")

    # Skip header
    if content.startswith("---"):
        end = content.find("\n---\n", 4)
        if end != -1:
            content = content[end + 5:]

    contacts = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if line:
            contacts.append(json.loads(line))

    return contacts


# --- CLI Commands ---


@click.group()
def contacts():
    """Google Contacts operations."""
    pass


@contacts.command("clone")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "jsonl"]),
    default="md",
    help="Output format: md (view-only) or jsonl (editable)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: contacts.<format>)",
)
def clone(fmt: str, output: Path | None):
    """Clone all contacts to a local file.

    \b
    Formats:
      md     Human-readable markdown (default, view-only)
      jsonl  JSON Lines format (editable, scriptable)
    """
    try:
        click.echo("Fetching contacts...")
        all_contacts, groups = list_contacts()

        if fmt == "jsonl":
            body = contacts_to_jsonl(all_contacts, groups)
            default_name = "contacts.jsonl"
        else:
            body = contacts_to_markdown(all_contacts, groups)
            default_name = "contacts.md"

        header = format_header(fmt, len(all_contacts))
        content = f"{header}\n{body}\n"

        file_path = output or Path(default_name)

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")

        click.echo(f"Created: {file_path}")
        click.echo(f"Contacts: {len(all_contacts)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@contacts.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def pull(file: Path):
    """Pull latest contacts from Google.

    Updates the file with current contact data, preserving format.
    """
    try:
        header = parse_contacts_file(file)
        fmt = header.get("format", "md")

        click.echo("Fetching contacts...")
        all_contacts, groups = list_contacts()

        if fmt == "jsonl":
            body = contacts_to_jsonl(all_contacts, groups)
        else:
            body = contacts_to_markdown(all_contacts, groups)

        new_header = format_header(fmt, len(all_contacts))
        content = f"{new_header}\n{body}\n"

        file.write_text(content, encoding="utf-8")

        click.echo(f"Updated: {file}")
        click.echo(f"Contacts: {len(all_contacts)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
