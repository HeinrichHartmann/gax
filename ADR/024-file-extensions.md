# ADR 024: File Extension Convention — `.gax.md`

## Status

**PROPOSED**

Extends ADR 022 (Simplified CLI Model)

## Context

ADR 022 specifies file names like `project-notes.doc.gax`, `budget.sheet.gax`, etc. These files are plain text with YAML frontmatter and a markdown body, but the `.gax` suffix means:

- Editors don't apply markdown syntax highlighting
- Language servers don't offer markdown completion or preview
- GitHub and other viewers don't render the content
- Tools like `pandoc`, `markdownlint`, and linters ignore the files

The `.gax` extension was chosen to signal "this is a gax file, not a raw markdown file." However, the content **is** markdown (with a YAML frontmatter header), and treating it as markdown in editors is strictly better: no information is lost, and tooling improves.

## Decision

### New Extension Convention

Flip the extension order: use `.gax.md` instead of `.doc.gax`.

The resource type moves into the filename stem rather than the extension:

| Resource | Old name | New name |
|----------|----------|----------|
| Google Doc | `project-notes.doc.gax` | `project-notes.doc.gax.md` |
| Sheet | `budget.sheet.gax` | `budget.sheet.gax.md` |
| Form | `survey.form.gax` | `survey.form.gax.md` |
| Doc tab | `section.tab.gax` | `section.tab.gax.md` |
| Mail thread | `thread.mail.gax` | `thread.mail.gax.md` |
| Mailbox | `inbox.mailbox.gax` | `inbox.mailbox.gax.md` |
| Calendar | `work.cal.gax` | `work.cal.gax.md` |
| Contacts | `contacts.gax` | `contacts.gax.md` |
| Label | `label.gax` | `label.gax.md` |
| Filter | `filter.gax` | `filter.gax.md` |

The resource type (`.doc`, `.sheet`, etc.) remains embedded in the stem so `gax` can detect the type from the filename as a fallback (in addition to reading the `type:` frontmatter field).

### Why `.gax.md` and not `.md`

A plain `.md` extension would lose the type signal entirely. `.gax.md` means:

- Editors see `.md` → syntax highlighting, preview, linting
- `gax` sees `.gax.md` → recognizes the file as a gax-managed resource (not a random markdown file)
- Glob patterns like `*.gax.md` or `**/*.doc.gax.md` remain unambiguous

### Checkout Directories

Checkout directories follow the same pattern:

| Old | New |
|-----|-----|
| `budget.sheet.gax.d/` | `budget.sheet.gax.md.d/` |

### Type Detection Logic

`gax` determines resource type from (in order of priority):

1. `type:` field in YAML frontmatter (authoritative)
2. Stem suffix: `*.doc.gax.md`, `*.sheet.gax.md`, etc. (fallback when header is missing or partial)

This means a file named `notes.doc.gax.md` without a frontmatter `type:` field is still treated as a document.

## Batch Operations

Glob patterns continue to work:

```bash
gax diff *.gax.md          # All gax files
gax push *.doc.gax.md      # All documents
gax pull -y *.sheet.gax.md # All sheets
```

## Migration

### Renaming Existing Files

```bash
# Rename all existing gax files
for f in **/*.gax; do mv "$f" "${f}.md"; done
```

`gax` will provide a `gax migrate` subcommand (or a `--rename` flag on `gax pull`) that renames files in a working directory and rewrites any `source:` references as needed.

### Backward Compatibility

`gax` will continue to read `.gax` files (without the `.md` suffix) and emit a deprecation warning:

```
Warning: 'project-notes.doc.gax' uses the old extension. Rename to 'project-notes.doc.gax.md'.
```

Old-extension support is removed in the next major version.

## Consequences

### Positive

1. **Editor tooling** — syntax highlighting, markdown preview, and linters work out of the box
2. **GitHub rendering** — `.gax.md` files render as markdown in the web UI
3. **Discoverability** — `*.gax.md` is still an unambiguous glob; plain `*.md` finds gax files too
4. **No content change** — file format (YAML frontmatter + markdown body) is unchanged

### Negative

1. **Breaking rename** — existing `.gax` files must be renamed; scripts using old names break
2. **Slightly longer names** — `project-notes.doc.gax.md` is longer than `project-notes.doc.gax`
3. **Double extension** — `.gax.md` looks unusual; some tools parse only the last extension

## Alternatives Considered

### Alternative 1: Keep `.gax`, add editor config

Add a `.editorconfig` or VS Code `files.associations` setting to treat `*.gax` as markdown.

**Rejected because:** Per-editor configuration is per-repo boilerplate. GitHub rendering, `markdownlint`, `pandoc`, and other tools outside the editor still don't recognize `.gax`.

### Alternative 2: Use plain `.md` with no type in stem

Files named `project-notes.md` with type derived solely from frontmatter.

**Rejected because:** Loses the ability to detect type from filename; `*.doc.gax.md` globs become impossible; `gax` can't distinguish its managed files from other markdown files in the same directory.

### Alternative 3: Use `.doc.md`, `.sheet.md` (no `.gax` in extension)

`project-notes.doc.md`, `budget.sheet.md`.

**Rejected because:** Loses the "this file is gax-managed" signal. A file named `notes.doc.md` looks like a documentation file, not a gax sync file. The `.gax` in the extension communicates ownership.

## Related ADRs

- **ADR 022**: Simplified CLI Model — defines file naming conventions superseded here
- **ADR 002**: Multipart Markdown Format — file format (frontmatter + body) is unchanged
