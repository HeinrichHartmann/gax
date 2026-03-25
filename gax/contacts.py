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
    "addresses,birthdays,biographies,nicknames,urls"
)


def get_contacts_service():
    """Get authenticated People API service."""
    creds = get_authenticated_credentials()
    return build("people", "v1", credentials=creds)


def list_contacts(*, service=None) -> list[dict]:
    """Fetch all contacts with all fields."""
    service = service or get_contacts_service()
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

    return contacts


def normalize_contact(api_contact: dict) -> dict:
    """Normalize API contact to flat structure for JSONL."""
    names = api_contact.get("names") or [{}]
    orgs = api_contact.get("organizations") or [{}]
    addresses = api_contact.get("addresses") or [{}]
    birthdays = api_contact.get("birthdays") or []
    bios = api_contact.get("biographies") or [{}]
    nicknames = api_contact.get("nicknames") or [{}]
    urls = api_contact.get("urls") or [{}]

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
    }


def contacts_to_jsonl(contacts: list[dict]) -> str:
    """Format contacts as JSONL body (without header)."""
    normalized = [normalize_contact(c) for c in contacts]
    # Sort by name for consistent output
    normalized.sort(key=lambda c: c.get("name", "").lower())
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in normalized)


def contacts_to_markdown(contacts: list[dict]) -> str:
    """Format contacts as markdown body (without header)."""
    normalized = [normalize_contact(c) for c in contacts]
    # Sort by name
    normalized.sort(key=lambda c: c.get("name", "").lower())

    sections = []
    for c in normalized:
        lines = [f"## {c['name'] or '(unnamed)'}"]
        if c["email"]:
            lines.append(f"- **Email**: {', '.join(c['email'])}")
        if c["phone"]:
            lines.append(f"- **Phone**: {', '.join(c['phone'])}")
        if c["organization"] or c["title"]:
            org = c["organization"] or ""
            if c["title"]:
                org = f"{org} ({c['title']})" if org else c["title"]
            lines.append(f"- **Organization**: {org}")
        if c["address"]:
            lines.append(f"- **Address**: {c['address']}")
        if c["birthday"]:
            lines.append(f"- **Birthday**: {c['birthday']}")
        if c["website"]:
            lines.append(f"- **Website**: {c['website']}")
        if c["notes"]:
            # Truncate long notes
            notes = c["notes"]
            if len(notes) > 100:
                notes = notes[:100] + "..."
            lines.append(f"- **Notes**: {notes}")
        lines.append(f"\n<!-- resourceName: {c['resourceName']} -->")
        sections.append("\n".join(lines))

    return "\n\n---\n\n".join(sections)


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
        all_contacts = list_contacts()

        if fmt == "jsonl":
            body = contacts_to_jsonl(all_contacts)
            default_name = "contacts.jsonl"
        else:
            body = contacts_to_markdown(all_contacts)
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
        all_contacts = list_contacts()

        if fmt == "jsonl":
            body = contacts_to_jsonl(all_contacts)
        else:
            body = contacts_to_markdown(all_contacts)

        new_header = format_header(fmt, len(all_contacts))
        content = f"{new_header}\n{body}\n"

        file.write_text(content, encoding="utf-8")

        click.echo(f"Updated: {file}")
        click.echo(f"Contacts: {len(all_contacts)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
