# ADR 025: Directory-Only Collections — Drop Multipart File Format

## Status

**PROPOSED**

Supersedes ADR 002 (Multipart YAML-Markdown Format)
Supersedes ADR 019 (Clone vs Checkout Pattern)
Extends ADR 022 (Simplified CLI Model)
Extends ADR 024 (File Extension Convention)

## Context

ADR 002 defined a multipart file format where multiple tabs or items are concatenated into a single `.gax.md` file separated by `---` YAML headers. ADR 019 built on this with a clone (multipart file) vs checkout (directory) two-mode model.

The multipart format has accumulated significant complexity with no proportionate benefit:

1. **`content-length` header** — required when markdown body contains `---` horizontal rules; editors and standard markdown tools break without it; users must not manually add `---` in their files
2. **Two-mode confusion** — users must choose between `clone` (multipart) and `checkout` (directory) before they know which they need; the distinction is not obvious
3. **`explode` command** — exists only to convert between the two modes; a conversion step that exists only because two modes exist is a symptom of accidental complexity
4. **Split/join operations** — additional commands needed to work around multipart limitations
5. **Poor editor experience** — a 2000-line multipart sheet file is harder to navigate than a directory of 5 tab files; jump-to-definition, file search, and outline views all work better with separate files
6. **Nested tabs** — some resources (docs with nested tabs) produce nested structures; directories map naturally to nesting, multipart files do not

The single benefit of multipart — "one file to copy or email" — is not a primary use case for a sync tool. Users who want a snapshot can use standard tools (`cat`, `zip`) after the fact.

## Decision

### Remove Multipart Support

Drop the multipart file format entirely. Multi-item resources are **always** represented as directories.

- No multipart files
- No `clone` command (replaced by `pull` per ADR 022)
- No `explode` command
- No `content-length` header
- No split/join operations

### Directory Layout for Multi-Item Resources

A resource with multiple items (tabs, events, threads) is represented as a directory named `<name>.<type>.gax.md.d/`:

```
budget.sheet.gax.md.d/
├── .gax.yaml              ← collection metadata (source URL, pull timestamp)
├── revenue.tab.gax.md
├── expenses.tab.gax.md
└── summary.tab.gax.md
```

Each item file is a standard single-section YAML-frontmatter + markdown file. No special parsing is needed.

### Nested Tabs (Docs with Tab Hierarchy)

Google Docs supports nested tab trees. These map directly to nested subdirectories:

```
project-doc.doc.gax.md.d/
├── .gax.yaml
├── overview.tab.gax.md
├── design.tab.gax.md.d/        ← tab with children
│   ├── .gax.yaml
│   ├── architecture.tab.gax.md
│   └── api.tab.gax.md
└── appendix.tab.gax.md
```

A tab that has children becomes a directory. A tab that has no children is a plain file. This mirrors how filesystems naturally represent hierarchies.

### The `.gax.yaml` Metadata File

Each directory contains a `.gax.yaml` file with collection-level metadata:

```yaml
type: gax/sheet
title: Budget 2026
source: https://docs.google.com/spreadsheets/d/abc123
pulled: 2026-04-14T10:00:00Z
tabs: [revenue, expenses, summary]
```

This file:
- Is never edited by users (managed by `gax`)
- Provides the `source:` URL for push operations on the whole collection
- Records the pull timestamp and tab order

### Single-Tab Resources

Resources that always have exactly one item (contacts, labels, filters, mail threads) remain plain files — no directory is created:

```
contacts.gax.md
inbox.filter.gax.md
thread-abc123.mail.gax.md
```

### Updated `pull` Behavior

`gax pull <url>` on a multi-item resource creates the directory:

```bash
gax pull https://docs.google.com/spreadsheets/d/abc123
# → budget.sheet.gax.md.d/
#   ├── .gax.yaml
#   ├── revenue.tab.gax.md
#   ├── expenses.tab.gax.md
#   └── summary.tab.gax.md

gax pull budget.sheet.gax.md.d/
# → refreshes all tabs, shows diff per tab, confirms
```

`gax pull <dir>/revenue.tab.gax.md` refreshes a single tab.

### Updated `push` Behavior

```bash
gax push budget.sheet.gax.md.d/
# → diffs and pushes all tabs

gax push budget.sheet.gax.md.d/revenue.tab.gax.md
# → diffs and pushes one tab
```

### Updated `diff` Behavior

```bash
gax diff budget.sheet.gax.md.d/
# → shows diff for all tabs

gax diff budget.sheet.gax.md.d/revenue.tab.gax.md
# → shows diff for one tab
```

### Glob Patterns

```bash
gax diff **/*.gax.md          # All individual item files
gax push -y **/*.tab.gax.md   # All tab files across all collections
gax pull -y *.sheet.gax.md.d/ # Refresh all sheet collections
```

## Updated Operation Matrix

| Resource | Single file or directory | push target |
|----------|--------------------------|-------------|
| doc (single tab) | `name.doc.gax.md` | file |
| doc (multi-tab) | `name.doc.gax.md.d/` | dir or individual tab file |
| sheet | `name.sheet.gax.md.d/` | dir or individual tab file |
| mail thread | `name.mail.gax.md` | file |
| mailbox | `name.mailbox.gax.md.d/` | dir or individual thread file |
| contacts | `contacts.gax.md` | file |
| form | `name.form.gax.md` | file |
| calendar | `name.cal.gax.md.d/` | dir or individual event file |
| label | `name.label.gax.md` | file |
| filter | `name.filter.gax.md` | file |

## Single-Tab to Multi-Tab Lifecycle

A Google Doc starts with one tab and renders as a plain file:

```
project.doc.gax.md
```

If tabs are later added in Google Docs and the user runs `pull`, `gax` detects the remote now has multiple tabs and creates a directory, removing the old file:

```bash
gax pull project.doc.gax.md
# Remote now has 3 tabs
# → Removes project.doc.gax.md
# → Creates project.doc.gax.md.d/
#     ├── .gax.yaml
#     ├── overview.tab.gax.md
#     ├── design.tab.gax.md
#     └── appendix.tab.gax.md
```

`gax` prints a notice when this transition happens:

```
Notice: 'project.doc.gax.md' → 'project.doc.gax.md.d/' (document now has multiple tabs)
```

The reverse transition (tabs collapsed to one) works the same way: a directory is replaced by a plain file on the next `pull`.

## Consequences

### Positive

1. **One format** — every file is a standard YAML-frontmatter + markdown file; no multipart parser needed
2. **No `content-length`** — markdown `---` horizontal rules work normally in any file
3. **No mode choice** — `pull` always produces the right structure; no clone-vs-checkout decision
4. **Removed commands** — `clone`, `explode`, `split`, `join` are gone
5. **Natural nesting** — nested tabs map to nested directories without any format gymnastics
6. **Editor-friendly** — file search, outline, and navigation work on individual files
7. **Incremental push** — push one tab at a time without touching the others

### Negative

1. **More files on disk** — a 20-tab sheet becomes 20 files + a metadata file
2. **No single-file snapshot** — users who want one file must `cat` or `zip` themselves
3. **Breaking change** — existing multipart `.gax` files must be migrated

## Migration

### Existing Multipart Files

Multipart files are not supported. Re-pull from the remote source to get the directory layout.

## Alternatives Considered

### Alternative 1: Keep Both Modes (Multipart + Directory)

Keep ADR 019's clone/checkout duality with optional explode.

**Rejected because:** This ADR exists precisely to eliminate that duality. Two modes for the same thing means two code paths, two sets of docs, and a user decision that shouldn't exist.

### Alternative 2: Single File with Named Sections (Not YAML Concatenation)

Use a different delimiter (e.g. `<!-- tab: Revenue -->`) to embed multiple tabs in one file without the content-length problem.

**Rejected because:** Custom delimiters are not standard markdown; editors still won't understand the file structure; nested tabs still don't map cleanly; per-tab operations still require parsing the whole file.

### Alternative 3: Zip/Archive Format

Bundle tabs as files inside a `.gax.zip` or similar container.

**Rejected because:** Not human-readable or directly editable; requires unpacking to use; loses all the editor tooling benefits.

## Related ADRs

- **ADR 002**: Multipart YAML-Markdown Format (superseded)
- **ADR 019**: Clone vs Checkout Pattern (superseded)
- **ADR 022**: Simplified CLI Model (pull/push/diff/new — extended here)
- **ADR 024**: File Extension Convention (`.gax.md` — extended here)
