# GAX(1) - Google Access CLI

## NAME

**gax** - sync Google Sheets with local files

## SYNOPSIS

```
gax auth login
gax auth status
gax auth logout

gax gsheet pull <file>
gax gsheet push <file> [--with-formulas]
gax gsheet init <url> <tab> [--format FORMAT]
```

## DESCRIPTION

**gax** synchronizes Google Sheets data with local `.sheet.gax` files, enabling version control and local editing of spreadsheet data. Data is synced without affecting Google Sheets formatting (colors, fonts, borders).

## COMMANDS

### Authentication

**gax auth login**
: Authenticate with Google via OAuth browser flow. Stores token in `~/.config/gax/token.json`.

**gax auth status**
: Show current authentication status.

**gax auth logout**
: Remove stored authentication token.

### Google Sheets

**gax gsheet pull** *file*
: Pull data from Google Sheets to local file. Reads spreadsheet ID and tab from file frontmatter.

**gax gsheet push** *file* [**--with-formulas**]
: Push local file data to Google Sheets. With `--with-formulas`, cell values starting with `=` are interpreted as formulas.

**gax gsheet init** *url* *tab* [**--format** *FORMAT*]
: Initialize a new `.sheet.gax` file from a Google Sheets URL. Outputs to stdout.

## FILE FORMAT

Files use YAML frontmatter followed by data:

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

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| spreadsheet_id | yes | Google Sheets document ID |
| tab | yes | Sheet/tab name |
| format | yes | Data format (see FORMATS) |
| url | no | Full URL (informational) |
| range | no | Cell range (default: all) |

## FORMATS

| Format | Extension | Description |
|--------|-----------|-------------|
| csv | .sheet.gax | Comma-separated values |
| tsv | .sheet.gax | Tab-separated values |
| psv | .sheet.gax | Pipe-separated values |
| json | .sheet.gax | JSON array of objects |
| jsonl | .sheet.gax | JSON lines |
| markdown | .sheet.gax | Markdown table |

## EXAMPLES

Initialize from existing sheet:

```
gax gsheet init "https://docs.google.com/spreadsheets/d/16f1.../edit" \
    Actuals --format csv > budget.sheet.gax
```

Pull latest data:

```
gax gsheet pull budget.sheet.gax
```

Edit locally and push:

```
vim budget.sheet.gax
gax gsheet push budget.sheet.gax --with-formulas
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
- https://docs.google.com/spreadsheets - Google Sheets
- https://github.com/burnash/gspread - gspread library

## AUTHORS

Heinrich Hartmann <heinrich@heinrichhartmann.com>

## LICENSE

MIT
