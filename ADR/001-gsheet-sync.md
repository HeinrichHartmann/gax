# ADR: gax - Google Access CLI

## Status
Implemented

## Context
We need a simple way to sync Google Sheets data with local files for version control and scripting. The tool should be extensible to support multiple output formats and potentially other Google services in the future.

## Decision
Create `gax` CLI with subcommands, starting with `gax sheet pull/push`.

## Architecture

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│ Google Sheet│ ←──→ │   pandas    │ ←──→ │ Local File  │
│   (API)     │      │  DataFrame  │      │ (csv/tsv/…) │
└─────────────┘      └─────────────┘      └─────────────┘
                     (intermediate)
```

**Key design decisions:**

1. **Pandas DataFrame as intermediate representation**
   - Robust read/write for Google Sheets (via gspread)
   - Native support for CSV, TSV, JSON, etc.
   - Well-tested, handles edge cases (quoting, escaping, unicode)

2. **Pluggable format system**
   - Each format is a reader/writer pair
   - Easy to add: YAML, JSON, JSONL, XML in future

3. **Formatting preservation**
   - Only sync **data**, never formatting
   - No clear operations - overwrite cells in place
   - Google Sheets formatting (colors, borders, fonts) remains intact

## File Format

Extension: `*.sheet.gax`

```
---
spreadsheet_id: 16f107gJ4_hqkvhwIUXIwxS5-CaPspBmYQs6NE-lvsBg
tab: Actuals
format: csv
---
Date,Type,Category,Description,Amount,Status,Reference
2025-12-09,Revenue,Sponsor-Core,Dash0,10000,Invoiced,#237
```

### Frontmatter (YAML)

| Field | Required | Description |
|-------|----------|-------------|
| `spreadsheet_id` | Yes | Google Sheets document ID |
| `tab` | Yes | Sheet/tab name |
| `format` | Yes | Data format: `csv`, `tsv`, `psv`, `json`, `jsonl`, `markdown` |
| `url` | No | Full URL (informational) |
| `range` | No | Specific range, default `A:ZZ` |

### Supported Formats

| Format | Separator | Description |
|--------|-----------|-------------|
| `csv` | `,` | Comma-separated values |
| `tsv` | `\t` | Tab-separated values |
| `psv` | `\|` | Pipe-separated values |
| `json` | - | JSON array of objects |
| `jsonl` | - | JSON lines (one object per line) |
| `markdown` | `\|` | Markdown table format |

## Directory Structure

```
gax/
├── __init__.py
├── cli.py               # Click CLI
├── auth.py              # OAuth authentication
├── frontmatter.py       # YAML frontmatter parsing
├── gsheet/
│   ├── __init__.py
│   ├── client.py        # Google Sheets API wrapper
│   ├── pull.py
│   └── push.py
└── formats/
    ├── __init__.py
    ├── base.py          # Abstract reader/writer
    ├── csv.py           # CSV/TSV/PSV
    ├── json.py          # JSON/JSONL
    └── markdown.py      # Markdown tables
```

## Formatting Preservation

**Critical:** We only sync data, never formatting.

| Operation | Behavior |
|-----------|----------|
| Push | `worksheet.update()` - overwrites cell values only |
| Pull | `worksheet.get_all_values()` - reads values only |
| Clear | **Never** - no `worksheet.clear()` |

Google Sheets formatting (colors, fonts, borders, conditional formatting) lives in the sheet and is untouched.

## Future Extensions

### Additional Google Services
```bash
gax doc clone/pull      # Google Docs (implemented - see ADR 003)
gax drive pull/push     # Google Drive files
gax cal list/add        # Google Calendar
gax mail search/send    # Gmail
```
