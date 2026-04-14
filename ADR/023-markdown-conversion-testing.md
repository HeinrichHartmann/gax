# ADR 023: Markdown-to-Google-Docs Conversion and Testing Strategy

## Status

Proposed

## Context

gax syncs Google Docs as local markdown files. The **pull** direction (Google Docs to markdown) uses Google's native Drive API export (`files().export(mimeType="text/markdown")`). The **push** direction (markdown to Google Docs) uses a custom pipeline (`md2docs.py`) that parses markdown into an AST and generates Docs API `batchUpdate` requests.

Push was added incrementally (ADR 003 deferred it to v2). It now supports headings, bold/italic, unordered lists, tables, and code blocks. However, comparison experiments revealed several fidelity issues:

- **Ordered lists** are inserted as plain `1. ` text prefixes (not Google Docs numbered lists)
- **Inline code** (backticks) is not parsed
- **Code blocks** render as unstyled paragraphs
- **Paragraph spacing** around lists is lost
- **Table cell formatting** requires 3 API round-trips and is fragile

These issues prompted an investigation into alternative push approaches and a need for systematic testing.

## Alternatives Considered

### Alternative 1: Native Drive API Markdown Upload

Google's Drive API accepts `text/markdown` as input when creating documents (`files().create(mimetype="text/markdown")`). We confirmed that `files().update()` also works for replacing existing document content. This produces high-fidelity results: ordered lists, inline code, code blocks, and paragraph spacing all render correctly.

**For single-tab documents**, this is ideal: one API call, perfect fidelity, zero custom code.

**For per-tab updates** (our primary use case), this doesn't work: the Drive API operates on whole documents, not individual tabs. We investigated workarounds:

- **Create temp doc, copy content to target tab**: There is no copy/paste API between documents. We'd need to serialize and replay the content.
- **Concatenate all tabs, replace whole doc**: Risks clobbering tab structure. Untested whether `files().update()` preserves tabs at all.

**Rejected because**: creating temporary documents for every push is hacky (requires broader Drive permissions, creates transient clutter, race conditions with concurrent pushes). The tab limitation makes it unsuitable as a general solution.

### Alternative 2: JSON Replay (Create Temp Doc, Read Structure, Replay via batchUpdate)

1. Upload markdown to a temp doc via Drive API (native, high fidelity)
2. Read the document's body JSON via `documents().get()`
3. Replay the structural elements into the target tab using `batchUpdate`
4. Delete the temp doc

We prototyped this (`experiments/replay_json.py`). Results were near-identical to native upload. The approach:

- Inserts paragraph text in one batch, skipping table positions
- Applies paragraph styles (headings), text styles (bold/italic), and bullets using source document indices
- Inserts tables separately, populates cells with text and formatting
- Cleans up spurious empty paragraphs created by `insertTable`

**Quality**: Matched native upload exactly. All formatting preserved.

**Rejected because**: The table handling is equally complex as in `md2docs.py` (same insertTable/re-read/populate/re-read/format cycle). Total code is ~470 lines vs ~340 for md2docs. The approach replaces "parse markdown" (well-solved, many libraries) with "create temp doc + read JSON + delete" (hacky, more permissions, more API calls, transient Drive objects). The complexity budget is spent in the same place (Docs API table operations), just with a different input source.

### Alternative 3: Fix md2docs.py (Chosen)

Keep the current architecture: parse markdown locally, generate `batchUpdate` requests directly. Fix the specific known bugs:

- Use `createParagraphBullets` for ordered lists (investigate and fix the "bleeding" issue)
- Add inline code parsing and rendering (monospace font via `updateTextStyle`)
- Add code block styling
- Fix `_unescape_md` inconsistencies

**Chosen because**: The markdown parser is the easy part (and can be replaced with a standard library like `mistune` if needed). The hard part (Docs API index arithmetic, table operations) is the same in all approaches. Fixing specific bugs in working code is lower risk than replacing the architecture.

## Decision

### Conversion Architecture

Keep `md2docs.py` as the push pipeline. Fix known bugs incrementally, guided by a test suite structured around **round-trip stability**.

### Testing Strategy: Identity Round-Trip + Push Verification

The mathematical property we enforce:

- Let `push: Markdown -> GoogleDoc` and `pull: GoogleDoc -> Markdown`
- For the supported markdown subset (the "canonical form"), `pull . push` is the **identity**: `M == M1`

```
M  -push->  D  -pull->  M1

Assert: M == M1  (identity on canonical form)
```

The fixture (`tests/fixtures/e2e_rich_formatting.md`) is written in canonical form and contains only supported features. If identity fails, the diff shows exactly what was lost.

### Test Levels

#### Level 1: Push Verification (API structure inspection)

After pushing the fixture, read the Google Doc back via the Docs API and assert that styling was applied correctly:

- Headings have correct `namedStyleType` (HEADING_1 through HEADING_6)
- Bold/italic spans have correct `textStyle` properties
- Hyperlinks have correct `textStyle.link.url`
- List items have `bullet` property set
- Tables have correct dimensions and cell content/formatting

This catches bugs where the markdown round-trips correctly (because pull-side normalization compensates) but the Google Doc itself is wrong.

Implementation: `TestPushVerify` in `test_roundtrip.py` — one API read, ~15 assertions.

#### Level 2: Identity Round-Trip (e2e, real API)

Push the canonical fixture, pull it back, assert `M == M1`. One push + one pull = 2-4 API calls total (depending on tables).

Implementation: `TestIdentityRoundTrip` in `test_roundtrip.py` — one test.

#### Level 3: Visual Inspection

The e2e tests leave tabs in the test doc for browser inspection.

### Supported Features (Canonical Form)

The fixture covers all of these — they must round-trip as identity:

- Headings: `# ` through `###### `
- Bold: `**text**`, Italic: `*text*`, Bold-italic: `***text***`
- Unordered lists: `- item`
- Ordered lists: `1. item` (Google renumbers)
- Hyperlinks: `[text](url)`
- Tables: `| cell |` with `| :---- |` separator, including bold/italic/emoji in cells
- Emoji: inline and in table cells
- Special characters: $, %, _, -, ~, #, <>, [], dots after numbers

### Unsupported Features (Not in Fixture)

These are known to not round-trip correctly and are excluded from the fixture:

- Nested lists (depth parsed but ignored in push)
- Inline code / backticks (parsed but no monospace styling applied)
- Code blocks (projected as `> ` prefixed lines)
- Strikethrough
- Images (separate blob store pipeline, tested in `test_e2e.py`)
- Blockquotes (as distinct from code block workaround)

### Pull-Side Normalizations

Google's markdown export has documented quirks. These normalizations are applied on pull (`native_md.py`):

- `* ` bullets to `- ` (Google uses `*`, we standardize to `-`)
- Strip trailing whitespace
- Unescape `\-`, `\>`, `\#`, `\~`, `` \` ``, `\_`, `\.`, `\=`, `\<`, `\[`, `\]`, `\*` (Google over-escapes)
- Strip italic wrapping from h6 headings (Google wraps h6 in `*...*`)
- Ensure trailing newline

### Pull-Side Normalizations

The pull side (`native_md.export_doc_markdown`) applies normalizations to Google's export:

- `* ` bullets to `- ` (standardize list markers)
- Strip trailing double-spaces (soft breaks)
- `\-` to `-` (over-escaped dashes)

Additional normalizations needed (identified by experiments):

- `\~` to `~`
- `\#` to `#`

These should be added to `export_doc_markdown` to expand the canonical subset.

## Consequences

**Positive:**
- Clear definition of what's supported vs. not
- Round-trip stability tests catch regressions automatically
- Progressive fixtures make it easy to add support for new features
- Unit tests pinpoint index arithmetic bugs without API calls
- xfail tests document the roadmap of features to fix

**Negative:**
- E2e tests require Google API credentials and a test document
- E2e tests are slow (multiple API round-trips per fixture)
- The canonical form is constrained by Google's export behavior, which may change

## References

- ADR 003: Google Docs Sync (original design, deferred push to v2)
- `experiments/compare_push.py`: md2docs vs native upload comparison
- `experiments/replay_json.py`: JSON replay prototype
- `gax/md2docs.py`: Current push pipeline
- `gax/native_md.py`: Pull pipeline (Drive API export + normalizations)
