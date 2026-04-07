"""Google Contacts support for gax.

Formats:
- MD: Human-readable view (default, view-only)
- JSONL: Full data, scriptable, editable
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from .ui import operation, success, error

logger = logging.getLogger(__name__)

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
    normalized = []
    with operation("Normalizing contacts", total=len(contacts)) as op:
        for c in contacts:
            logger.info(f"Processing: {c.get('names', [{}])[0].get('displayName', '(unnamed)')}")
            normalized.append(normalize_contact(c, groups))
            op.advance()
    # Sort by name for consistent output
    normalized.sort(key=lambda c: c.get("name", "").lower())
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in normalized)


def contacts_to_markdown(contacts: list[dict], groups: dict[str, str]) -> str:
    """Format contacts as markdown body (without header)."""
    normalized = []
    with operation("Normalizing contacts", total=len(contacts)) as op:
        for c in contacts:
            logger.info(f"Processing: {c.get('names', [{}])[0].get('displayName', '(unnamed)')}")
            normalized.append(normalize_contact(c, groups))
            op.advance()
    # Sort by name
    normalized.sort(key=lambda c: c.get("name", "").lower())

    entries = []
    with operation("Formatting contacts", total=len(normalized)) as op:
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
            op.advance()

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

        success(f"Created: {file_path}")
        click.echo(f"Contacts: {len(all_contacts)}")

    except Exception as e:
        error(f"Error: {e}")
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

        success(f"Updated: {file}")
        click.echo(f"Contacts: {len(all_contacts)}")

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


# --- Plan/Apply Support ---


def compare_contacts(
    local: list[dict], remote: list[dict]
) -> tuple[list[dict], list[dict], list[str]]:
    """Compare local and remote contacts.

    Returns:
        (creates, updates, deletes) where:
        - creates: local contacts with no resourceName (new)
        - updates: local contacts that differ from remote
        - deletes: resourceNames in remote but not in local
    """
    remote_by_id = {c["resourceName"]: c for c in remote}
    local_by_id = {c["resourceName"]: c for c in local if c.get("resourceName")}

    creates = []
    updates = []
    deletes = []

    # Check local contacts
    for contact in local:
        resource_name = contact.get("resourceName", "")
        if not resource_name:
            # No resourceName = new contact
            creates.append(contact)
        elif resource_name in remote_by_id:
            # Exists remotely - check for changes
            remote_contact = remote_by_id[resource_name]
            if contact_differs(contact, remote_contact):
                updates.append(contact)

    # Check for deletes (in remote but not in local)
    for resource_name in remote_by_id:
        if resource_name not in local_by_id:
            deletes.append(resource_name)

    return creates, updates, deletes


def contact_differs(local: dict, remote: dict) -> bool:
    """Check if local contact differs from remote."""
    # Compare relevant fields
    fields = [
        "name", "givenName", "familyName", "email", "phone",
        "organization", "title", "department", "address",
        "birthday", "notes", "nickname", "website", "labels"
    ]
    for field in fields:
        local_val = local.get(field, "" if field not in ("email", "phone", "labels") else [])
        remote_val = remote.get(field, "" if field not in ("email", "phone", "labels") else [])
        # Normalize lists for comparison
        if isinstance(local_val, list) and isinstance(remote_val, list):
            if sorted(local_val) != sorted(remote_val):
                return True
        elif local_val != remote_val:
            return True
    return False


def get_contact_diff(local: dict, remote: dict) -> dict:
    """Get fields that differ between local and remote."""
    diff = {}
    fields = [
        "name", "givenName", "familyName", "email", "phone",
        "organization", "title", "department", "address",
        "birthday", "notes", "nickname", "website", "labels"
    ]
    for field in fields:
        local_val = local.get(field, "" if field not in ("email", "phone", "labels") else [])
        remote_val = remote.get(field, "" if field not in ("email", "phone", "labels") else [])
        if isinstance(local_val, list) and isinstance(remote_val, list):
            if sorted(local_val) != sorted(remote_val):
                diff[field] = {"from": remote_val, "to": local_val}
        elif local_val != remote_val:
            diff[field] = {"from": remote_val, "to": local_val}
    return diff


def local_to_api_contact(contact: dict, groups_by_name: dict[str, str]) -> dict:
    """Convert normalized contact to API format for create/update."""
    api_contact = {}

    # Names
    if contact.get("name") or contact.get("givenName") or contact.get("familyName"):
        api_contact["names"] = [{
            "givenName": contact.get("givenName", ""),
            "familyName": contact.get("familyName", ""),
        }]

    # Email addresses
    if contact.get("email"):
        api_contact["emailAddresses"] = [{"value": e} for e in contact["email"]]

    # Phone numbers
    if contact.get("phone"):
        api_contact["phoneNumbers"] = [{"value": p} for p in contact["phone"]]

    # Organization
    if contact.get("organization") or contact.get("title") or contact.get("department"):
        api_contact["organizations"] = [{
            "name": contact.get("organization", ""),
            "title": contact.get("title", ""),
            "department": contact.get("department", ""),
        }]

    # Address
    if contact.get("address"):
        api_contact["addresses"] = [{"formattedValue": contact["address"]}]

    # Birthday
    if contact.get("birthday"):
        bday = contact["birthday"]
        date_obj = {}
        if bday.startswith("--"):
            # Month-day only
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
            api_contact["birthdays"] = [{"date": date_obj}]

    # Notes/biography
    if contact.get("notes"):
        api_contact["biographies"] = [{"value": contact["notes"]}]

    # Nickname
    if contact.get("nickname"):
        api_contact["nicknames"] = [{"value": contact["nickname"]}]

    # Website
    if contact.get("website"):
        api_contact["urls"] = [{"value": contact["website"]}]

    # Labels (memberships)
    if contact.get("labels"):
        memberships = []
        for label in contact["labels"]:
            if label in groups_by_name:
                memberships.append({
                    "contactGroupMembership": {
                        "contactGroupResourceName": groups_by_name[label]
                    }
                })
        if memberships:
            api_contact["memberships"] = memberships

    return api_contact


def generate_plan(
    local_file: Path,
    local_contacts: list[dict],
    remote_contacts: list[dict],
) -> dict:
    """Generate a plan comparing local and remote contacts."""
    creates, updates, deletes = compare_contacts(local_contacts, remote_contacts)

    plan = {
        "type": "gax/contacts-plan",
        "source": str(local_file),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": {
            "create": len(creates),
            "update": len(updates),
            "delete": len(deletes),
        },
        "changes": [],
    }

    # Build remote lookup for diffs
    remote_by_id = {c["resourceName"]: c for c in remote_contacts}

    # Creates
    for contact in creates:
        plan["changes"].append({
            "action": "create",
            "contact": contact,
        })

    # Updates
    for contact in updates:
        resource_name = contact["resourceName"]
        remote = remote_by_id.get(resource_name, {})
        diff = get_contact_diff(contact, remote)
        plan["changes"].append({
            "action": "update",
            "resourceName": resource_name,
            "name": contact.get("name", ""),
            "diff": diff,
            "contact": contact,
        })

    # Deletes
    for resource_name in deletes:
        remote = remote_by_id.get(resource_name, {})
        plan["changes"].append({
            "action": "delete",
            "resourceName": resource_name,
            "name": remote.get("name", ""),
        })

    return plan


def format_plan_summary(plan: dict) -> str:
    """Format plan for display."""
    lines = ["Plan:"]
    summary = plan["summary"]
    lines.append(f"  Create: {summary['create']} contacts")
    lines.append(f"  Update: {summary['update']} contacts")
    lines.append(f"  Delete: {summary['delete']} contacts")
    lines.append("")
    lines.append("Changes:")

    for change in plan["changes"]:
        action = change["action"]
        if action == "create":
            contact = change["contact"]
            name = contact.get("name", "(unnamed)")
            emails = ", ".join(contact.get("email", []))
            lines.append(f"  + Create: \"{name}\" <{emails}>")
        elif action == "update":
            name = change.get("name", "(unnamed)")
            diff_fields = list(change.get("diff", {}).keys())
            lines.append(f"  ~ Update: \"{name}\" - {', '.join(diff_fields)} changed")
        elif action == "delete":
            name = change.get("name", "(unnamed)")
            lines.append(f"  - Delete: \"{name}\"")

    return "\n".join(lines)


@contacts.command("plan")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o", "--output",
    type=click.Path(path_type=Path),
    help="Output plan file (default: <file>.plan.yaml)",
)
def plan(file: Path, output: Path | None):
    """Generate a plan for syncing local contacts to Google.

    Compares local JSONL file with remote contacts and generates
    a plan showing creates, updates, and deletes.
    """
    try:
        header = parse_contacts_file(file)
        fmt = header.get("format", "")
        if fmt != "jsonl":
            click.echo("Error: plan only works with JSONL format", err=True)
            sys.exit(1)

        click.echo("Fetching remote contacts...")
        service = get_contacts_service()
        remote_raw, groups = list_contacts(service=service)
        remote_contacts = [normalize_contact(c, groups) for c in remote_raw]

        click.echo("Parsing local contacts...")
        local_contacts = parse_contacts_jsonl(file)

        change_plan = generate_plan(file, local_contacts, remote_contacts)

        # Display summary
        click.echo("")
        click.echo(format_plan_summary(change_plan))

        if change_plan["summary"]["create"] == 0 and \
           change_plan["summary"]["update"] == 0 and \
           change_plan["summary"]["delete"] == 0:
            click.echo("\nNo changes to apply.")
            return

        # Write plan file
        plan_file = output or file.with_suffix(".plan.yaml")
        with open(plan_file, "w", encoding="utf-8") as f:
            yaml.dump(change_plan, f, default_flow_style=False, allow_unicode=True)

        click.echo(f"\nPlan written to: {plan_file}")
        click.echo(f"Run 'gax contacts apply {plan_file}' to apply changes.")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@contacts.command("apply")
@click.argument("plan_file", type=click.Path(exists=True, path_type=Path))
@click.option('-y', '--yes', is_flag=True, help='Skip confirmation')
def apply(plan_file: Path, yes: bool):
    """Apply a contacts plan to Google.

    Executes the creates, updates, and deletes specified in the plan file.
    """
    try:
        with open(plan_file, encoding="utf-8") as f:
            change_plan = yaml.safe_load(f)

        if change_plan.get("type") != "gax/contacts-plan":
            click.echo("Error: Not a valid contacts plan file", err=True)
            sys.exit(1)

        # Display summary
        click.echo(format_plan_summary(change_plan))

        total = sum(change_plan["summary"].values())
        if total == 0:
            click.echo("\nNo changes to apply.")
            return

        # Confirm (unless --yes flag)
        click.echo("")
        if not yes and not click.confirm("Apply these changes?"):
            click.echo("Aborted.")
            return

        # Apply changes
        service = get_contacts_service()
        groups = get_contact_groups(service=service)
        groups_by_name = {v: k for k, v in groups.items()}

        created = 0
        updated = 0
        deleted = 0
        errors = []

        with operation("Applying changes", total=len(change_plan["changes"])) as op:
            for change in change_plan["changes"]:
                action = change["action"]
                try:
                    if action == "create":
                        contact = change["contact"]
                        logger.info(f"Creating: {contact.get('name', '(unnamed)')}")
                        api_contact = local_to_api_contact(contact, groups_by_name)
                        service.people().createContact(body=api_contact).execute()
                        created += 1
                        click.echo(f"  + Created: {contact.get('name', '(unnamed)')}")

                    elif action == "update":
                        resource_name = change["resourceName"]
                        contact = change["contact"]
                        logger.info(f"Updating: {contact.get('name', '(unnamed)')}")
                        api_contact = local_to_api_contact(contact, groups_by_name)

                        # Get current etag for update
                        current = service.people().get(
                            resourceName=resource_name,
                            personFields=ALL_PERSON_FIELDS,
                        ).execute()
                        etag = current.get("etag", "")

                        # Build update mask from diff
                        diff = change.get("diff", {})
                        update_fields = []
                        field_mapping = {
                            "name": "names",
                            "givenName": "names",
                            "familyName": "names",
                            "email": "emailAddresses",
                            "phone": "phoneNumbers",
                            "organization": "organizations",
                            "title": "organizations",
                            "department": "organizations",
                            "address": "addresses",
                            "birthday": "birthdays",
                            "notes": "biographies",
                            "nickname": "nicknames",
                            "website": "urls",
                            "labels": "memberships",
                        }
                        for field in diff:
                            api_field = field_mapping.get(field)
                            if api_field and api_field not in update_fields:
                                update_fields.append(api_field)

                        api_contact["etag"] = etag
                        service.people().updateContact(
                            resourceName=resource_name,
                            body=api_contact,
                            updatePersonFields=",".join(update_fields),
                        ).execute()
                        updated += 1
                        click.echo(f"  ~ Updated: {contact.get('name', '(unnamed)')}")

                    elif action == "delete":
                        resource_name = change["resourceName"]
                        name = change.get("name", "(unnamed)")
                        logger.info(f"Deleting: {name}")
                        service.people().deleteContact(
                            resourceName=resource_name,
                        ).execute()
                        deleted += 1
                        click.echo(f"  - Deleted: {name}")

                except Exception as e:
                    errors.append(f"{action} {change.get('name', change.get('resourceName', '?'))}: {e}")

                op.advance()

        click.echo("")
        click.echo(f"Applied: {created} created, {updated} updated, {deleted} deleted")
        if errors:
            click.echo(f"Errors: {len(errors)}")
            for err in errors:
                click.echo(f"  {err}", err=True)
            sys.exit(1)

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)
