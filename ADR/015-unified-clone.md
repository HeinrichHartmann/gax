# ADR 015: Unified Clone Command

## Status

Proposed

## Context

Currently, cloning a Google resource requires knowing which subcommand to use:

```bash
gax doc clone https://docs.google.com/document/d/...
gax sheet clone https://docs.google.com/spreadsheets/d/...
gax form clone https://docs.google.com/forms/d/...
gax mail thread clone https://mail.google.com/...
gax cal event clone https://calendar.google.com/...
```

Users must identify the resource type and use the correct command. A unified `gax clone <url>`
command could infer the appropriate handler from the URL pattern.

This extends ADR 012 (Unified Pull) to cover initial cloning from URLs.

## Current Clone Commands

### URL-Based (Single Resource)

These commands take a URL and clone a single resource:

| Command | URL Domain | Example |
|---------|------------|---------|
| `gax doc clone <url>` | `docs.google.com/document/d/{id}` | Clone Google Doc |
| `gax sheet clone <url>` | `docs.google.com/spreadsheets/d/{id}` | Clone spreadsheet (all tabs) |
| `gax form clone <url>` | `docs.google.com/forms/d/{id}` | Clone form definition |
| `gax mail thread clone <url>` | `mail.google.com/...#{threadId}` | Clone email thread |
| `gax mail draft clone <url>` | `mail.google.com/...#drafts/{draftId}` | Clone email draft |
| `gax cal event clone <url>` | `calendar.google.com/.../event/{eventId}` | Clone calendar event |

### Substructure Commands

These clone parts of a larger resource:

| Command | Description |
|---------|-------------|
| `gax doc tab clone <url> [tab]` | Clone single tab from multi-tab doc |
| `gax tab clone <url> [tab]` | Clone single tab from spreadsheet |

### State/Config Commands (No URL)

These clone account-level state, not URL-addressable resources:

| Command | Description |
|---------|-------------|
| `gax mail filter clone [file]` | Clone all Gmail filters |
| `gax mail label clone [file]` | Clone all Gmail labels |
| `gax mail list clone <file> -q <query>` | Clone threads matching query (for bulk labeling) |
| `gax cal list clone [file] -d <days>` | Clone calendar events in date range |

## Decision

### Unified Clone for URL-Based Resources

The `gax clone <url>` command uses simple regex pattern matching to dispatch to the appropriate handler:

### Command Interface

```bash
# Google Workspace
gax clone https://docs.google.com/document/d/abc123
gax clone https://docs.google.com/spreadsheets/d/xyz789
gax clone https://docs.google.com/forms/d/form123

# Gmail
gax clone "https://mail.google.com/mail/u/0/#inbox/18f5a3b2c1d0e9f8"
gax clone "https://mail.google.com/mail/u/0/#drafts/r-123456789"

# Calendar
gax clone "https://calendar.google.com/calendar/event?eid=abc123"

# With options
gax clone --format yaml https://docs.google.com/forms/d/form123
gax clone -o myfile.gax https://docs.google.com/document/d/abc123
```

### Not Covered by Unified Clone

These require explicit commands (no single-URL semantics):

```bash
# Substructures (need tab name or gid)
gax tab clone https://docs.google.com/spreadsheets/d/xyz789 "Sheet1"
gax doc tab clone https://docs.google.com/document/d/abc123 "Chapter 1"

# Account state (not URL-addressable)
gax mail filter clone
gax mail label clone
gax mail list clone inbox.gax -q "in:inbox"
gax cal list clone week.gax -d 7
```

### Implementation

```python
import re

@main.command()
@click.argument("url")
@click.option("-o", "--output", type=click.Path(path_type=Path))
@click.option("--format", type=click.Choice(["md", "yaml"]), default="md")
@click.pass_context
def clone(ctx, url: str, output: Path | None, format: str):
    """Clone a Google resource from URL."""

    # Google Docs
    if re.search(r"docs\.google\.com/document/d/", url):
        ctx.invoke(doc_clone, url=url, output=output)

    # Google Sheets
    elif re.search(r"docs\.google\.com/spreadsheets/d/", url):
        ctx.invoke(sheet_clone, url=url, output=output)

    # Google Forms
    elif re.search(r"docs\.google\.com/forms/d/", url):
        ctx.invoke(form_clone, url=url, output=output, fmt=format)

    # Gmail drafts (must come before general mail pattern)
    elif re.search(r"mail\.google\.com/mail/[^#]*#drafts/", url):
        ctx.invoke(draft_clone, draft_id_or_url=url, output=output)

    # Gmail threads
    elif re.search(r"mail\.google\.com/mail/", url):
        ctx.invoke(mail_thread_clone, thread_id_or_url=url, output=output)

    # Calendar events
    elif re.search(r"calendar\.google\.com/calendar/", url):
        ctx.invoke(cal_event_clone, id_or_url=url, output_path=output)

    else:
        click.echo(f"Unrecognized URL: {url}", err=True)
        click.echo("Supported: Google Docs/Sheets/Forms, Gmail, Calendar", err=True)
        sys.exit(1)
```

## Consequences

### Positive

- **Single entry point**: `gax clone <url>` works for any URL-based resource
- **Copy-paste friendly**: Users can paste URLs directly from browser
- **Discoverable**: Unknown URLs get helpful error messages

### Negative

- **Substructures excluded**: Tab cloning still needs explicit command
- **State commands excluded**: Filters/labels/bulk operations need explicit commands

### Neutral

- **Consistent with pull**: Mirrors the unified pull approach from ADR 012

## Outlook

### Phase 1: Implement Clone Command

Add `gax clone` to `cli.py` with the pattern matching logic.

### Phase 2: Extend URL Patterns

As Google URLs evolve, add new patterns:
- Google Slides: `docs.google.com/presentation/d/{id}`
- Google Drive files: `drive.google.com/file/d/{id}`

### Phase 3: Unified Push

Implement `gax push <file>` that:
1. Detects file type from header
2. Shows preview of changes
3. Confirms before pushing
4. Dispatches to appropriate push/apply command

### Non-Goals

- **Tab-level inference**: `gax clone spreadsheet#gid=0` won't auto-detect tab.
  Use `gax tab clone` explicitly.

- **Query-based cloning**: `gax clone "in:inbox"` won't work.
  Use `gax mail list clone` for bulk operations.

- **URL shorteners**: Won't resolve bit.ly or goo.gl links.
