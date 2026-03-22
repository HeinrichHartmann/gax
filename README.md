# gax - Google Access CLI

Sync Google Workspace (Sheets, Docs, Gmail, Calendar) to local files that are human-readable, machine-readable, and git-friendly.

## Design

- **YAML frontmatter** stores metadata (source URL, IDs) for re-sync
- **Plain text body** (CSV, Markdown, TSV) for easy editing and diffing
- **Clone/Pull pattern** like git - clone once, pull to update
- **Plan/Apply pattern** for bulk operations - preview changes before applying
- **Bi-directional** for Sheets (push), declarative for Labels/Relabel

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

## Commands

### Sheets

Two-way sync for Google Sheets.

```bash
# Clone all tabs
gax sheet clone URL

# Clone single tab
gax sheet tab clone URL TAB

# Pull latest / Push changes
gax sheet pull file.sheet.gax
gax sheet tab push file.sheet.gax --with-formulas
```

### Docs

Read-only sync for Google Docs (supports multi-tab documents).

```bash
gax doc clone URL
gax doc pull Document.doc.gax
gax doc clone URL --with-comments
```

### Mail

Archive and manage Gmail.

```bash
# Search threads
gax mail search "from:alice after:2025/01/01"

# Clone thread(s)
gax mail clone THREAD_ID
gax mail clone "label:Inbox" --to Inbox/

# Pull updates
gax mail pull thread.mail.gax
```

### Mail Drafts

Compose and send drafts.

```bash
# Create draft
gax mail draft create draft.gax

# List / Pull / Send
gax mail draft list
gax mail draft pull draft.gax
gax mail draft send draft.gax
```

### Mail Relabel (Bulk Label Operations)

Declarative bulk labeling with IaC-style workflow.

```bash
# Clone threads for relabeling
gax mail relabel clone "in:inbox" -o inbox.gax --limit 50

# Edit the .gax file:
#   sys column: I=Inbox S=Spam T=Trash U=Unread *=Starred !=Important
#   cat column: P=Personal U=Updates R=Promotions S=Social F=Forums
#   labels column: user labels (comma-separated)

# Generate plan and apply
gax mail relabel plan inbox.gax
gax mail relabel apply relabel.plan.yaml -y

# Update existing file
gax mail relabel pull inbox.gax
```

**Example .gax file:**
```
---
type: gax/relabel
query: in:inbox
limit: 50
---
id	from	subject	date	sys	cat	labels
19d1586c...	alice@example.com	Meeting notes	2026-03-22	I	U	work,projects/active
19d1445a...	spam@fake.com	Buy now!!!	2026-03-22	S
```

### Labels (Declarative Management)

Manage Gmail labels declaratively.

```bash
# Export labels to YAML
gax label pull -o labels.yaml

# Edit: add/rename/delete labels, change visibility
# Then generate plan and apply
gax label plan labels.yaml
gax label apply labels.plan.yaml -y

# List labels (TSV)
gax label list
```

**Visibility settings:**
- `visible`: show | hide | unread (sidebar)
- `show_in_list`: show | hide (on messages)

**Example labels.yaml:**
```yaml
labels:
- name: Work
- name: Projects/Active
  visible: show
- name: Archive
  visible: hide
  show_in_list: hide
- name: OldName
  rename_from: NewName
```

### Calendar

Sync Google Calendar events.

```bash
# Clone calendar
gax cal clone CALENDAR_ID -o calendar.cal.gax

# Pull updates
gax cal pull calendar.cal.gax

# List calendars
gax cal list
```

## File Formats

| Extension | Content |
|-----------|---------|
| `.sheet.gax` | Spreadsheet (CSV/TSV/JSON) |
| `.doc.gax` | Document (Markdown) |
| `.mail.gax` | Email thread (Markdown) |
| `.draft.gax` | Email draft (Markdown) |
| `.cal.gax` | Calendar events (YAML) |
| `.gax` | Relabel state (TSV with YAML header) |

### Multipart Format

Documents with multiple sections (Doc tabs, Mail threads) use a **multipart format**: multiple YAML+content blocks concatenated. Each section is self-contained.

```
---
title: Project Plan
source: https://docs.google.com/...
tab: Overview
---
# Overview

Project goals...

---
title: Project Plan
source: https://docs.google.com/...
tab: Timeline
---
# Timeline

| Phase | Date |
|-------|------|
| Alpha | Q1   |
```

## Help

```bash
gax --help
gax <command> --help
gax man  # Full manual
```

## License

MIT
