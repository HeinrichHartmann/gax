# GAX(1) - Google Access CLI

## NAME

**gax** - human and machine-readable sync for Google Workspace

## DESCRIPTION

**gax** enables humans and AI agents to collaborate on data in Google Workspace (Sheets, Docs, Calendar, Gmail). It syncs documents to local files that are both human-readable and machine-readable, with YAML metadata headers.

See DESIGN.md for the full vision.

## SYNOPSIS

```
gax auth login
gax auth status
gax auth logout

gax sheet pull <file>
gax sheet push <file> [--with-formulas]
gax sheet clone <url> <tab> [--format FORMAT]

gax doc clone <url>
gax doc pull <file>
```

## COMMANDS

### Authentication

**gax auth login**
: Authenticate with Google via OAuth browser flow. Stores token in `~/.config/gax/token.json`.

**gax auth status**
: Show current authentication status.

**gax auth logout**
: Remove stored authentication token.

### Google Sheets

**gax sheet pull** *file*
: Pull data from Google Sheets to local file. Reads spreadsheet ID and tab from file frontmatter.

**gax sheet push** *file* [**--with-formulas**]
: Push local file data to Google Sheets. With `--with-formulas`, cell values starting with `=` are interpreted as formulas.

**gax sheet clone** *url* *tab* [**--format** *FORMAT*]
: Initialize a new `.sheet.gax` file from a Google Sheets URL. Outputs to stdout.

### Google Docs

**gax doc clone** *url*
: Clone a Google Doc to a local `.doc.gax` file. Uses multipart YAML-markdown format (see ADR 002).

**gax doc pull** *file*
: Pull latest content from Google Docs. Reads source URL from file frontmatter.

## FILE FORMATS

### Sheets (`.sheet.gax`)

YAML frontmatter followed by tabular data:

```
---
spreadsheet_id: 16f107gJ4_hqkvhwIUXIwxS5-CaPspBmYQs6NE-lvsBg
tab: Actuals
format: csv
url: https://docs.google.com/spreadsheets/d/.../edit
---
Date,Type,Amount
2025-12-09,Revenue,10000
2025-12-18,Expense,-5000
```

### Docs (`.doc.gax`)

Multipart YAML-markdown format. Each section (tab) is self-contained:

```
---
title: My Document
source: https://docs.google.com/document/d/xxx/edit
time: 2026-03-20T10:00:00Z
section: 1
section_title: Overview
---
# Overview

Document content here...
```

See ADR 002 for full multipart format specification.

## FORMATS (Sheets)

| Format | Description |
|--------|-------------|
| csv | Comma-separated values |
| tsv | Tab-separated values |
| psv | Pipe-separated values |
| json | JSON array of objects |
| jsonl | JSON lines |
| markdown | Markdown table |

## EXAMPLES

### Sheets

Initialize from existing sheet:

```
gax sheet clone "https://docs.google.com/spreadsheets/d/16f1.../edit" \
    Actuals --format csv > budget.sheet.gax
```

Pull latest data:

```
gax sheet pull budget.sheet.gax
```

Edit locally and push:

```
vim budget.sheet.gax
gax sheet push budget.sheet.gax --with-formulas
```

### Docs

Clone a Google Doc:

```
gax doc clone "https://docs.google.com/document/d/1ky1.../edit"
```

Pull latest changes:

```
gax doc pull My_Document.doc.gax
```

## FILES

**~/.config/gax/credentials.json**
: OAuth client credentials (download from Google Cloud Console)

**~/.config/gax/token.json**
: Stored OAuth token (created by `gax auth login`)

## ENVIRONMENT

**GAX_CONFIG_DIR**
: Override default config directory (~/.config/gax)

## EXIT STATUS

| Code | Description |
|------|-------------|
| 0 | Success |
| 1 | Error (authentication, API, file) |

## SEE ALSO

- DESIGN.md - Architecture and design decisions
- ADR/002-multipart-markdown-format.md - Multipart format spec
- ADR/003-gdoc-sync.md - Google Docs sync design

## AUTHORS

Heinrich Hartmann <heinrich@heinrichhartmann.com>

## LICENSE

MIT
