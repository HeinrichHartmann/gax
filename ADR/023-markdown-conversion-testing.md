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

### Testing Strategy: Round-Trip Projections

The mathematical property we want:

- Let `push: Markdown -> GoogleDoc` and `pull: GoogleDoc -> Markdown`
- `pull . push` is a **projection**: applying it twice yields the same result as applying it once
- For a well-defined subset of markdown (the "canonical form"), `pull . push` is the **identity**

Concretely:

```
M  -push->  D1  -pull->  M1  -push->  D2  -pull->  M2

Assert: M1 == M2  (stability / idempotency)
Assert: M == M1   (for supported features — identity on canonical subset)
```

The first cycle (`M -> M1`) may lose information (unsupported features). But the second cycle (`M1 -> M2`) must be stable. Markdown in "canonical form" (what Google exports) should survive perfectly.

### Test Levels

#### Level 1: Unit Tests (no API calls)

Test `parse_markdown` and `generate_requests` in isolation.

- **Parser tests**: Verify AST nodes for each markdown construct
- **Request generation tests**: Verify plain_text output and request structure (action types, index ranges, tab_id propagation)
- **Index arithmetic tests**: The most fragile area. Multiple nodes, mixed formatting, emoji (UTF-16 surrogate pairs), tables as placeholders
- **Known bugs as xfail**: Pin desired behavior for ordered lists, inline code, links, heading unescape. These become regression tests once fixed.

#### Level 2: Round-Trip Stability Tests (e2e, real API)

Progressive fixtures with increasing complexity:

| Level | Fixture content | Expectation |
|-------|----------------|-------------|
| 1 | Plain paragraphs | `M == M1` (identity) |
| 2 | Headings + paragraphs | `M == M1` |
| 3 | Bold and italic | `M == M1` |
| 4 | Unordered lists | `M == M1` |
| 5 | Ordered lists | `M1 == M2` (stable after first cycle) |
| 6 | Tables with plain cells | `M1 == M2` |
| 7 | Tables with formatted cells | `M1 == M2` |
| 8 | Code blocks | `M1 == M2` |
| 9 | Full mixed document | `M1 == M2` |

Each fixture is written in **canonical form** (the markdown style Google exports): `- ` bullets, `**bold**`, `| :---- |` table alignment, no trailing spaces. Fixtures in canonical form should achieve `M == M1` (identity). Fixtures with unsupported features should achieve `M1 == M2` (stability).

The e2e test procedure for each fixture:

```python
def assert_roundtrip_stable(md_content, doc_id, docs_service, drive_service):
    """Push twice, pull twice. Assert second cycle is stable."""
    # Cycle 1
    tab1 = create_and_push(doc_id, "rt_cycle1", md_content)
    m1 = pull_tab(doc_id, "rt_cycle1")

    # Cycle 2
    tab2 = create_and_push(doc_id, "rt_cycle2", m1)
    m2 = pull_tab(doc_id, "rt_cycle2")

    assert m1 == m2, f"Not stable:\n{unified_diff(m1, m2)}"
```

Fixtures that should be identity (canonical form) additionally assert `md_content == m1`.

#### Level 3: Visual Inspection

The e2e tests create tabs in the test doc. These can be visually inspected in the browser to catch rendering issues that markdown comparison misses (e.g., wrong heading level, missing bullet style, broken table borders).

### Canonical Markdown Form

The "canonical form" is the subset of markdown that round-trips perfectly. It is defined empirically by what Google's Drive API markdown export actually produces after our pull-side normalizations. This form is discovered, not designed — we run fixtures through a push/pull cycle and observe what comes back.

**Known canonical properties** (to be expanded as we test):

- Headings: `# ` through `###### `
- Bold: `**text**`, Italic: `*text*`
- Unordered lists: `- item` (not `* item`)
- Ordered lists: `1.` / `2.` (auto-numbered by Google)
- Tables: `| cell |` with `| :---- |` separator
- No inline code, no links, no images (until supported)

**Known non-obvious behaviors** (discovered through testing):

- Google Docs exports paragraph spacing as blank lines, but our push inserts paragraphs as consecutive `\n` — blank lines in markdown are lost on push. The canonical form for paragraph spacing depends on what `generate_requests` produces, not what markdown convention expects.
- Google's export escapes `.` at end of sentences (`1\.`), `#`, and `~` with backslashes. Pull-side normalizations must unescape these.
- Trailing whitespace and final newlines may differ between Google's export and our fixtures.

The canonical form evolves as we fix bugs and add features. Each test fixture documents what "correct" looks like for that complexity level, and the test suite serves as the living specification.

Features outside the canonical subset (inline code, links, strikethrough, nested lists, blockquotes) are explicitly unsupported. The test suite documents which features are in which category.

### Pull-Side Normalizations

The pull side (`native_md.export_doc_markdown`) applies normalizations to Google's export:

- `* ` bullets to `- ` (standardize list markers)
- Strip trailing double-spaces (soft breaks)
- `\-` to `-` (over-escaped dashes)

Additional normalizations needed (identified by experiments):

- `\~` to `~`
- `\#` to `#`

These should be added to `export_doc_markdown` to expand the canonical subset.

## Implementation Status

### Test Suite (`tests/test_roundtrip.py`)

14 parametrized fixtures of progressive complexity, from single paragraph to mixed documents with tables, lists, and formatting. Three test classes:

- **TestStability**: push → pull → push → pull, assert `M1 == M2`. All 14 pass.
- **TestProjectionDiff**: push → pull, report diff against original. Informational, does not fail.
- **TestComplexIdempotency**: full rich formatting fixture round-trip stability. Passes.

Tab cleanup (module-scoped fixture) prevents hitting Google's 100-tab limit.

### Fixes Applied

1. **Ordered lists**: now use `createParagraphBullets` with `NUMBERED_DECIMAL_NESTED` instead of plain `1. ` text prefix. Produces proper Google Docs numbered lists.
2. **Paragraph spacing**: empty paragraphs (`\n`) inserted between node types that need visual spacing (consecutive paragraphs, around headings/lists/code/tables). Preserves blank lines through round-trip.

### Remaining Projection Diffs

After the fixes above, the only remaining diffs between original and first-cycle output are:

| Issue | Fixtures affected | Severity |
|-------|------------------|----------|
| Trailing `\n` missing | All | Trivial |
| Ordered list renumbering (`2.` → `1.`) | ordered_list, mixed_document | Google behavior, not a bug |
| Stray chars after tables (`0`, `4`) | simple_table, table_with_bold | Bug in table placeholder cleanup |
| Code fences stripped (``` removed) | code_block | Unsupported feature |

All fixtures are idempotent after one cycle despite these diffs.

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
