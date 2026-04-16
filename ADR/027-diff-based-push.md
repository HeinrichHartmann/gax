# ADR 027: Diff-Based Document Push

## Status

Proposed

## Context

The current push pipeline (`md2docs.py`) destroys and recreates all tab content on every push. This works for simple documents but has fundamental limitations:

- **Formatting loss.** Any Google Docs formatting not representable in markdown (colors, font sizes, centered alignment, footnotes, images, comments, suggestions) is destroyed on push. Collaborators who polished the document lose their work.
- **No incremental updates.** Changing a single paragraph requires re-uploading the entire document. This is slow, wasteful, and creates unnecessary revision history noise.
- **Fragile for complex documents.** The full-replace approach requires perfect reconstruction of every element (table population, bullet creation, inline formatting). Any bug in that pipeline corrupts the entire document, not just the part that changed.

The goal of gax is not a faithful markdown-to-Google-Docs converter. Markdown is a *simplified machine-readable view* of the document — an interface for LLMs and local editing. We want to apply targeted edits derived from local markdown changes without destroying the rest of the document.

## Approach: Diff-Based Push

Instead of replacing all content, push computes the difference between the local markdown and the last-known state, then translates that difference into minimal Google Docs API mutations.

### The Flow

```
1. Pull:    Google Doc  →  markdown + index mapping (in memory)
2. Edit:    User/LLM modifies markdown locally
3. Push:    Parse edited markdown
            Diff against base markdown AST
            Translate diff operations to Docs API mutations
            Apply mutations to the live document
```

### Step 1: Alignment — Mapping Markdown AST to Google Doc Indices

On push, we need to know which Google Doc elements correspond to which markdown AST nodes. We do this by pulling the current state and aligning the two structures:

- **Pull the markdown** via the Drive API markdown export
- **Read the doc JSON** via `documents().get()` (provides the body element tree with `startIndex`/`endIndex` for every element)
- **Parse the pulled markdown** into our AST (using mistune)
- **Walk both structures in parallel**, skipping empty paragraphs in the doc JSON

The Google Doc JSON is always finer-grained than the markdown AST — the Drive API markdown export sometimes merges consecutive paragraphs (those without a blank line between them) into a single markdown paragraph. We handle this by accumulating doc elements until their combined text length matches the AST node.

This produces a mapping: each AST node maps to one or more Google Doc body elements with known indices.

### Step 2: AST Diff

Diff the base AST (from the pull) against the edited AST (from the local file). Since both are flat sequences of typed nodes, this is a sequence alignment problem. Operations:

- **Update**: node at position N changed its text or formatting
- **Insert**: new node appeared between positions N and N+1
- **Delete**: node at position N was removed

Standard sequence matching (`difflib.SequenceMatcher` or similar) on the node list, keyed by type + text content.

### Step 3: Translate to Docs API Mutations

Each diff operation maps to Docs API requests:

| Diff operation | Docs API request(s) |
|---|---|
| Update paragraph text | `deleteContentRange` + `insertText` at the mapped index |
| Update inline formatting | `updateTextStyle` at the mapped index range |
| Update heading level | `updateParagraphStyle` with new `namedStyleType` |
| Insert paragraph | `insertText` at the index after the preceding element |
| Delete paragraph | `deleteContentRange` for the mapped index range |
| Update table cell | `deleteContentRange` + `insertText` within the cell's index range |
| Insert/delete list item | Same as paragraph, plus `createParagraphBullets` / `deleteParagraphBullets` |

Mutations are applied in reverse index order so that earlier indices remain stable.

### Verification

Before applying mutations, we verify the upstream document hasn't changed since the pull. This can be done by comparing the document's `revisionId` or by re-pulling and checking that the base markdown matches. If it has changed, we abort and ask the user to re-pull.

## Experimental Validation

We validated the alignment approach with three experiments of increasing complexity:

### Experiment 1: Markdown-native document (87 nodes)

The e2e test fixture — headings, paragraphs, lists, 7 tables with emoji/formatting, hyperlinks, special characters.

**Result: 87/87 exact matches.** Simple parallel walk with empty-paragraph skipping.

### Experiment 2: Human-style document (22 nodes)

A meeting notes document created directly via the Docs API (not from markdown). Includes centered title, bold+italic labels, colored text, footnotes, superscript, small-font text, a populated table.

**Result: 18/18 aligned (15 exact + 3 merged).** Three cases where the Drive API markdown export merged consecutive paragraphs without blank lines (e.g., "Date:" and "Attendees:" became one markdown paragraph mapping to two doc elements). The accumulation heuristic handled all three.

**One structural mismatch found:** Footnotes. The footnote reference (`[^1]`) appears inline in the paragraph text, and the footnote definition (`[^1]: ...`) appears at the end of the markdown with no corresponding body element (footnote content lives in a separate `footnotes` section of the doc JSON). Footnotes would need special-case handling.

### Experiment 3: Stress test (35 nodes)

Adversarial structures: emoji-heavy table cells, three back-to-back tables, lists immediately before/after tables, nested lists (3 levels of indentation), multi-paragraph table cells, wide 6-column table, tables with hyperlinks.

**Result: 35/35 exact matches.** No merges needed. Notable findings:

- **Multi-paragraph table cells**: the Drive API export joins multiple cell paragraphs into a single markdown cell line. Alignment at the table level still works; cell-internal edits would need to handle the N:1 mapping.
- **Nested lists**: Google Docs reports all items at nesting level 0 despite visual indentation. The markdown export flattens them to top-level `- ` items. Alignment works; nesting information is lost.
- **Back-to-back tables**: empty paragraphs between them are skipped cleanly.

## Alternatives Considered

### Full-replace (current approach)

Delete all content, regenerate from markdown. Simple but destroys all non-markdown formatting. See ADR 023 for details.

**Rejected for complex documents** because it cannot preserve collaborator edits, comments, or rich formatting.

### Hand-rolled pull converter

Instead of using the Drive API markdown export, walk the Google Doc JSON ourselves and produce markdown with embedded index metadata. This gives perfect 1:1 correspondence by construction.

**Deferred.** The alignment experiment shows the Drive API export + parallel walk works well enough. The only structural mismatch found (footnotes) is a niche case. Hand-rolling the pull converter is a large effort that can be pursued later if alignment proves insufficient for more document types.

### Operational Transformation / CRDT

Full merge support for concurrent upstream edits.

**Rejected.** The Docs API has no compare-and-swap or revision-gated writes. We handle concurrency by verifying the upstream state before pushing and aborting if it changed.

## Decision

Implement diff-based push as an **experimental second push path**, gated behind the `--patch` flag on `gax doc tab push`. The current full-replace approach remains the default. The `--patch` path is available for further evaluation on real-world documents; once it has proven robust across the document types we care about, we may promote it to the default (or to the primary strategy with full-replace as a fallback).

### Implementation Plan

1. **Alignment module**: Extract the parallel-walk alignment logic from the experiments into `gax/align.py`. Input: pulled markdown + doc JSON body. Output: list of `MappedNode(ast_node, doc_elements, start_index, end_index)`.

2. **AST diff module**: `gax/ast_diff.py`. Input: base AST + edited AST. Output: list of `EditOp(type, position, old_node, new_node)`.

3. **Mutation translator**: `gax/mutations.py`. Input: list of `EditOp` + alignment mapping. Output: list of Docs API `batchUpdate` requests.

4. **Push command integration**: Wire the diff-based pipeline into `gax doc tab push` behind an experimental `--patch` flag. Re-derive the base state on push and verify upstream unchanged before applying. The default push path remains full-replace.

5. **Fallback**: If the diff contains structural changes that the mutation translator can't handle (e.g., table dimension changes), abort with a message directing the user to run without `--patch`.

6. **Promotion criteria**: Before making `--patch` the default, evaluate it on a representative set of real documents (markdown-native, human-authored, collaboratively edited) and confirm it preserves formatting without data loss or index drift.

## Consequences

**Positive:**

- Preserves Google Docs formatting, comments, and suggestions that markdown doesn't represent
- Enables incremental edits — change one paragraph without touching the rest
- Safer for collaborative documents
- Smaller API payloads for minor edits
- Edit plan is inspectable before applying

**Negative:**

- More complex than full-replace — three new modules (alignment, diff, mutations)
- Index arithmetic must account for cascading shifts when multiple mutations are applied
- Footnotes, images, and other non-body elements need special handling
- The Drive API markdown export is a dependency we don't control — changes to its behavior could break alignment

## References

- ADR 023: Markdown-to-Google-Docs Conversion and Testing Strategy
- `experiments/align_ast_json.py`: Alignment experiment on markdown-native fixture
- `experiments/align_complex_doc.py`: Alignment experiment on human-style document
- `experiments/align_adversarial.py`: Alignment experiment with footnotes, colored text, centered alignment
- `experiments/align_tables_stress.py`: Stress test with emoji tables, nested lists, multi-paragraph cells
- `gax/md2docs.py`: Current push pipeline (AST nodes, mistune parser)
