# gax - Google Access CLI

Sync Google Workspace (Sheets, Docs, Gmail, Calendar) to local files that are human-readable, machine-readable, and git-friendly.

Designed to be equally usable by humans and AI agents, gax facilitates AI-enhanced workflows where LLMs can read, analyze, and modify Google Workspace content through familiar file operations.

## Design

- **Native abstractions** - Work at the same level as upstream: email threads (not individual messages), documents and sheets with tabs (not pages or cells)
- **Multipart YAML format** - Each file has a YAML header with provenance metadata, followed by plain text content (Markdown, CSV, TSV). Multi-section resources (threads, tabs) use concatenated YAML+content blocks
- **Clone/Pull pattern** - Like git: `clone` creates a local file, `pull` updates it. The file extension encodes the resource type, so `gax pull *.gax` always works
- **Plan/Apply pattern** - Bulk operations (labels, filters, mail triage) go through a two-phase workflow: `plan` generates a changeset for review, `apply` executes it. No `-y` prompts needed
- **Bi-directional sync** - Sheets and Docs support `push` for edits. Labels, filters, and mail lists use declarative plan/apply

## Install

```bash
uv tool install git+https://github.com/HeinrichHartmann/gax.git
# or
pip install git+https://github.com/HeinrichHartmann/gax.git
```

## Setup

```bash
gax auth login    # Opens browser for Google OAuth
gax auth status   # Check authentication
```

Requires `~/.config/gax/credentials.json` from [Google Cloud Console](https://console.cloud.google.com/apis/credentials).

## Reference

<!-- BEGIN GAX MAN -->
```
GAX(1)

NAME
    gax - Google Access CLI

COMMANDS

  auth:
    gax auth login
        Authenticate with Google (opens browser).
    gax auth logout
        Remove stored authentication token.
    gax auth status
        Show authentication status.

  cal:
    gax cal calendars
        List available calendars.
    gax cal checkout [CALENDAR]
        Checkout events as individual .cal.gax files into a folder.
        -o, --output: Output folder (default: calendar.cal.gax.d)
        --days, -d: Number of days (default: 7)
        --from: Start date (YYYY-MM-DD)
        --to: End date (YYYY-MM-DD)
    gax cal clone [CALENDAR]
        Clone events to a .cal.gax file.
        -o, --output: Output file (default: calendar.cal.gax)
        --days, -d: Number of days (default: 7)
        --from: Start date (YYYY-MM-DD)
        --to: End date (YYYY-MM-DD)
        -v, --verbose: Include event descriptions
    gax cal event clone [ID_OR_URL]
        Clone an event to a local .cal.gax file.
        --cal, -c: Calendar ID (default: primary)
        -o, --output: Output file path
    gax cal event delete [FILE_PATH]
        Delete event from calendar.
        -y, --yes: Skip confirmation
    gax cal event new
        Create a new event file (edit and push to create upstream).
        --cal, -c: Calendar ID (default: primary)
        -o, --output: Output file path
    gax cal event pull [FILE_PATH]
        Pull latest event data from API.
    gax cal event push [FILE_PATH]
        Push local changes to API.
        -y, --yes: Skip confirmation
    gax cal list [CALENDAR]
        List events from a calendar.
        --days, -d: Number of days to show (default: 7)
        --from: Start date (YYYY-MM-DD)
        --to: End date (YYYY-MM-DD)
        --format, -f: Output format (default: md)
        -v, --verbose: Include event descriptions
    gax cal pull [FILE]
        Pull latest events to existing file.

  clone:
    gax clone [URL]
        Clone a Google resource from URL.
        -o, --output: Output file
        -f, --format: Output format (for forms)

  contacts:
    gax contacts apply [PLAN_FILE]
        Apply a contacts plan to Google.
    gax contacts clone
        Clone all contacts to a local file.
        -f, --format: Output format: md (view-only) or jsonl (editable)
        -o, --output: Output file (default: contacts.<format>)
    gax contacts plan [FILE]
        Generate a plan for syncing local contacts to Google.
        -o, --output: Output plan file (default: <file>.plan.yaml)
    gax contacts pull [FILE]
        Pull latest contacts from Google.

  doc:
    gax doc clone [URL]
        Clone a Google Doc to a local .doc.gax file.
        --output, -o: Output file (default: <title>.doc.gax)
        --with-comments: Include document comments as separate sections
    gax doc pull [FILE]
        Pull latest content from Google Docs to local file.
        --with-comments: Include document comments as separate sections
    gax doc tab clone [URL] [TAB_NAME]
        Clone a single tab to a .tab.gax file.
        --output, -o: Output file (default: <tab>.tab.gax)
    gax doc tab diff [FILE]
        Show diff between local file and remote tab.
    gax doc tab import [URL] [FILE]
        Import a markdown file as a new tab in a document.
        --output, -o: Output tracking file (default: <filename>.tab.gax)
    gax doc tab list [URL]
        List tabs in a document (TSV output).
    gax doc tab pull [FILE]
        Pull latest content for a single tab.
    gax doc tab push [FILE]
        Push local changes to a single tab (with confirmation).
        -y, --yes: Skip confirmation prompt

  draft:
    gax draft clone [DRAFT_ID_OR_URL]
        Clone an existing draft from Gmail.
        --output, -o: Output file (default: <subject>.draft.gax)
    gax draft list
        List Gmail drafts (TSV output).
        --limit: Maximum results (default: 100)
    gax draft new
        Create a new local draft file.
        --output, -o: Output file (default: <subject>.draft.gax)
        --to: Recipient email address
        --subject: Email subject
    gax draft pull [FILE]
        Pull latest content from Gmail draft.
    gax draft push [FILE]
        Push local draft to Gmail.
        -y, --yes: Skip confirmation prompt

  form:
    gax form apply [PLAN_FILE]
        Apply form changes from a plan file.
    gax form clone [URL]
        Clone a Google Form to a local .form.gax file.
        --output, -o: Output file (default: <title>.form.gax)
        --format, -f: Content format: md (readable, default) or yaml (round-trip safe)
    gax form plan [FILE]
        Generate a plan from edited form file.
        -o, --output: Output plan file
    gax form pull [FILE]
        Pull latest form definition from Google Forms.

  mail:
    gax mail clone [THREAD_ID_OR_URL]
        Clone a single email thread to a local .mail.gax file.
        --output, -o: Output file
    gax mail pull [PATH]
        Pull latest messages for .mail.gax file(s).
    gax mail reply [FILE_OR_URL]
        Create a reply draft from a thread.
        --output, -o: Output file (default: Re_<subject>.draft.gax)

  mail-filter:
    gax mail-filter apply [PLAN_FILE]
        Apply filter changes from plan file.
    gax mail-filter clone
        Clone Gmail filters to a .gax file.
        -o, --output: Output file (default: mail-filters.gax)
    gax mail-filter list
        List Gmail filters (TSV output).
    gax mail-filter plan [FILE]
        Generate plan from edited filters file.
        -o, --output: Output plan file
    gax mail-filter pull [FILE]
        Pull latest filters to existing file.

  mail-label:
    gax mail-label apply [PLAN_FILE]
        Apply label changes from plan file.
    gax mail-label clone
        Clone Gmail labels to a .gax file.
        -o, --output: Output file (default: mail-labels.gax)
        --all: Include system labels (read-only)
    gax mail-label list
        List Gmail labels (TSV output).
    gax mail-label plan [FILE]
        Generate plan from edited labels file.
        -o, --output: Output plan file
        --delete: Include deletions in plan
    gax mail-label pull [FILE]
        Pull latest labels to existing file.
        --all: Include system labels (read-only)

  mailbox:
    gax mailbox apply [PLAN_FILE]
        Apply label changes from plan.
    gax mailbox clone
        Clone threads from Gmail for bulk labeling.
        -o, --output: Output file (default: mailbox.gax)
        -q, --query: Search query (default: in:inbox)
        --limit: Maximum threads (default: 50)
    gax mailbox fetch
        Fetch full threads matching query into a folder.
        -o, --output: Output folder (default: mailbox.gax.d)
        -q, --query: Search query (default: in:inbox)
        --limit: Maximum threads (default: 50)
    gax mailbox plan [FILE]
        Generate plan from edited list file.
        -o, --output: Output file (default: mailbox.plan.yaml)
    gax mailbox pull [FILE]
        Update a .gax file by re-fetching from Gmail.

  pull:
    gax pull [FILES]
        Pull/update .gax file(s) from their sources.
        -v, --verbose: Verbose output

  sheet:
    gax sheet checkout [URL]
        Checkout all tabs to individual files in a folder.
        -o, --output: Output folder (default: <title>.sheet.gax.d)
        -f, --format: Output format: md, csv, tsv, psv, json, jsonl
    gax sheet clone [URL]
        Clone all tabs from a spreadsheet to a multipart .sheet.gax file.
        --output, -o: Output file (default: <title>.sheet.gax)
        -f, --format: Output format: md, csv, tsv, psv, json, jsonl
    gax sheet pull [FILE]
        Pull latest data for all tabs in a multipart file.
    gax sheet tab clone [URL] [TAB_NAME]
        Clone a single tab to a .sheet.gax file.
        --output, -o: Output file (default: <tab>.sheet.gax)
        -f, --format: Output format: md, csv, tsv, psv, json, jsonl
    gax sheet tab list [URL]
        List tabs in a spreadsheet (TSV output).
    gax sheet tab pull [FILE]
        Pull latest data for a single tab.
    gax sheet tab push [FILE]
        Push local data to a single tab.
        --with-formulas: Interpret formulas (e.g. =SUM(A1:A10))
        -y, --yes: Skip confirmation prompt

FILES
    .sheet.gax         Spreadsheet data (single or multipart)
    .doc.gax           Document (all tabs, multipart)
    .tab.gax           Single document tab
    .mail.gax          Email thread
    .draft.gax         Email draft
    .cal.gax           Calendar event
    .form.gax          Google Form definition
    .gax               Mail list (TSV with YAML header)
    .label.mail.gax    Gmail labels state
    .filter.mail.gax   Gmail filters state

    ~/.config/gax/credentials.json    OAuth credentials
    ~/.config/gax/token.json          Access token

SEE ALSO
    gax <command> --help
```
<!-- END GAX MAN -->

## File Format

Every `.gax` file is self-describing: a YAML header contains the resource type and source URL, followed by the content. This allows `gax pull FILE` to update any file without additional arguments.

### Extension Convention

The file extension mirrors the command path:

| Command | Extension | Notes |
|---------|-----------|-------|
| `gax mail` | `.mail.gax` | Individual thread |
| `gax draft` | `.draft.gax` | Email draft |
| `gax mailbox` | `.mailbox.gax` | Thread collection |
| `gax mail-label` | `mail-labels.gax` | Gmail labels |
| `gax mail-filter` | `mail-filters.gax` | Gmail filters |
| `gax sheet` | `.sheet.gax` | Multipart spreadsheet |
| `gax sheet tab` | `.tab.sheet.gax` | Individual tab |
| `gax doc` | `.doc.gax` | Multipart document |
| `gax doc tab` | `.tab.gax` | Individual doc tab |
| `gax cal` | `.cal.gax` | Calendar event |
| `gax form` | `.form.gax` | Google Form |
| `gax contacts` | `.contacts.gax` or `.jsonl` | Contact list |

### Single-Section Files

Simple resources have one YAML header followed by content:

```
---
type: gax/sheet
source: https://docs.google.com/spreadsheets/d/1ABC.../edit
tab: Sheet1
format: csv
pulled: 2026-03-23T10:00:00Z
---
name,email,role
Alice,alice@example.com,admin
Bob,bob@example.com,user
```

### Multipart Files

Resources with multiple sections (email threads, multi-tab documents) concatenate YAML+content blocks:

```
---
type: gax/mail
thread_id: 18abc123def
source: https://mail.google.com/mail/u/0/#inbox/18abc123def
subject: Project Update
---
From: alice@example.com
Date: 2026-03-22 10:00

Here's the latest update...

---
type: gax/mail
thread_id: 18abc123def
message_id: <reply-456@mail.gmail.com>
---
From: bob@example.com
Date: 2026-03-22 11:30

Thanks for the update!
```

### Checkout Folders (`.gax.d` directories)

Checkout commands create folders with individual resource files plus shared metadata:

```
Budget.sheet.gax.d/
  .gax.yaml                  # Folder metadata (spreadsheet_id, url, format)
  Summary.tab.sheet.gax      # Individual tab (full YAML header + content)
  Expenses.tab.sheet.gax     # Individual tab (full YAML header + content)
```

Each file in a `.gax.d` folder is:
- **Self-describing** - Contains full YAML headers for independent pulling
- **Pullable** - Can be updated with `gax pull *.tab.sheet.gax`
- **Named by command** - Extension matches the command path (e.g., `gax sheet tab` → `.tab.sheet.gax`)

The `.gax.yaml` file contains shared metadata (spreadsheet URL, format, etc.) for the entire checkout.

## License

MIT
