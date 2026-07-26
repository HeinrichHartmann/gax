---
title: File Conventions
description: The .gax.md file format, extensions, and checkout folder structure
status: current
updated: 2026-07-26
sources:
  - gax/gaxfile.py
  - gax/docs.py
  - README.md
---

## The .gax.md format

Every gax file is self-describing: a YAML header carries the resource
type and source URL, followed by plain-text content. This lets
`gax pull <file>` update any file without additional arguments.

### Single-section format

Used by most resources (sheets, contacts, labels, filters, forms,
mailbox):

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
```

### Multipart format

Used by mail threads, drafts, and docs. Multiple sections separated
by `---` delimiters, each with its own headers:

```
---
type: gax/mail
thread_id: abc123
---
first message body
---
type: gax/mail
from: alice@example.com
---
second message body
```

When a section body contains literal `---` lines, gax adds a
`content-length` header for byte-accurate parsing (avoids misparse).

### Header parsing

Headers are parsed as simple `key: value` pairs (not full
`yaml.safe_load`) to preserve round-trip fidelity. Timestamps and
other values stay as strings, avoiding coercion.

## File extensions

| Extension | Resource type |
|---|---|
| `.sheet.gax.md` | Spreadsheet data |
| `.doc.gax.md` | Document |
| `.tab.gax.md` | Single document tab |
| `.mail.gax.md` | Email thread |
| `.draft.gax.md` | Email draft |
| `.cal.gax.md` | Calendar event |
| `.form.gax.md` | Google Form definition |
| `.gax.md` | Mail list (TSV with YAML header) |
| `.label.mail.gax.md` | Gmail labels state |
| `.filter.mail.gax.md` | Gmail filters state |

The extension tells gax the resource type; the YAML `type:` header is
the authoritative source when both are present.

## Checkout folder structure

`checkout` creates a `.gax.md.d/` directory:

```
Budget.sheet.gax.md.d/
  .gax.yaml                  # Folder metadata (type, url)
  Revenue.tab.sheet.gax.md   # Individual tab
  Expenses.tab.sheet.gax.md  # Individual tab
```

The `.gax.yaml` file contains `type:` and `url:` for dispatch. The
unified `pull`/`push` commands detect checkout folders and operate on
all files within.

## Sync metadata

Each file tracks sync state in its headers:

```yaml
sync:
  time: 2026-04-15T10:00:00Z
  rev: ALm37BW...
```

`time` records when the file was last synced. `rev` is an opaque
revision token (etag, historyId, revisionId) used to detect remote
changes since the last sync. gax warns when sync data is older than
one hour.
