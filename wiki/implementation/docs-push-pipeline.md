---
title: Docs Push Pipeline
description: How gax pushes edits to Google Docs — IR, patch vs bulk, UTF-16 indexing, tables
status: current
updated: 2026-07-26
sources:
  - gax/gdoc/ir.py
  - gax/gdoc/diff_push.py
  - gax/gdoc/doc.py
  - ADR/034-faithful-surgical-push.md
---

## Two push modes

### Bulk (full-replace, default)

1. Delete entire document body (`deleteContentRange` index 1 to end).
2. Insert new text from markdown-rendered IR.
3. Apply paragraph styles (headings) and inline formatting in reverse
   index order.

Destroys all non-markdown formatting: colors, fonts, comments,
suggestions. Fast and reliable; appropriate for early drafts.

### Patch (`--patch` flag, experimental)

1. Pull remote doc, parse to blocks with `doc_range` populated.
2. Parse local markdown to blocks (no `doc_range`).
3. Diff block lists via `ast_diff` (SequenceMatcher on block keys).
4. Translate edit ops to Docs API mutations (`diff_to_mutations`).
5. Apply via `batchUpdate`.

Preserves formatting on untouched blocks. Falls back to bulk on
structural changes (table shape, unsupported edits).

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

ADR 034/035 address this: a pull-time baseline in the CAS store
preserves the full document JSON so push can be surgical even though
the markdown is lossy.

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

| Feature | Survives bulk push | Survives patch push |
|---|---|---|
| Bold, italic, strikethrough, links | yes | yes |
| Heading levels | yes | yes |
| List type (ordered/unordered) | yes | yes |
| List nesting | no (flattened) | no (flattened) |
| Table structure | yes | yes (shape must match) |
| Colors, fonts, alignment | no | yes (untouched blocks) |
| Comments, suggestions | no | yes (untouched blocks) |
| Images | partial (re-embedded) | yes (untouched blocks) |
