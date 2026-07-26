---
title: Architecture
description: Module structure, key abstractions, and how resource implementations plug in
status: current
updated: 2026-07-26
sources:
  - gax/resource.py
  - gax/gaxfile.py
  - gax/cli.py
  - gax/store.py
  - gax/syncstate.py
---

## Module layout

```
gax/
  __init__.py
  cli.py              # Top-level Click commands + resource CLI imports
  resource.py          # Resource base class and dispatch
  gaxfile.py           # .gax.md parsing/formatting (single + multipart)
  syncstate.py         # Sync timestamps and revision tracking
  store.py             # Content-addressable blob storage
  auth.py              # OAuth credential management
  ui.py                # Progress spinner, logging, confirmation dialogs
  docs.py              # Man page generation from Click metadata
  formats/             # Pluggable format adapters (csv, json, markdown)
  gdoc/                # Google Docs (Tab, Doc resources)
  gsheet/              # Google Sheets (SheetTab, Sheet resources)
  mail/                # Gmail (Thread, Draft, Mailbox resources)
  gcal/                # Calendar
  gtask/               # Google Tasks
  form/                # Google Forms
  contacts/            # Google Contacts
  gdrive/              # Google Drive files/folders
  gslides/             # Google Slides
  label/               # Gmail labels (declarative)
  filter/              # Gmail filters (declarative)
  search/              # Cross-resource search
```

## Key abstractions

### Resource (`resource.py`)

Base class for every resource type. Provides:

- **Dispatch**: `from_url(url)` and `from_file(path)` try each
  registered subclass and return the first match.
- **Standard operations**: `clone`, `checkout`, `pull`, `diff`, `get`,
  `push` (each raises `NotImplementedError` if unsupported).
- **Registration**: explicit via `Resource.register(cls)`.

See [CLI Model](../solution/cli-model.md) for dispatch details.

### GaxFile (`gaxfile.py`)

Parses and formats the `.gax.md` file format. Two variants:
single-section and multipart. See
[File Conventions](../solution/file-conventions.md).

### SyncState (`syncstate.py`)

Tracks `time` (last sync UTC) and `rev` (opaque revision token) per
file. Used by UI to warn on stale data (>1 hour) and by push to
detect remote drift via `rev_guard`.

### Store (`store.py`)

Content-addressable blob storage under `~/.gax/store/`:

- `blob/` — SHA-256 named files (deduplicated)
- `meta/` — JSON metadata per blob
- `ref/` — Named symlinks to blobs

Currently used for Gmail attachments. ADR 034 extends it to store
pull-time baselines for Docs (raw JSON + revisionId).

## Resource implementation pattern

Each resource module follows a consistent structure (reference:
`gax/mail/draft.py`):

- **`cli.py`** — Click commands (thin: arg parsing, prompts, output
  formatting). No business logic.
- **Core module** (e.g. `doc.py`, `draft.py`) — Resource subclass with
  all business logic. No Click, no `sys.exit()`.
- **Helpers** — shared utilities within the module (e.g.
  `mail/shared.py`).

Communication: `logging.info()` for status (spinner picks up),
`ValueError` for user-fixable errors, return values for results.

## How a new resource is added

1. Create `gax/newresource/` with `cli.py` and a core module.
2. Subclass `Resource`, set `URL_PATTERN`, `FILE_TYPE`,
   `FILE_EXTENSIONS`, `CHECKOUT_TYPE`.
3. Implement the relevant operations (`clone`, `pull`, etc.).
4. Import the CLI group in `gax/cli.py` — this triggers registration
   and makes dispatch work.

## Notable subsystems

### Docs IR (`gdoc/ir.py`)

Block/span tree that bridges markdown and Google Docs API. Blocks:
Heading, Paragraph, ListItem, CodeBlock, Table. Spans carry text with
bold/italic/strikethrough/url. Used for both pull (Docs JSON to
markdown) and push (markdown to API `batchUpdate` requests).

### Patch-based push (`gdoc/diff_push.py`)

Computes deltas using difflib, builds Docs API `batchUpdate` requests
for changed blocks only. Falls back to full-replace when structural
changes are detected. See [ADR Map](adr-map.md) for the accepted ADRs
(034, 035) that extend this with baselines and a faithful Tree IR.
