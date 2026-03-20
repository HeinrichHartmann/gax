# gax - Google Access CLI

Sync Google Workspace (Sheets, Docs, Gmail) to local files that are human-readable, machine-readable, and git-friendly.

## Design

- **YAML frontmatter** stores metadata (source URL, IDs) for re-sync
- **Plain text body** (CSV, Markdown) for easy editing and diffing
- **Clone/Pull pattern** like git - clone once, pull to update
- **Bi-directional** for Sheets (push), read-only archive for Docs/Mail

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

## Sheets

Two-way sync for Google Sheets tabs.

```bash
# Clone a tab to local file (outputs to stdout)
gax sheet clone URL TAB > budget.sheet.gax

# Pull latest data
gax sheet pull budget.sheet.gax

# Edit locally, then push back
gax sheet push budget.sheet.gax
gax sheet push budget.sheet.gax --with-formulas  # preserve =SUM(...) etc.
```

**Arguments:**
- `URL` - Google Sheets URL
- `TAB` - Tab/sheet name to sync
- `--format` - Output format: csv (default), tsv, json, jsonl, markdown

## Docs

Read-only sync for Google Docs (supports multi-tab documents).

```bash
# Clone document
gax doc clone URL

# Pull latest
gax doc pull Document.doc.gax

# Include comments
gax doc clone URL --with-comments
gax doc pull Document.doc.gax --with-comments
```

**Arguments:**
- `URL` - Google Docs URL
- `--with-comments` - Include document comments as separate sections
- `-o, --output` - Output file path

## Mail

Archive Gmail threads as local markdown files.

```bash
# List labels
gax mail labels

# Search threads (TSV output)
gax mail search "from:alice after:2025/01/01"
gax mail search "label:Inbox has:attachment" --limit 50

# Clone single thread
gax mail clone THREAD_ID
gax mail clone "https://mail.google.com/..."

# Clone search results to folder
gax mail clone "label:Inbox" --to Inbox/
gax mail clone "from:alice" --to Alice/ --limit 100

# Update existing threads with new messages
gax mail pull thread.mail.gax
gax mail pull Inbox/   # updates all .mail.gax files in folder
```

**Arguments:**
- `QUERY` - Gmail search query (same syntax as Gmail search box)
- `THREAD_ID` - Thread ID or Gmail URL
- `--to` - Target folder for bulk clone
- `--limit` - Max threads to fetch (default: 100)

## File Formats

All files use YAML frontmatter + plain text body:

```
---
source: https://docs.google.com/...
...metadata...
---
Content here (CSV, Markdown, etc.)
```

| Extension | Content |
|-----------|---------|
| `.sheet.gax` | Spreadsheet tab (CSV/TSV/JSON) |
| `.doc.gax` | Document (Markdown) |
| `.mail.gax` | Email thread (Markdown) |

## License

MIT
