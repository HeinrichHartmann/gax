---
title: Docs Push Pipeline
description: How gax pushes edits to Google Docs — plan-driven surgical push, revision guard, run splicing, full-replace fallback
status: current
updated: 2026-07-26
sources:
  - gax/gdoc/ir.py
  - gax/gdoc/diff_push.py
  - gax/gdoc/doc.py
  - gax/gdoc/cli.py
  - ADR/034-faithful-surgical-push.md
  - ADR/037-single-editor-sync.md
---

## Sync model (ADR 037)

gax uses a single-editor model: one loop, two guards.

```
pull  →  edit  →  push
```

- **Push guard**: refuses when the remote revision differs from the
  stored one (`Remote changed (rev abc → def). Pull first.`).
- **Pull guard**: refuses when you have unpushed local edits (local
  content differs from render of stored baseline). Use `--force` to
  discard.

Nothing merges. Nothing rebases. Under the guard the remote IS the
state you pulled, so diffing remote vs local directly gives your edits
with correct indices — no baseline load on push, no alignment, no
drift logic. One code path.

## Two push modes

### Surgical patch (default)

Plan-driven diff that preserves non-markdown formatting:

1. Fetch remote tab, parse to IR blocks with `doc_range` populated.
2. Parse local markdown to IR blocks (no `doc_range`).
3. Diff block lists via `SequenceMatcher` on block keys → `EditOp`s.
4. Translate ops to Docs API `batchUpdate` mutations using remote
   `doc_range` indices (run-level splicing).
5. Apply via `batchUpdate`; refresh baseline + revision stamp.

Falls back to full-replace (with user confirmation) when the patch
cannot be computed (e.g. structural changes in tables).

### Full-replace (`--force-replace`)

1. Delete entire document body (`deleteContentRange` index 1 to end).
2. Insert new text from markdown-rendered IR.
3. Apply paragraph styles (headings) and inline formatting in reverse
   index order.

Destroys all non-markdown formatting: colors, fonts, comments,
suggestions. Fast and reliable; appropriate for early drafts or when
surgical patch fails. The old `--patch`/`--bulk` flags are deprecated;
use `--force-replace` for the destructive path.

## The IR (Intermediate Representation)

`gax/gdoc/ir.py` defines five block types and one span type:

| Type | Fields | What it captures |
|---|---|---|
| Heading | level (1-6), spans | Named styles HEADING_1 through HEADING_6 |
| Paragraph | spans | Normal text blocks |
| ListItem | spans, ordered, depth | Bullets and numbered lists with nesting |
| CodeBlock | code, language | Fenced code (no native Docs equivalent) |
| Table | rows (3D: row/col/spans) | Table structure and cell content |
| Span | text, bold, italic, strikethrough, url | Inline formatting |

### What the IR drops

Colors, fonts, sizes, alignment, indentation, spacing, underline,
footnotes, comments, suggestions, images (extracted to blob store as
`file://` URLs), block quotes (converted to plain paragraphs),
horizontal rules (skipped), custom styles.

ADR 034/035/037 address this: a pull-time baseline in the CAS store
preserves the full document JSON. Under the single-editor model
(ADR 037), the revision guard ensures remote hasn't moved, so the
remote itself provides correct indices for surgical push. The baseline
is now only used by the pull guard (detecting unpushed local edits).

## UTF-16 index arithmetic

Google Docs API indices are in UTF-16 code units. Emoji and characters
above U+FFFF occupy 2 code units but count as 1 in Python's `len()`.
All index calculations use `_utf16_len()`:

```python
def _utf16_len(s: str) -> int:
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)
```

Getting this wrong produces corrupted documents. Every call site in
`ir.py` and `diff_push.py` must use this function, never bare `len()`.

## Table handling (two-pass insertion)

Tables cannot be fully created in a single `batchUpdate`:

1. **Pass 1**: `insertTable(rows, columns)` creates an empty table.
   Cell indices are auto-generated and unknown until the document is
   re-read.
2. **Pass 2**: `documents().get()` re-reads the doc to discover cell
   indices, then `insertText` + `updateTextStyle` populate each cell.

In patch mode, `_update_table_requests` reads cell indices from
`_raw_table` (the raw Docs JSON stored on pull). Only single-paragraph
cells are supported; multi-paragraph cells or shape changes
(adding/removing rows or columns) raise `ValueError`.

## Code blocks

Google Docs has no native code block element. gax converts code blocks
to lines prefixed with `"> "` (quote-style indentation). This is a
known lossy workaround; `check_unsupported()` emits a `PushWarning`.

## Nested lists

List nesting depth is captured on pull but **flattened to depth 0 on
push**. The Docs API does not expose nesting level in
`createParagraphBullets`. `check_unsupported()` warns about this.

## Mutation ordering

All mutations are sorted by **descending `startIndex`**. Processing
from the end backward ensures earlier indices remain valid after each
mutation. This invariant is critical — violating it corrupts the
document.

## Paragraph trailing newline

Google Docs includes a paragraph-terminating `\n` in every paragraph's
last textRun. `_spans_from_textruns` strips it on pull.
`_update_paragraph_requests` subtracts 1 from `endIndex` to preserve
it on patch updates. Document body starts at index 1, not 0.

## Formatting round-trip summary

| Feature | Survives force-replace | Survives surgical patch (default) |
|---|---|---|
| Bold, italic, strikethrough, links | yes | yes |
| Heading levels | yes | yes |
| List type (ordered/unordered) | yes | yes |
| List nesting | no (flattened) | no (flattened) |
| Table structure | yes | yes (shape must match) |
| Colors, fonts, alignment | no | yes (untouched blocks) |
| Comments, suggestions | no | yes (untouched blocks) |
| Images | partial (re-embedded) | yes (untouched blocks) |
