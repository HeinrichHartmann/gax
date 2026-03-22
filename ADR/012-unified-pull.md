# ADR 012: Unified Pull Command

## Status

Proposed

## Context

Currently, updating a .gax file requires knowing which subcommand to use:
- `gax doc pull file.doc.gax`
- `gax mail pull file.mail.gax`
- `gax sheet pull file.sheet.gax`
- `gax mail relabel pull file.gax`
- `gax cal pull file.cal.gax`

Users must remember the correct command for each file type. A unified `gax pull` command
would simplify the workflow by auto-detecting the file type from the YAML header.

## Decision

### Command

```
gax pull <file>       # Pull/update any .gax file
gax pull *.gax        # Pull multiple files
gax pull .            # Pull all .gax files in current directory
```

### File Type Detection

Each .gax file includes a `type` field in its YAML header that identifies the content type:

| Type | Maps To | Description |
|------|---------|-------------|
| `gax/doc` | `gax doc pull` | Google Doc |
| `gax/sheet` | `gax sheet pull` | Google Sheet (multipart) |
| `gax/sheet-tab` | `gax sheet tab pull` | Single sheet tab |
| `gax/mail` | `gax mail pull` | Email thread |
| `gax/draft` | `gax mail draft pull` | Email draft |
| `gax/relabel` | `gax mail relabel pull` | Relabel state |
| `gax/cal` | `gax cal pull` | Calendar events |
| `gax/labels` | `gax label pull` | Labels state |
| `gax/filters` | `gax filter pull` | Filters state |

### YAML Header Updates

Ensure all file types include a `type` field:

```yaml
---
type: gax/doc
title: My Document
source: https://docs.google.com/...
---
```

```yaml
---
type: gax/mail
title: Re: Meeting
thread_id: abc123
source: https://mail.google.com/...
---
```

```yaml
---
type: gax/relabel
pulled: 2026-03-22T17:30:00Z
query: in:inbox
limit: 50
---
```

### Implementation

```python
@main.command()
@click.argument("files", nargs=-1, type=click.Path(exists=True))
def pull(files):
    """Pull/update .gax file(s) from their sources."""
    for file in files:
        header = parse_yaml_header(file)
        file_type = header.get("type")

        if file_type == "gax/doc":
            doc_pull(file)
        elif file_type == "gax/mail":
            mail_pull(file)
        elif file_type == "gax/relabel":
            relabel_pull(file)
        # ... etc
        else:
            # Fallback: detect by extension
            if file.endswith(".doc.gax"):
                doc_pull(file)
            elif file.endswith(".mail.gax"):
                mail_pull(file)
            # ...
```

### Fallback Detection

For backwards compatibility with files lacking `type` field:

1. Check `type` field in YAML header (preferred)
2. Check file extension (`.doc.gax`, `.mail.gax`, etc.)
3. Infer from header fields (`thread_id` → mail, `spreadsheet_id` → sheet)

### Batch Operations

```bash
# Pull all .gax files in directory
gax pull .

# Pull specific pattern
gax pull inbox/*.mail.gax

# Pull with verbose output
gax pull -v *.gax
```

### Error Handling

- Unknown type: error with suggestion to specify subcommand
- Missing source/ID: error explaining what's missing
- API errors: report per-file, continue with others

### Output

```
$ gax pull inbox.gax notes.doc.gax
Pulling inbox.gax (gax/relabel)... 15 threads
Pulling notes.doc.gax (gax/doc)... 3 tabs
Done: 2 files updated
```

## Consequences

- **Simpler UX**: One command for all pull operations
- **Discoverable**: Users don't need to remember subcommands
- **Consistent**: Same pattern works for all file types
- **Backwards compatible**: Falls back to extension detection
- **Batch friendly**: Supports globs and directories

## Migration

Existing files without `type` field continue to work via extension/header inference.
New files created by `clone` commands will include `type` field.

## Future Considerations

- `gax push <file>` - unified push command
- `gax sync <file>` - bidirectional sync
- `gax status <file>` - show sync status without updating
