# ADR 026: Clone Creates Files, Checkout Creates Directories

**Status:** Proposed
**Date:** 2026-04-15

Supersedes ADR 019 (Clone vs Checkout Pattern)
Supersedes ADR 002 (Multipart Markdown Format) for doc/sheet resources

## Context

ADR 019 defined clone as producing a multipart file (all tabs concatenated) and checkout as producing a directory of individual files. In practice, the multipart format has caused problems:

- `content-length` header needed when markdown contains `---`
- Poor editor experience for large multipart files
- Users rarely want "all tabs in one file" — they want one tab or all tabs individually
- The `explode` command exists only to undo the multipart bundling

The core insight: **clone should produce a single resource, checkout should produce a collection**.

## Decision

### Rule

| Command | Output | Scope |
|---------|--------|-------|
| `clone` | Single file | One resource (one tab, one thread, one event) |
| `checkout` | Directory | Collection (all tabs, all threads, all events) |

Clone never produces multipart files. Checkout always produces a directory.

### Doc Clone

`gax doc clone <url>` clones the **first tab only** as a single `.doc.gax` file.

If the document has multiple tabs, print a status message:

```
✓ Created: Project_Notes.doc.gax
  Tab "Overview" cloned (1 of 3 tabs).
  For all tabs: gax doc checkout <url>
```

If the document has a single tab, no extra message is needed.

### Doc Checkout

`gax doc checkout <url>` — no change from current behavior. Creates a directory with individual `.tab.gax` files and `.gax.yaml` metadata.

```
Project_Notes.doc.gax.d/
├── .gax.yaml
├── Overview.tab.gax
├── Design.tab.gax
└── Appendix.tab.gax
```

### Sheet Clone

`gax sheet clone <url>` clones the **first tab only** as a single `.sheet.gax` file (single-section, no multipart).

If the spreadsheet has multiple tabs, print a status message:

```
✓ Created: Budget_2026.sheet.gax
  Tab "Revenue" cloned (1 of 5 tabs).
  For all tabs: gax sheet checkout <url>
```

### Sheet Checkout

`gax sheet checkout <url>` — no change from current behavior. Creates a directory with individual `.tab.sheet.gax` files and `.gax.yaml` metadata.

### Top-Level Commands

`gax clone <url>` dispatches to the resource-specific clone (single file).
`gax checkout <url>` dispatches to the resource-specific checkout (directory).

### Other Resources

| Resource | clone | checkout |
|----------|-------|----------|
| doc | First tab → file | All tabs → directory |
| sheet | First tab → file | All tabs → directory |
| mail | Single thread → file | N/A (use `mailbox`) |
| mailbox | Label worksheet → file | `mailbox fetch` (existing) |
| cal | Events snapshot → file | Events → directory |
| form | Single form → file | N/A |
| contacts | Contacts → file | N/A |

Cal and mailbox are out of scope for this ADR — their existing behavior is unchanged.

### Explode Command

`gax explode` is removed. There are no multipart files to explode. Users who have existing multipart files can re-clone (single tab) or checkout (all tabs).

### Quiet Mode

`-q/--quiet` suppresses the multi-tab status message. Useful for scripting.

```bash
gax doc clone -q <url>   # No "1 of 3 tabs" message
```

### Tab Selection

To clone a specific tab, use the tab URL (e.g. `...#tab=t.xxx` for docs, `...#gid=123` for sheets). The URL identifies the tab — no extra flag needed.

## Implementation

### Doc Clone Changes (`gdoc.py:clone`)

1. Fetch tab list from Docs API (metadata only, via `get_doc_tabs()`)
2. Export and extract only the first tab's content
3. Write as single section via `format_section()`
4. If tab count > 1, print multi-tab status message with checkout hint

### Sheet Clone Changes (`cli.py:sheet_clone` / `gsheet/clone.py`)

1. Fetch spreadsheet metadata to get tab list and total count
2. Fetch content for only the first tab
3. Write as single-section file (no `format_multipart`)
4. If tab count > 1, print status message with checkout hint

### What Gets Removed

- `format_multipart()` usage in doc clone and sheet clone
- `explode` command (if implemented)
- Multipart parsing is kept for backward compatibility with existing `.gax` files and other resources (cal, mailbox) that still use it

## Migration

Existing multipart `.doc.gax` and `.sheet.gax` files continue to work with `pull` and `push`. No forced migration. Users can re-clone (getting a single-tab file) or checkout (getting a directory) at their convenience.

## Consequences

### Positive

1. **No more multipart files** for docs and sheets — simpler format
2. **Clear mental model** — clone=file, checkout=directory, always
3. **No explode** — one fewer command to learn
4. **Better editor experience** — single-tab files are small and focused

### Negative

1. **Clone gives less data** — only first tab, not all tabs
2. **Users must know about checkout** — the status message guides them

## Related ADRs

- **ADR 002**: Multipart Markdown Format (superseded for doc/sheet)
- **ADR 019**: Clone vs Checkout Pattern (superseded — clone no longer produces multipart files)
- **ADR 022**: Simplified CLI Model (deferred — `clone`/`checkout` remain; no `pull` from URLs)
- **ADR 025**: Directory-Only Collections (deferred — proposed eliminating clone entirely)
