# GAX Design

**Google Access CLI** - Human and machine-readable sync for Google Workspace

## Vision

GAX enables humans and AI agents to collaborate on data that lives in Google Workspace: Sheets, Docs, Calendar, Gmail, and Drive.

The core idea: sync Google Workspace documents to local files that are both **human-readable** and **machine-readable**, enabling version control, scripting, and AI-assisted workflows.

## Principles

### 1. Human-Readable First

All local files should be readable and editable by humans in any text editor. We prefer:

- **Markdown** for documents and tables
- **YAML frontmatter** for metadata
- **Plain text** over binary formats

### 2. Machine-Readable Always

Files must be parseable by scripts and AI agents. Structured formats enable:

- Automated processing and transformation
- AI agent collaboration (read, modify, push)
- Version control diffs that make sense

### 3. Metadata Travels With Data

Each file contains its own sync metadata in YAML frontmatter:

```yaml
---
spreadsheet_id: 16f107gJ4_hqkvhwIUXIwxS5-CaPspBmYQs6NE-lvsBg
tab: Actuals
format: markdown
---
```

This means you can push a file without remembering where it came from. The file knows.

### 4. Local-First Workflow

Work happens in your project folder:

```
project/
├── budget/
│   ├── actuals.sheet.gax
│   └── planning.sheet.gax
├── docs/
│   └── proposal.doc.gax
└── ...
```

Pull to get latest, edit locally, push to sync back. Git for version control.

## Current State

**Implemented:**
- Google Sheets (`gax gsheet pull/push/init`)
- Formats: CSV, TSV, PSV, JSON, JSONL, Markdown

**Planned:**
- Google Docs (`gax gdoc`)
- Google Calendar (`gax gcal`)
- Gmail (`gax gmail`)
- Google Drive (`gax gdrive`)

## Implementation

Built with Python and UV for:
- Excellent library support (gspread, google-api-python-client)
- Fast iteration
- Easy installation (`uv tool install`)

## File Format Convention

```
*.sheet.gax  - Google Sheets
*.doc.gax    - Google Docs (future)
*.cal.gax    - Calendar events (future)
*.mail.gax   - Email threads (future)
```

All files share the same structure:
1. YAML frontmatter with sync metadata
2. Human-readable content in appropriate format
