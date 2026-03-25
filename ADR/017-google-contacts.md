# ADR 017: Google Contacts Support

## Status

Proposed

## Context

Google Contacts is a commonly used Google service not yet supported by gax. Users may want to:
- Export contacts for backup or migration
- Edit contacts in bulk using familiar tools (spreadsheet, text editor)
- Sync contact changes back to Google

The Google People API provides access to contacts with rich metadata (names, emails, phones, addresses, organizations, etc.).

## Decision

### API

Use the Google People API (`people.googleapis.com`):
- `people.connections.list` - list user's contacts
- `people.get` - get single contact
- `people.createContact` - create contact
- `people.updateContact` - update contact
- `people.deleteContact` - delete contact

Required OAuth scope: `https://www.googleapis.com/auth/contacts`

### Commands

```bash
# Clone all contacts (default: markdown for viewing)
gax contacts clone                     # contacts.md
gax contacts clone -o contacts.md      # explicit

# Clone as JSONL (full data, scriptable)
gax contacts clone -f jsonl            # contacts.jsonl

# Clone as TSV (selected fields, spreadsheet editing)
gax contacts clone -f tsv                              # default fields
gax contacts clone -f tsv --fields "name,email,phone"  # custom fields

# Pull latest (respects format/fields in header)
gax contacts pull contacts.jsonl
gax contacts pull contacts.tsv

# Push changes (JSONL or TSV only)
gax contacts push contacts.jsonl
gax contacts push contacts.tsv

# List available fields for TSV
gax contacts fields
```

### File Formats

Three formats with different use cases:

| Format | Extension | Use Case | Editable |
|--------|-----------|----------|----------|
| MD | `.contacts.md` | Human-readable view (default) | No |
| JSONL | `.contacts.jsonl` | Full data, scripting, round-trip | Yes |
| TSV | `.contacts.tsv` | Spreadsheet editing, selected fields | Yes |

```bash
# Default: markdown for viewing
gax contacts clone contacts.md

# JSONL: all fields, scriptable
gax contacts clone -f jsonl contacts.jsonl

# TSV: selected fields for spreadsheet editing
gax contacts clone -f tsv --fields "name,email,phone" contacts.tsv
```

#### Markdown Format (default, view-only)

Human-readable format for quick review. Not editable.

```
---
type: gax/contacts
format: md
pulled: 2024-03-25T10:00:00Z
count: 42
---

## Alice Smith
- **Email**: alice@example.com
- **Phone**: +1-555-0101
- **Organization**: Acme Corp (Engineer)
- **Address**: 123 Main St, City

<!-- resourceName: people/c123 -->

---

## Bob Jones
- **Email**: bob@example.com, bob.jones@work.com
- **Phone**: +1-555-0102
- **Organization**: Widgets Inc (Manager)

<!-- resourceName: people/c456 -->
```

#### JSONL Format (editable, full data)

JSON Lines format - one JSON object per line. Contains all fields from the API.
Round-trip safe, works with `jq`, easy to script.

```
---
type: gax/contacts
format: jsonl
pulled: 2024-03-25T10:00:00Z
count: 42
---
{"resourceName":"people/c123","name":"Alice Smith","email":["alice@example.com"],"phone":["+1-555-0101"],"organization":"Acme Corp","title":"Engineer"}
{"resourceName":"people/c456","name":"Bob Jones","email":["bob@example.com","bob.jones@work.com"],"phone":["+1-555-0102"],"organization":"Widgets Inc","title":"Manager"}
```

Notes:
- Each line is a complete JSON object
- Arrays for multi-value fields (email, phone)
- All available fields included
- Use `jq` for filtering/transformation: `jq 'select(.organization=="Acme Corp")'`

#### TSV Format (editable, selected fields)

Column-oriented projection of the contact data. Select specific fields with `--fields`.
TSV is a subset view of the full JSONL data - pick the columns you need.

```bash
# Clone with specific fields
gax contacts clone -f tsv --fields "name,email,phone" contacts.tsv

# Default TSV fields if --fields not specified
gax contacts clone -f tsv contacts.tsv  # uses: resourceName,name,email,phone
```

```
---
type: gax/contacts
format: tsv
fields: [resourceName, name, email, phone]
pulled: 2024-03-25T10:00:00Z
count: 42
---
resourceName	name	email	phone
people/c123	Alice Smith	alice@example.com	+1-555-0101
people/c456	Bob Jones	bob@example.com;bob.jones@work.com	+1-555-0102
```

**TSV Spec:**
- First line after header: column names (matching `fields` in YAML)
- `resourceName` always included (required for updates)
- Multi-value fields (email, phone) joined with semicolon
- Empty fields are empty string (not null/undefined)
- Tab-separated, no quoting (values must not contain tabs/newlines)
- `fields` in header determines columns on pull

**Available fields:**

| Field | API Source | Notes |
|-------|------------|-------|
| resourceName | `resourceName` | Required, contact ID |
| name | `names[0].displayName` | Full name |
| givenName | `names[0].givenName` | First name |
| familyName | `names[0].familyName` | Last name |
| email | `emailAddresses[*].value` | Semicolon-joined |
| phone | `phoneNumbers[*].value` | Semicolon-joined |
| organization | `organizations[0].name` | Company |
| title | `organizations[0].title` | Job title |
| department | `organizations[0].department` | Department |
| address | `addresses[0].formattedValue` | Full address |
| birthday | `birthdays[0].date` | YYYY-MM-DD |
| notes | `biographies[0].value` | Notes |

### Plan/Apply Workflow

Push works with JSONL and TSV formats (MD is view-only):

```bash
$ gax contacts push contacts.gax
Plan:
  Create: 2 contacts
  Update: 5 contacts
  Delete: 0 contacts

Changes:
  + New Contact: "Jane Doe <jane@example.com>"
  + New Contact: "John Smith <john@example.com>"
  ~ Update: "Alice Smith" - phone changed
  ~ Update: "Bob Jones" - email added
  ...

Apply these changes? [y/N]
```

### Implementation

```python
# gax/contacts.py

import json

CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
ALL_PERSON_FIELDS = "names,emailAddresses,phoneNumbers,organizations,addresses,birthdays,biographies,nicknames,urls"
DEFAULT_TSV_FIELDS = ["resourceName", "name", "email", "phone"]

def list_contacts(service=None) -> list[dict]:
    """Fetch all contacts with all fields."""
    service = service or get_contacts_service()
    contacts = []
    page_token = None

    while True:
        result = service.people().connections().list(
            resourceName="people/me",
            pageSize=1000,
            personFields=ALL_PERSON_FIELDS,
            pageToken=page_token,
        ).execute()
        contacts.extend(result.get("connections", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return contacts

def normalize_contact(api_contact: dict) -> dict:
    """Normalize API contact to flat structure for JSONL."""
    return {
        "resourceName": api_contact.get("resourceName", ""),
        "name": (api_contact.get("names") or [{}])[0].get("displayName", ""),
        "givenName": (api_contact.get("names") or [{}])[0].get("givenName", ""),
        "familyName": (api_contact.get("names") or [{}])[0].get("familyName", ""),
        "email": [e["value"] for e in api_contact.get("emailAddresses", [])],
        "phone": [p["value"] for p in api_contact.get("phoneNumbers", [])],
        "organization": (api_contact.get("organizations") or [{}])[0].get("name", ""),
        "title": (api_contact.get("organizations") or [{}])[0].get("title", ""),
        # ... more fields
    }

def contacts_to_jsonl(contacts: list[dict]) -> str:
    """Format contacts as JSONL."""
    normalized = [normalize_contact(c) for c in contacts]
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in normalized)

def contacts_to_tsv(contacts: list[dict], fields: list[str]) -> str:
    """Format contacts as TSV with selected fields."""
    normalized = [normalize_contact(c) for c in contacts]

    def format_value(v):
        if isinstance(v, list):
            return ";".join(v)
        return str(v) if v else ""

    lines = ["\t".join(fields)]
    for contact in normalized:
        row = [format_value(contact.get(f, "")) for f in fields]
        lines.append("\t".join(row))
    return "\n".join(lines)

def contacts_to_markdown(contacts: list[dict]) -> str:
    """Format contacts as markdown."""
    normalized = [normalize_contact(c) for c in contacts]
    sections = []
    for c in normalized:
        lines = [f"## {c['name']}"]
        if c["email"]:
            lines.append(f"- **Email**: {', '.join(c['email'])}")
        if c["phone"]:
            lines.append(f"- **Phone**: {', '.join(c['phone'])}")
        if c["organization"]:
            org = c["organization"]
            if c["title"]:
                org += f" ({c['title']})"
            lines.append(f"- **Organization**: {org}")
        lines.append(f"\n<!-- resourceName: {c['resourceName']} -->")
        sections.append("\n".join(lines))
    return "\n\n---\n\n".join(sections)
```

## Consequences

### Positive

- **Backup/export**: Easy to export contacts for backup
- **Bulk editing**: TSV format enables spreadsheet editing (Excel, Google Sheets)
- **Scriptable**: JSONL works with `jq` and standard Unix tools
- **Consistent with gax patterns**: clone/pull/push workflow
- **Human readable**: MD format for quick review
- **Flexible**: TSV fields are configurable per-file

### Negative

- **New OAuth scope**: Requires contacts permission
- **Complex data model**: Contacts have many optional fields
- **Multi-value fields**: Email/phone arrays need special handling in TSV

### Neutral

- **Read-mostly**: Most users will clone/pull, few will push

## Outlook

### Phase 1: Read-only

Implement clone and pull only. TSV and MD formats.

### Phase 2: Push Support

Add plan/apply for updating contacts. Handle creates, updates, deletes.

### Phase 3: Groups

Support contact groups/labels:
```bash
gax contacts groups clone    # list groups
gax contacts clone -g "Work" # filter by group
```

### Phase 4: Sync

Bidirectional sync with conflict detection.

## Non-Goals

- **Photo sync**: Contact photos are not included
- **Linked contacts**: Google's merged contact view is not exposed
- **Other directory**: Only personal contacts, not Google Workspace directory
