# Tree IR Prototype Report

## Summary

This prototype validates the core bet of ADR 034 (faithful surgical push via
pull-time baseline) and ADR 035 (faithful Tree IR as a machine editing surface)
against the live Google Docs API.

**Result: 13/13 scenarios pass. GO recommendation for ADR 034 + 035.**

## Results Matrix

| # | Scenario | Result | Mutations | Notes |
|---|----------|--------|-----------|-------|
| 1 | No-op (serialize → parse → diff) | ✅ PASS | 0 | Full rich doc round-trips cleanly |
| 2 | Run-boundary noise (re-split runs) | ✅ PASS | 0 | Non-semantic run splits produce zero mutations |
| 3 | Word edit mid-paragraph | ✅ PASS | 3 | delete + insert + (index adj); bold/color/link survive |
| 4 | Style-only edit (color change) | ✅ PASS | 1 | Single updateTextStyle, NO delete/insert |
| 5 | Paragraph-style edit (alignment) | ✅ PASS | 1 | Single updateParagraphStyle, text untouched |
| 6 | Formatting beyond markdown (underline + font size) | ✅ PASS | 1 | Proves tree surface exceeds md vocabulary |
| 7 | Insert paragraph between styled paragraphs | ✅ PASS | 1 | Neighbors content-identical after insert |
| 8 | Delete one paragraph | ✅ PASS | 1 | Neighbors unchanged |
| 9 | Heading rename (text + level change) | ✅ PASS | 5 | Text splice + heading level update; section content untouched |
| 10 | Table cell text edit | ✅ PASS | 4 | Other cells (incl. bold cell) byte-identical |
| 11 | Emoji paragraph edit (UTF-16 stress) | ✅ PASS | 5 | Surrogate pairs handled correctly; emoji preserved |
| 12 | Comment anchor survival (stretch) | ⏭ SKIPPED | — | Out of scope for first pass; filed as follow-up |
| — | Round-trip identity | ✅ PASS | — | Structure + text + styles preserved through serialize/parse |
| — | Token measurement | ✅ PASS | — | See table below |

## Token Measurements

| Format | Chars | ~Tokens (char/4) | Ratio vs Raw JSON |
|--------|-------|-------------------|-------------------|
| Raw Docs JSON | 10,206 | ~2,551 | 1.00× |
| Tree IR YAML | 5,528 | ~1,382 | 0.54× |
| Markdown (lossy) | 322 | ~80 | 0.03× |

**Key ratios:**
- YAML is **54%** of raw JSON size — nearly 2× compression while remaining faithful
- Markdown is **6%** of YAML — but lossy (no colors, fonts, alignment, underline)
- YAML/MD ratio: 17× — the cost of faithfulness in token budget

**Interpretation:** The YAML tree surface is viable for LLM context. A typical
5-page Google Doc (raw JSON ~50-100K chars) would serialize to ~27-54K chars
of YAML (~7-14K tokens), comfortably within context windows. The 2× compression
over raw JSON comes from default elision and compact run notation. Further
compression is achievable by filtering table cell raw styles (see API Surprises).

## YAML Serialization Sample

```yaml
source: https://docs.google.com/document/d/EXAMPLE/edit
kind: doc-tree/v1
body:
- h1: Test Heading
- p:
    runs:
    - 'This is plain text with '
    - b: true
      t: bold word
    - ' and '
    - color: '#cc0000'
      t: colored
    - ' '
    - u: true
      url: https://example.com
      color: '#1155cc'
      t: link here
    - ' see more.'
- p:
    runs:
    - size: 9
      t: Confidential — internal only
    style:
      align: center
- ul:
    text: Revenue increased significantly
    style:
      indent_start: 36
      indent_first: 18
- ul:
    text: Costs remained flat
    style:
      indent_start: 36
      indent_first: 18
- ul:
    text: Market share grew
    style:
      indent_start: 36
      indent_first: 18
- table:
    rows:
    - - Region
      - Revenue
    - - EMEA
      - runs:
        - b: true
          t: 4.2M
- p: 'Emoji test: 🎉 party 🚀 rocket 🏳️‍🌈 flag 𝕳𝖊𝖑𝖑𝖔'
```

*Note: Table shown with cell raw styles stripped for readability. Full output
includes opaque `raw:` passthrough for table paragraph styles (border/shading
defaults). See API Surprises below.*

## API Surprises / Limitations Discovered

### 1. Table cells carry verbose default paragraph styles

Every table cell paragraph carries ~20 lines of default border/shading/spacing
properties in its paragraphStyle. This is the Docs API's representation of
"table cell formatting inherited from the table style." Our `raw:` passthrough
captures it faithfully but it bloats the YAML for tables significantly.

**Impact:** The 0.54× compression ratio would improve to ~0.3× if table cell
defaults were elided. This is a production schema decision: either recognize
and elide known table defaults, or treat them as non-editable noise.

**Recommendation:** Phase B of ADR 035 should classify known table cell
paragraph style defaults and elide them from serialization while preserving
them in the `raw:` passthrough for faithful push.

### 2. Link styling auto-applies foreground color + underline

When a textRun has a `link` property, the Docs API also reports
`foregroundColor: #1155cc` and `underline: true` in the textStyle. These are
inherited visual styles, not explicit user formatting. The prototype captures
them as explicit style attributes.

**Impact:** Removing a link but keeping the text will leave an explicit
blue+underline if naively diffed. The three-way diff handles this correctly
(style changes are relative to baseline), but the serialization is noisier
than necessary.

**Recommendation:** Treat link-implied styles as inherited (suppress
`color`/`underline` when `url` is present) in a production schema.

### 3. Bullet list indent is paragraph-level

Bulleted list items carry `indentStart` and `indentFirstLine` in their
paragraphStyle. This is Google Docs' way of representing list nesting level.
The prototype preserves it via `para_style` but it clutters the serialization.

**Recommendation:** Model list depth as a first-class `depth` field (already
done) and suppress the corresponding indent values from serialization.

### 4. `startIndex`/`endIndex` location in table cells

The Docs API places `startIndex`/`endIndex` on the structural element wrapper
(the dict containing `"paragraph": {...}`), NOT inside the paragraph dict itself.
This caught a bug during development. The existing `diff_push.py` code appears
to work around this differently.

### 5. UTF-16 index math with emoji works correctly

The prototype's `_utf16_len` helper correctly handles surrogate pairs
(🎉 = 2 UTF-16 code units, 𝕳 = 2 units). The character-level diff combined
with UTF-16 offset calculation produces correct index ranges for the Docs API.
No API-level surprises here.

## Go/No-Go Recommendation

### GO — with design refinements

The prototype demonstrates that all four ADR 034 invariants hold:

1. **No-op push ⇒ zero mutations** — Validated. Serialize → parse → diff
   produces empty plan for a rich document with headings, lists, tables,
   colors, fonts, links, alignment, and emoji.

2. **Untouched content is untouched** — Validated. Word edits, style edits,
   insertions, and deletions all leave sibling content unchanged.

3. **Style edits produce style-only mutations** — Validated. Color/font/
   underline changes emit `updateTextStyle` without any `deleteContentRange`.
   Paragraph alignment changes emit `updateParagraphStyle` without touching text.

4. **Run boundaries are non-semantic** — Validated. Re-splitting runs with
   identical text+style produces zero mutations.

### Design changes the evidence demands

1. **Table cell default elision** — The `raw:` passthrough for table cells
   is too verbose. Phase B must define a known-defaults list for table cell
   paragraph styles and suppress them from serialization. This is a schema
   design task, not an algorithm problem.

2. **Link-implied style suppression** — Suppress auto-applied
   foregroundColor/underline on linked runs to reduce YAML noise and avoid
   spurious style diffs when links are removed.

3. **List indent suppression** — Model list depth explicitly, suppress the
   redundant `indentStart`/`indentFirstLine` that Google uses internally.

4. **Structured element index access** — The production `_diff_table_cells`
   should access `startIndex`/`endIndex` from the structural element wrapper,
   not the paragraph dict.

5. **Two-pass style application for text edits** — When text is changed AND
   the new text needs styling (e.g., inserting a bold word), the current
   prototype relies on the Docs API's insertion-point style inheritance. A
   production implementation needs a second pass to explicitly style newly
   inserted text that differs from the insertion context.

### Phase sequencing confirmed

The ADR 034/035 phase plan is validated:
- Phase 1 (baseline persistence) can proceed immediately
- Phase 2 (three-way plan) is proven viable
- Phase 3 (run-level splicing) works as designed
- Phase 5 (tree surface) delivers on the "exceeds markdown vocabulary" promise

## Files

| File | Purpose |
|------|---------|
| `enriched_ir.py` | Enriched IR: TextStyle, ParagraphStyle, Span, Block types; `from_doc_json` |
| `yaml_serializer.py` | YAML serializer/parser with default elision and compact runs |
| `tree_diff.py` | Three-way diff engine: normalize, diff, plan, plan_to_requests |
| `conftest.py` | Test fixtures: scratch_doc, populate_rich_doc |
| `test_tree_ir.py` | 13 integration tests (11 scenarios + token measurement + round-trip) |

## Running

```bash
direnv exec . python -m pytest experiments/tree_ir_prototype/ -m e2e -v
```

Requires: `GAX_TEST_DOC` env var (for session fixture), authenticated via `gax auth login`.
