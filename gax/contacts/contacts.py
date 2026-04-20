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
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from googleapiclient.discovery import build

from ..gaxfile import GaxFile, format_single
from ..auth import get_authenticated_credentials
from ..resource import Resource

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
    gf = GaxFile.from_path(path, multipart=False)

    header = ContactsHeader(
        format=gf.headers.get("format", "md"),
        count=int(gf.headers.get("count", 0)),
        pulled=gf.headers.get("pulled", ""),
    )

    return header, gf.body


def format_contacts_file(header: ContactsHeader, body: str) -> str:
    """Format a contacts header and body as file content."""
    h = {
        "type": "gax/contacts",
        "format": header.format,
        "pulled": header.pulled,
        "count": header.count,
    }
    return format_single(h, body + "\n")


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
# Individual contact file format — split header/body YAML (.contact.gax.yaml)
#
# contact_to_yaml and yaml_to_contact are an inverse pair.
# =============================================================================


def contact_to_yaml(contact: dict) -> str:
    """Serialize a normalized contact dict to split YAML (header + body)."""
    header: dict = {
        "type": "gax/contact",
        "resourceName": contact.get("resourceName", ""),
        "synced": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    body: dict = {}
    for field in CONTACT_BODY_FIELDS:
        val = contact.get(field)
        if val:  # omit empty strings and empty lists
            body[field] = val

    header_str = yaml.dump(
        header, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    body_str = yaml.dump(
        body, default_flow_style=False, allow_unicode=True, sort_keys=False
    )

    return f"---\n{header_str}---\n{body_str}"


CONTACT_BODY_FIELDS = [
    "name",
    "givenName",
    "familyName",
    "email",
    "phone",
    "organization",
    "title",
    "department",
    "address",
    "birthday",
    "notes",
    "nickname",
    "website",
    "labels",
]


def yaml_to_contact(content: str) -> dict:
    """Parse split YAML content to a normalized contact dict."""
    if not content.startswith("---"):
        raise ValueError("Expected YAML frontmatter (---)")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid split YAML format")

    header = yaml.safe_load(parts[1])
    body = yaml.safe_load(parts[2])
    data = {**header, **(body or {})}

    # Build normalized contact dict with defaults
    contact = {"resourceName": data.get("resourceName", "")}
    for field in CONTACT_BODY_FIELDS:
        if field in LIST_FIELDS:
            contact[field] = data.get(field, [])
        else:
            contact[field] = data.get(field, "")
    return contact


def _safe_filename(name: str) -> str:
    """Create a filesystem-safe filename from a contact name."""
    safe = re.sub(r"[^\w\s-]", "", name)[:40].strip()
    return re.sub(r"\s+", "_", safe) or "unnamed"


# =============================================================================
# Comparison helpers — diff logic for local vs remote contacts.
# =============================================================================

COMPARABLE_FIELDS = [
    "name",
    "givenName",
    "familyName",
    "email",
    "phone",
    "organization",
    "title",
    "department",
    "address",
    "birthday",
    "notes",
    "nickname",
    "website",
    "labels",
]

LIST_FIELDS = {"email", "phone", "labels"}

# Mapping from normalized field names to API personFields for update masks
FIELD_TO_API = {
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
# Resource classes — the public interface for cli.py.
# =============================================================================


class Contact(Resource):
    """A single Google Contact (.contact.gax.yaml file)."""

    name = "contact"
    FILE_TYPE = "gax/contact"
    FILE_EXTENSIONS = (".contact.gax.yaml",)
    SCOPES = ("contacts.readonly",)

    def pull(self, **kw) -> None:
        """Pull latest contact data from Google."""
        content = self.path.read_text(encoding="utf-8")
        local = yaml_to_contact(content)
        rn = local.get("resourceName", "")
        if not rn:
            raise ValueError("Contact has no resourceName")

        raw_contacts, groups = fetch_contacts()
        for raw_c in raw_contacts:
            if raw_c.get("resourceName") == rn:
                updated = api_to_contact(raw_c, groups)
                self.path.write_text(contact_to_yaml(updated), encoding="utf-8")
                return
        raise ValueError(f"Contact {rn} not found remotely")


class Contacts(Resource):
    """Google Contacts resource.

    Constructed via from_file(path).
    Operations use instance state (self.path).
    """

    name = "contacts"
    FILE_TYPE = "gax/contacts"
    FILE_EXTENSIONS = (".contacts.gax.md",)
    CHECKOUT_TYPE = "gax/contacts-checkout"
    SCOPES = ("contacts",)

    def _fetch_and_normalize(self, *, service=None):
        """Fetch contacts from API and normalize. Returns (normalized, groups)."""
        raw_contacts, groups = fetch_contacts(service=service)
        normalized = [api_to_contact(c, groups) for c in raw_contacts]
        return normalized, groups

    def clone(
        self, output: Path | None = None, *, fmt: str = "md", **kw
    ) -> Path:
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

    def pull(self, **kw) -> None:
        """Pull latest contacts from Google."""
        if self.path.is_dir():
            self._pull_checkout()
            return

        header, _ = parse_contacts_file(self.path)

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
        self.path.write_text(content, encoding="utf-8")
        logger.info(f"Contacts: {len(normalized)}")

    def diff(self, **kw) -> str | None:
        """Preview changes between local and remote contacts."""
        if self.path.is_dir():
            return self._diff_checkout()

        header, body = parse_contacts_file(self.path)
        if header.format != "jsonl":
            raise ValueError("diff/push only works with JSONL format")

        local_contacts = parse_jsonl_body(body)

        logger.info("Fetching remote contacts...")
        remote_contacts, _ = self._fetch_and_normalize()

        creates, updates, deletes = compare_contacts(local_contacts, remote_contacts)
        return format_diff_summary(creates, updates, deletes) or None

    def push(self, **kw) -> None:
        """Push local contacts to Google. Unconditional."""
        if self.path.is_dir():
            self._push_checkout()
            return

        header, body = parse_contacts_file(self.path)
        if header.format != "jsonl":
            raise ValueError("push only works with JSONL format")

        local_contacts = parse_jsonl_body(body)
        self._push_contacts(local_contacts)

    # ── Checkout operations ──────────────────────────────────────────────

    def checkout(self, *, output: Path | None = None, **kw) -> tuple[int, int]:
        """Checkout contacts as individual .contact.gax.yaml files.

        Returns (cloned, skipped).
        """
        logger.info("Fetching contacts...")
        normalized, _ = self._fetch_and_normalize()

        folder = output or Path("contacts.contacts.gax.md.d")
        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata
        meta = {
            "type": "gax/contacts-checkout",
            "checked_out": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (folder / ".gax.yaml").write_text(
            yaml.dump(meta, default_flow_style=False, sort_keys=False)
        )

        # Get existing resourceNames in folder
        existing_ids: set[str] = set()
        for f in folder.glob("*.contact.gax.yaml"):
            try:
                c = yaml_to_contact(f.read_text(encoding="utf-8"))
                if c.get("resourceName"):
                    existing_ids.add(c["resourceName"])
            except Exception:
                pass

        cloned = 0
        skipped = 0

        for contact in normalized:
            resource_name = contact.get("resourceName", "")
            if resource_name in existing_ids:
                skipped += 1
                continue

            name = contact.get("name", "unnamed")
            filename = f"{_safe_filename(name)}.contact.gax.yaml"
            file_path = folder / filename

            # Handle filename collisions
            if file_path.exists():
                suffix = (
                    resource_name.rsplit("/", 1)[-1][:8]
                    if resource_name
                    else str(cloned)
                )
                filename = f"{_safe_filename(name)}_{suffix}.contact.gax.yaml"
                file_path = folder / filename

            file_path.write_text(contact_to_yaml(contact), encoding="utf-8")
            cloned += 1
            logger.info(f"Writing {filename}")

        logger.info(f"Contacts: {cloned} cloned, {skipped} skipped")
        return cloned, skipped

    def _pull_checkout(self) -> None:
        """Pull latest contacts into checkout folder, updating/adding/removing files."""
        folder = self.path
        logger.info("Fetching contacts...")
        normalized, _ = self._fetch_and_normalize()
        remote_by_id = {
            c["resourceName"]: c for c in normalized if c.get("resourceName")
        }

        # Scan existing files
        local_files: dict[str, Path] = {}  # resourceName -> file path
        for f in folder.glob("*.contact.gax.yaml"):
            try:
                c = yaml_to_contact(f.read_text(encoding="utf-8"))
                rn = c.get("resourceName", "")
                if rn:
                    local_files[rn] = f
            except Exception:
                pass

        # Update existing and remove stale
        for rn, file_path in local_files.items():
            if rn in remote_by_id:
                file_path.write_text(
                    contact_to_yaml(remote_by_id[rn]), encoding="utf-8"
                )
            else:
                file_path.unlink()
                logger.info(f"Removed: {file_path.name}")

        # Add new contacts
        added = 0
        for rn, contact in remote_by_id.items():
            if rn not in local_files:
                name = contact.get("name", "unnamed")
                filename = f"{_safe_filename(name)}.contact.gax.yaml"
                file_path = folder / filename
                if file_path.exists():
                    suffix = rn.rsplit("/", 1)[-1][:8]
                    filename = f"{_safe_filename(name)}_{suffix}.contact.gax.yaml"
                    file_path = folder / filename
                file_path.write_text(contact_to_yaml(contact), encoding="utf-8")
                added += 1
                logger.info(f"Added: {filename}")

        # Update metadata
        meta = {
            "type": "gax/contacts-checkout",
            "checked_out": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (folder / ".gax.yaml").write_text(
            yaml.dump(meta, default_flow_style=False, sort_keys=False)
        )

        logger.info(
            f"Updated: {len(local_files)} existing, {added} added, "
            f"{len(local_files) - sum(1 for rn in local_files if rn in remote_by_id)} removed"
        )

    def _diff_checkout(self) -> str | None:
        """Preview changes between checkout folder and remote contacts."""
        local_contacts = self._read_checkout_contacts(self.path)

        logger.info("Fetching remote contacts...")
        remote_contacts, _ = self._fetch_and_normalize()

        creates, updates, deletes = compare_contacts(local_contacts, remote_contacts)
        return format_diff_summary(creates, updates, deletes) or None

    def _push_checkout(self) -> None:
        """Push checkout folder contacts to Google."""
        local_contacts = self._read_checkout_contacts(self.path)
        self._push_contacts(local_contacts)

    # ── Private helpers ──────────────────────────────────────────────────

    def _read_checkout_contacts(self, folder: Path) -> list[dict]:
        """Read all .contact.gax.yaml files from a checkout folder."""
        contacts = []
        for f in sorted(folder.glob("*.contact.gax.yaml")):
            try:
                contacts.append(yaml_to_contact(f.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning(f"Skipping {f.name}: {e}")
        return contacts

    def _push_contacts(self, local_contacts: list[dict]) -> None:
        """Push a list of normalized contacts to Google. Shared by push() and push_checkout()."""
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


Resource.register(Contact)
Resource.register(Contacts)
