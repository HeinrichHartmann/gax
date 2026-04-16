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

    checkout:
      gax checkout [URL]
          Checkout a Google resource from URL into a folder of individual files.
          -o, --output: Output folder
          -f, --format: Output format (for sheets)

    clone:
      gax clone [URL]
          Clone a Google resource from URL.
          -o, --output: Output file
          -f, --format: Output format (for forms)

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
          -o, --output: Output folder (default: calendar.cal.gax.md.d)
          --days, -d: Number of days (default: 7)
          --from: Start date (YYYY-MM-DD)
          --to: End date (YYYY-MM-DD)
      gax cal clone [CALENDAR]
          Clone events to a .cal.gax.md file.
          -o, --output: Output file (default: calendar.cal.gax.md)
          --days, -d: Number of days (default: 7)
          --from: Start date (YYYY-MM-DD)
          --to: End date (YYYY-MM-DD)
          -v, --verbose: Include event descriptions
      gax cal event clone [ID_OR_URL]
          Clone an event to a local .cal.gax.md file.
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

    contacts [unstable]:
      gax contacts apply [PLAN_FILE]
          Apply a contacts plan to Google.
          -y, --yes: Skip confirmation
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
      gax doc checkout [URL]
          Checkout all tabs to individual files in a folder.
          -o, --output: Output folder (default: <title>.doc.gax.md.d)
      gax doc clone [URL]
          Clone a Google Doc to a local .doc.gax.md file.
          --output, -o: Output file (default: <title>.doc.gax.md)
          --with-comments: Include document comments as separate sections
          -q, --quiet: Suppress multi-tab status message
      gax doc pull [FILE]
          Pull latest content from Google Docs to local file.
          --with-comments: Include document comments as separate sections
      gax doc tab clone [URL] [TAB_NAME]
          Clone a single tab to a .tab.gax.md file.
          --output, -o: Output file (default: <tab>.tab.gax.md)
      gax doc tab diff [FILE]
          Show diff between local file and remote tab.
      gax doc tab import [URL] [FILE]
          Import a markdown file as a new tab in a document.
          --output, -o: Output tracking file (default: <filename>.tab.gax.md)
      gax doc tab list [URL]
          List tabs in a document (TSV output).
      gax doc tab pull [FILE]
          Pull latest content for a single tab.
      gax doc tab push [FILE]
          Push local changes to a single tab (with confirmation).
          -y, --yes: Skip confirmation prompt
          --patch: Incremental push: apply only changed elements (experimental)

    draft:
      gax draft clone [DRAFT_ID_OR_URL]
          Clone an existing draft from Gmail.
          --output, -o: Output file (default: <subject>.draft.gax.md)
      gax draft list
          List Gmail drafts (TSV output).
          --limit: Maximum results (default: 100)
      gax draft new
          Create a new local draft file.
          --output, -o: Output file (default: <subject>.draft.gax.md)
          --to: Recipient email address
          --subject: Email subject
      gax draft pull [FILE]
          Pull latest content from Gmail draft.
      gax draft push [FILE]
          Push local draft to Gmail.
          -y, --yes: Skip confirmation prompt

    file [unstable]:
      gax file clone [URL_OR_ID]
          Clone a file from Google Drive.
          -o, --output: Output file path
      gax file pull [FILE_PATH]
          Pull latest version of a file from Google Drive.
      gax file push [FILE_PATH]
          Push local file to Google Drive.
          --public: Make file publicly accessible
          -y, --yes: Skip confirmation

    form [unstable]:
      gax form apply [PLAN_FILE]
          Apply form changes from a plan file.
          -y, --yes: Skip confirmation
      gax form clone [URL]
          Clone a Google Form to a local .form.gax.md file.
          --output, -o: Output file (default: <title>.form.gax.md)
          --format, -f: Content format: md (readable, default) or yaml (round-trip safe)
      gax form plan [FILE]
          Generate a plan from edited form file.
          -o, --output: Output plan file
      gax form pull [FILE]
          Pull latest form definition from Google Forms.

    mail:
      gax mail clone [THREAD_ID_OR_URL]
          Clone a single email thread to a local .mail.gax.md file.
          --output, -o: Output file
      gax mail pull [PATH]
          Pull latest messages for .mail.gax.md file(s).
      gax mail reply [FILE_OR_URL]
          Create a reply draft from a thread.
          --output, -o: Output file (default: Re_<subject>.draft.gax.md)

    mail-filter [unstable]:
      gax mail-filter apply [PLAN_FILE]
          Apply filter changes from plan file.
          -y, --yes: Skip confirmation
      gax mail-filter clone
          Clone Gmail filters to a .gax.md file.
          -o, --output: Output file (default: mail-filters.gax.md)
      gax mail-filter list
          List Gmail filters (TSV output).
      gax mail-filter plan [FILE]
          Generate plan from edited filters file.
          -o, --output: Output plan file
      gax mail-filter pull [FILE]
          Pull latest filters to existing file.

    mail-label [unstable]:
      gax mail-label apply [PLAN_FILE]
          Apply label changes from plan file.
          -y, --yes: Skip confirmation
      gax mail-label clone
          Clone Gmail labels to a .gax.md file.
          -o, --output: Output file (default: mail-labels.gax.md)
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
          -y, --yes: Skip confirmation
      gax mailbox clone
          Clone threads from Gmail for bulk labeling.
          -o, --output: Output file (default: mailbox.gax.md)
          -q, --query: Search query (default: in:inbox)
          --limit: Maximum threads (default: 50)
      gax mailbox fetch
          Fetch full threads matching query into a folder.
          -o, --output: Output folder (default: mailbox.gax.md.d)
          -q, --query: Search query (default: in:inbox)
          --limit: Maximum threads (default: 50)
      gax mailbox plan [FILE]
          Generate plan from edited list file.
          -o, --output: Output file (default: mailbox.plan.yaml)
      gax mailbox pull [FILE]
          Update a .gax.md file by re-fetching from Gmail.

    sheet:
      gax sheet apply [FOLDER]
          Apply planned changes by pushing to Google Sheets.
          --with-formulas: Interpret formulas (e.g. =SUM(A1:A10))
          -y, --yes: Skip confirmation
      gax sheet checkout [URL]
          Checkout all tabs to individual files in a folder.
          -o, --output: Output folder (default: <title>.sheet.gax.md.d)
          -f, --format: Output format: md, csv, tsv, psv, json, jsonl
      gax sheet clone [URL]
          Clone first tab from a spreadsheet to a .sheet.gax.md file.
          --output, -o: Output file (default: <title>.sheet.gax.md)
          -f, --format: Output format: md, csv, tsv, psv, json, jsonl
          -q, --quiet: Suppress multi-tab status message
      gax sheet plan [FOLDER]
          Show what changes would be pushed to Google Sheets.
      gax sheet pull [FILE]
          Pull latest data for all tabs in a multipart file or checkout folder.
      gax sheet push [FOLDER]
          Push all tabs in a checkout folder to Google Sheets.
          --with-formulas: Interpret formulas (e.g. =SUM(A1:A10))
          -y, --yes: Skip confirmation prompt
      gax sheet tab clone [URL] [TAB_NAME]
          Clone a single tab to a .sheet.gax.md file.
          --output, -o: Output file (default: <tab>.sheet.gax.md)
          -f, --format: Output format: md, csv, tsv, psv, json, jsonl
      gax sheet tab list [URL]
          List tabs in a spreadsheet (TSV output).
      gax sheet tab pull [FILE]
          Pull latest data for a single tab.
      gax sheet tab push [FILE]
          Push local data to a single tab.
          --with-formulas: Interpret formulas (e.g. =SUM(A1:A10))
          -y, --yes: Skip confirmation prompt

  Utility:

    auth:
      gax auth login
          Authenticate with Google (opens browser).
      gax auth logout
          Remove stored authentication token.
      gax auth status
          Show authentication status.

    issue:
      gax issue [TITLE]
          File a GitHub issue for gax (opens via gh CLI).
          --body, -b: Issue description
          --type: Issue type (sets the GitHub label)

    upgrade:
      gax upgrade
          Upgrade gax to the latest version from GitHub (uv tool install path).

FILES
    .sheet.gax.md         Spreadsheet data
    .doc.gax.md           Document
    .tab.gax.md           Single document tab
    .mail.gax.md          Email thread
    .draft.gax.md         Email draft
    .cal.gax.md           Calendar event
    .form.gax.md          Google Form definition
    .gax.md               Mail list (TSV with YAML header)
    .label.mail.gax.md    Gmail labels state
    .filter.mail.gax.md   Gmail filters state

    ~/.config/gax/credentials.json    OAuth credentials
    ~/.config/gax/token.json          Access token

SEE ALSO
    gax <command> --help
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

## FAQ

### How do I get Google OAuth credentials?

GAX requires OAuth 2.0 credentials from Google Cloud Platform to access your Google Workspace data. Here's how to set them up:

#### 1. Create a Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **NEW PROJECT**
3. Enter a project name (e.g., "GAX CLI Access") and click **CREATE**
4. Wait for the project to be created and selected

#### 2. Enable Required APIs

1. In the Google Cloud Console, go to **APIs & Services** → **Library**
   
   [Go to API Library](https://console.cloud.google.com/apis/library)

2. Search for and enable these APIs (click each, then click **ENABLE**):
   - **Google Drive API** (required for all file access)
   - **Google Docs API** (for document sync)
   - **Google Sheets API** (for spreadsheet sync)
   - **Gmail API** (for email operations)
   - **Google Calendar API** (for calendar sync)
   - **Google Forms API** (for form management)
   - **People API** (for contacts sync)

#### 3. Configure OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**
   
   [Go to OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent)

2. Choose **External** user type and click **CREATE**

3. Fill in the required fields:
   - **App name**: `GAX CLI` (or any name you prefer)
   - **User support email**: Your email address
   - **Application logo**: Optional
   - **App domain**: Leave blank for personal use
   - **Authorized domains**: Leave blank for personal use
   - **Developer contact information**: Your email address

4. Click **SAVE AND CONTINUE**

5. On the **Scopes** page, click **SAVE AND CONTINUE** (GAX will request scopes as needed)

6. On the **Test users** page:
   - Click **ADD USERS**
   - Add your Gmail address
   - Click **SAVE AND CONTINUE**

7. Review and click **BACK TO DASHBOARD**

#### 4. Create OAuth Client ID

1. Go to **APIs & Services** → **Credentials**
   
   [Go to Credentials](https://console.cloud.google.com/apis/credentials)

2. Click **CREATE CREDENTIALS** → **OAuth client ID**

3. Choose **Application type** → **Desktop application**

4. Enter name: `GAX CLI Client`

5. Click **CREATE**

6. In the popup, click **DOWNLOAD JSON**

7. Save the downloaded file as `~/.config/gax/credentials.json`

```bash
# Create the config directory if it doesn't exist
mkdir -p ~/.config/gax

# Move your downloaded file (adjust the filename as needed)
mv ~/Downloads/client_*.json ~/.config/gax/credentials.json
```

#### 5. First Authentication

Run the authentication flow:

```bash
gax auth login
```

This will:
1. Open your browser to Google's OAuth page
2. Ask you to sign in and grant permissions
3. Save your access tokens locally

### What permissions does GAX request?

GAX requests these OAuth scopes to access your Google Workspace data:

- `https://www.googleapis.com/auth/spreadsheets` - Read/write Google Sheets
- `https://www.googleapis.com/auth/drive` - Read/write/create Google Drive files
- `https://www.googleapis.com/auth/documents` - Read/write Google Docs
- `https://www.googleapis.com/auth/gmail.readonly` - Read Gmail messages
- `https://www.googleapis.com/auth/gmail.compose` - Send emails and create drafts
- `https://www.googleapis.com/auth/gmail.modify` - Modify Gmail labels and threads
- `https://www.googleapis.com/auth/gmail.settings.basic` - Manage Gmail filters
- `https://www.googleapis.com/auth/calendar` - Read/write Google Calendar events
- `https://www.googleapis.com/auth/forms.body` - Read/write Google Forms
- `https://www.googleapis.com/auth/contacts` - Read/write Google Contacts

### Authentication troubleshooting

**Error: `OAuth credentials not found`**
```bash
# Check that credentials file exists and has correct permissions
ls -la ~/.config/gax/credentials.json
# If missing, re-download from Google Cloud Console
```

**Error: `Access denied` or `OAuth error`**
```bash
# Clear stored tokens and re-authenticate
gax auth logout
gax auth login
```

**Error: `API not enabled`**
- Go to the [Google Cloud Console](https://console.cloud.google.com/apis/library)
- Make sure all required APIs are enabled for your project

**Error: `This app isn't verified`**
- Click **Advanced** → **Go to GAX CLI (unsafe)**
- This is safe for personal use with your own OAuth credentials

### Can I use GAX with G Suite/Workspace accounts?

Yes, but you may need additional setup:

1. **Personal Google account**: Follow the standard setup above
2. **Work/School account**: Your administrator may need to approve the OAuth application or you may need to create the project within your organization's Google Cloud account

Contact your Google Workspace administrator if you encounter access restrictions.

### Where are my credentials stored?

- **OAuth client credentials**: `~/.config/gax/credentials.json`
- **Access tokens**: `~/.config/gax/token.json`

These files contain sensitive authentication data. Keep them secure and never commit them to version control.

## License

MIT
