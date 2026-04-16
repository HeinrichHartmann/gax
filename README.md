# gax - Google Access CLI

Sync Google Workspace (Docs, Sheets, Gmail, Calendar) to local files that are human-readable, machine-readable, and git-friendly.

Designed to be equally usable by humans and AI agents, gax facilitates AI-enhanced workflows where LLMs can read, analyze, and modify Google Workspace content through familiar file operations.

## Design

- **Native abstractions** - Work at the same level as upstream: email threads (not individual messages), documents and sheets with tabs (not pages or cells)
- **YAML + Markdown format** - Each `.gax.md` file has a YAML header with provenance metadata, followed by plain text content (Markdown, CSV, TSV). Editors see markdown; gax sees a sync file
- **Clone/Checkout pattern** - `clone` creates a single file (one tab), `checkout` creates a directory (all tabs). `pull` updates existing files from remote
- **Push with confirmation** - Sheets and Docs support `push` for local edits. Shows diff and prompts before overwriting remote content
- **Plan/Apply pattern** - Bulk operations (labels, filters, mail triage) go through a two-phase workflow: `plan` generates a changeset for review, `apply` executes it

## Quick Start

```bash
# Install
uv tool install git+https://github.com/HeinrichHartmann/gax.git

# Authenticate
gax auth login

# Clone a Google Doc (first tab)
gax doc clone https://docs.google.com/document/d/.../edit

# Checkout all tabs as a directory
gax doc checkout https://docs.google.com/document/d/.../edit

# Clone a spreadsheet tab
gax sheet clone https://docs.google.com/spreadsheets/d/.../edit

# Pull updates
gax pull *.gax.md

# Push local changes
gax push doc.gax.md
```

## Setup

```bash
gax auth login    # Opens browser for Google OAuth
gax auth status   # Check authentication
```

Requires `~/.config/gax/credentials.json` from [Google Cloud Console](https://console.cloud.google.com/apis/credentials).

## Reference

Full reference via `gax man` or `man gax` (if man page is installed).

<!-- BEGIN GAX MAN -->
```
GAX(1)

NAME
    gax - Google Access CLI

COMMANDS

  Main:

    clone:
      gax clone [URL]
          Clone a Google resource from URL.
          -o, --output: Output file
          -f, --format: Output format (for forms)

    checkout:
      gax checkout [URL]
          Checkout a Google resource from URL into a folder of individual files.
          -o, --output: Output folder
          -f, --format: Output format (for sheets)

    pull:
      gax pull [FILES]
          Pull/update .gax.md file(s) or .gax.md.d folder(s) from their sources.
          -v, --verbose: Verbose output

    push:
      gax push [FILES]
          Push local .gax.md file(s) or .gax.md.d folder(s) to their sources.
          -y, --yes: Skip confirmation prompts
          --with-formulas: Interpret formulas (sheets only)

  Resources:

    cal:
      gax cal calendars
          List available calendars.
      gax cal checkout [CALENDAR]
          Checkout events as individual .cal.gax.md files into a folder.
      gax cal clone [CALENDAR]
          Clone events to a .cal.gax.md file.
      gax cal event clone [ID_OR_URL]
          Clone an event to a local .cal.gax.md file.
      gax cal event push [FILE_PATH]
          Push local changes to API.
      gax cal list [CALENDAR]
          List events from a calendar.
      gax cal pull [FILE]
          Pull latest events to existing file.

    contacts [unstable]:
      gax contacts clone
          Clone all contacts to a local file.
      gax contacts plan [FILE]
          Generate a plan for syncing local contacts to Google.
      gax contacts apply [PLAN_FILE]
          Apply a contacts plan to Google.
      gax contacts pull [FILE]
          Pull latest contacts from Google.

    doc:
      gax doc clone [URL]
          Clone a Google Doc to a local .doc.gax.md file.
      gax doc checkout [URL]
          Checkout all tabs to individual files in a folder.
      gax doc pull [FILE]
          Pull latest content from Google Docs to local file.
      gax doc tab clone [URL] [TAB_NAME]
          Clone a single tab to a .tab.gax.md file.
      gax doc tab push [FILE]
          Push local changes to a single tab.
      gax doc tab diff [FILE]
          Show diff between local file and remote tab.

    draft:
      gax draft new
          Create a new local draft file.
      gax draft clone [DRAFT_ID_OR_URL]
          Clone an existing draft from Gmail.
      gax draft push [FILE]
          Push local draft to Gmail.
      gax draft pull [FILE]
          Pull latest content from Gmail draft.
      gax draft list
          List Gmail drafts (TSV output).

    file [unstable]:
      gax file clone [URL_OR_ID]
          Clone a file from Google Drive.
      gax file push [FILE_PATH]
          Push local file to Google Drive.
      gax file pull [FILE_PATH]
          Pull latest version of a file from Google Drive.

    form [unstable]:
      gax form clone [URL]
          Clone a Google Form to a local .form.gax.md file.
      gax form plan [FILE]
          Generate a plan from edited form file.
      gax form apply [PLAN_FILE]
          Apply form changes from a plan file.
      gax form pull [FILE]
          Pull latest form definition from Google Forms.

    mail:
      gax mail clone [THREAD_ID_OR_URL]
          Clone a single email thread to a local .mail.gax.md file.
      gax mail pull [PATH]
          Pull latest messages for .mail.gax.md file(s).
      gax mail reply [FILE_OR_URL]
          Create a reply draft from a thread.

    mail-filter [unstable]:
      gax mail-filter clone
          Clone Gmail filters to a .gax.md file.
      gax mail-filter plan [FILE]
          Generate plan from edited filters file.
      gax mail-filter apply [PLAN_FILE]
          Apply filter changes from plan file.

    mail-label [unstable]:
      gax mail-label clone
          Clone Gmail labels to a .gax.md file.
      gax mail-label plan [FILE]
          Generate plan from edited labels file.
      gax mail-label apply [PLAN_FILE]
          Apply label changes from plan file.

    mailbox:
      gax mailbox clone
          Clone threads from Gmail for bulk labeling.
      gax mailbox fetch
          Fetch full threads matching query into a folder.
      gax mailbox plan [FILE]
          Generate plan from edited list file.
      gax mailbox apply [PLAN_FILE]
          Apply label changes from plan.
      gax mailbox pull [FILE]
          Update a .gax.md file by re-fetching from Gmail.

    sheet:
      gax sheet clone [URL]
          Clone first tab from a spreadsheet to a .sheet.gax.md file.
      gax sheet checkout [URL]
          Checkout all tabs to individual files in a folder.
      gax sheet push [FOLDER]
          Push all tabs in a checkout folder to Google Sheets.
      gax sheet pull [FILE]
          Pull latest data for all tabs.
      gax sheet tab clone [URL] [TAB_NAME]
          Clone a single tab to a .sheet.gax.md file.
      gax sheet tab push [FILE]
          Push local data to a single tab.

  Utility:

    auth:
      gax auth login
          Authenticate with Google (opens browser).
      gax auth logout
          Remove stored authentication token.
      gax auth status
          Show authentication status.

    issue:
      gax issue [TITLE] [--type bug|feature]
          File a GitHub issue for gax (opens via gh CLI). Defaults to --type bug.

FILES
    .doc.gax.md           Document
    .tab.gax.md           Single document tab
    .sheet.gax.md         Spreadsheet data
    .mail.gax.md          Email thread
    .draft.gax.md         Email draft
    .cal.gax.md           Calendar event
    .form.gax.md          Google Form definition
    .gax.md               Mail list (TSV with YAML header)
    .label.mail.gax.md    Gmail labels state
    .filter.mail.gax.md   Gmail filters state
```
<!-- END GAX MAN -->

## File Format

Every `.gax.md` file is self-describing: a YAML header contains the resource type and source URL, followed by the content in Markdown or CSV. This allows `gax pull FILE` to update any file without additional arguments.

```
---
type: gax/sheet
title: Budget 2026
source: https://docs.google.com/spreadsheets/d/1ABC.../edit
tab: Revenue
pulled: 2026-04-15T10:00:00Z
---
Month,Income,Expenses
Jan,5000,3200
Feb,5200,3100
```

### Clone vs Checkout

- **`clone`** creates a single `.gax.md` file (one tab). For multi-tab resources, clones the first tab and shows a hint
- **`checkout`** creates a `.gax.md.d/` directory with individual files per tab, plus a `.gax.yaml` metadata file

```
Budget.sheet.gax.md.d/
  .gax.yaml                  # Folder metadata
  Revenue.tab.sheet.gax.md   # Individual tab
  Expenses.tab.sheet.gax.md  # Individual tab
```

## Man Page

Generate and install the man page:

```bash
make man                              # Generate man/gax.1
sudo cp man/gax.1 /usr/share/man/man1/  # Install system-wide
```

## License

MIT
