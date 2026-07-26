# ADR 035: Faithful Tree IR — the Machine Editing Surface

## Status

Accepted (2026-07-26). Depends on ADR 034; implements its Phase 5.

Prototype-validated against the live Docs API: 13/13 scenarios pass
(no-op ⇒ zero mutations; run boundaries non-semantic; style-only
edits emit `updateTextStyle` with no delete/insert; beyond-markdown
formatting edits work; UTF-16/emoji index math correct). Token
measurement: YAML ≈ 0.54× raw JSON, ≈ 0.3× achievable with
table-default elision. See `experiments/tree_ir_prototype/REPORT.md`.
Known verification gap tracked in gax-hce (invariant-2 snapshot
equality).

## Context

ADR 034 establishes two checkout modes for Google Docs:

- **md** — lossy, compact, human-readable; for reading and for
  text-centric edits.
- **tree** — faithful, verbose, LLM-friendly; not meant for human
  reading. Enables edits outside markdown's vocabulary (colors,
  alignment, fonts, spacing).

Both modes are diff front-ends to the same plan/mutation back-end.
This ADR designs the tree surface: the in-memory model, the
serialization, and the edit contract.

The trick is the Tree IR. It must be:

1. **Faithful in practice** — captures what real documents actually
   use; anything not modeled is preserved opaquely, never dropped.
2. **Token-lean** — an LLM should be able to hold a real document in
   context. Raw Docs JSON is 10–50× the text size; that is the
   benchmark to beat by an order of magnitude.
3. **Index-free** — no `startIndex`/`endIndex`, no "paragraph 26", no
   counts to maintain. Order is list order; identity is position in
   the tree. An edit is: change the node, done. All index arithmetic
   happens at plan time against the baseline, never in the file.
4. **LLM-editable** — block-style serialization, text as readable
   scalars, styles as short attribute maps, defaults omitted.

## Decision

### One IR, two projections

Do not build a parallel format. Enrich the existing IR
(`gax/gdoc/ir.py` Block/Span) into the Tree IR — a superset of what it
captures today — and give it two projections:

```
                    ┌─→ render_markdown()   (lossy, md mode)
Docs JSON → Tree IR ┤
                    └─→ serialize_tree()    (faithful, tree mode)
```

- The Tree IR is the raw document tree with nodes **replaced by
  their compressed variants** where rewrite rules apply (see
  implementation strategy below); the common set (colors, fonts,
  sizes, underline, alignment, indentation, spacing, named styles,
  merged cells, footnotes) gets compact forms; everything else stays
  verbatim (appendix). Nothing is ever dropped.
- The markdown path is unchanged for users, but internally benefits:
  the baseline now retains the styles run-splicing must preserve
  (ADR 034 Phase 3), and `render_markdown` becomes a further, more
  aggressive compression of the same tree.

### Framing: serialization is compression, with a verbatim escape hatch

The ground truth is always the full document object (the raw Docs
JSON dict) — we hold it by construction, as the baseline. The tree
file is a **compression** of that object for human/LLM consumption:
type inference (`h1:` vs `namedStyleType: HEADING_1`), default
elision, run compaction, and ID/index removal are all compression
rules.

Like any compressor, it needs an escape hatch for incompressible
input. A node the compressor cannot render compactly is **not**
inlined verbosely (which would bloat the readable body). Instead the
node carries a reference, and the verbatim payload is appended in an
`appendix:` section at the end of the file:

```yaml
body:
- p:
    runs: ['Paragraph with exotic shading']
    raw: ref:r17
…
appendix:
  r17: {paragraphStyle: {shading: {…verbatim API dict…}}}
```

Readable head, untouchable tail: an LLM scans and edits the body; the
appendix is opaque, immutable under the edit contract, and may even be
truncated from an LLM's context without harming editability — push
resolves appendix content from the baseline regardless. Note:
YAML-native anchors/aliases cannot express this (aliases must follow
their anchor), so refs are schema-level (`ref:rNN`). Alternative:
links into the CAS store (as with image blobs), trading
self-containment for a cleaner file — decide with Phase A evidence.

#### Implementation strategy: top-down node rewriting

The compressor starts from the full raw in-memory tree (the JSON
dict) and walks it top-down, **replacing** nodes for which a rewrite
rule applies:

```
paragraph + HEADING_1          → {h1: "…"}
textRun, default style         → plain string
textRun, bold                  → {t: "…", b: true}
known table-cell defaults      → elided
no rule matches                → node left verbatim → appendix ref
```

Rules:

- Every rewrite rule ships with its inverse; the per-node invariant
  `expand(compress(node)) == node` is property-tested rule by rule.
  Whole-document round-trip identity is then just composition.
- **Coverage is monotone**: an unhandled node type is not a bug but
  an appendix entry. Adding a rule improves readability; it can never
  reduce correctness. Faithfulness becomes measurable: % nodes
  rewritten vs residual on a real-document corpus.
- The partially-rewritten mixed tree **is** the Tree IR: the YAML is
  its direct emission, the parser is the inverse walk, and the diff
  operates on it. (This supersedes maintaining a parallel
  typed-dataclass model as the primary representation; typed helpers
  may exist for ergonomics but the rewrite tree is canonical.)

### Serialization: YAML of the IR objects, compacted

Starting point per design discussion: a YAML serialization of the
Python IR objects — but a **hand-designed schema with a custom
serializer**, not a naive object dump. Compaction rules:

- **Default elision**: omit every attribute equal to its default or
  inherited named-style value. An unstyled paragraph of plain text
  serializes to one line.
- **Compact runs**: a run is a plain string when unstyled, a small
  map when styled. Shorthand keys for the common styles.
- **No noise**: no object IDs, no revision metadata, no indexes in
  the body. Document-level metadata lives in a small header.
- **Inherited-style suppression** (prototype findings): elide the
  known default paragraph styles the API attaches to every table cell
  (~20 lines of border/shading noise per cell — kept in `raw:` for
  faithful push, hidden from serialization); suppress link-implied
  `color`/`underline` when `url` is present (avoids spurious style
  diffs on link removal); model list nesting as a first-class `depth`
  field and suppress the redundant `indentStart`/`indentFirstLine`
  the API uses to encode it.

Sketch (schema to be frozen after the corpus experiment, below):

```yaml
source: https://docs.google.com/document/d/…/
kind: doc-tree/v1

body:
- h1: Quarterly Report
- p:
  - "The results were "
  - {t: strong, b: true, color: "#cc0000"}
  - " across all regions."
- p: {align: center, runs: ["Confidential"], style: {size: 9}}
- ul:
  - li: ["Revenue up "]
  - li: ["Costs flat"]
- table:
    rows:
    - [["Region"], ["Revenue"]]
    - [["EMEA"], [{t: "4.2M", b: true}]]
- p:
    runs: ["Legacy paragraph with unmodeled properties"]
    raw: {paragraphStyle: {shading: {…}}}   # opaque passthrough
```

Format choice (YAML vs JSON vs XML) is **open pending measurement**:
serialize a corpus of real documents in all three and compare token
counts and LLM edit reliability. Working hypothesis: YAML block style
wins on tokens and editability; JSON wins on parser strictness; XML
is likely out (token overhead). The IR is format-agnostic either way
— the serializer is swappable.

### The faithfulness contract

- **Round-trip identity**: `serialize(from_doc_json(J))` followed by
  `parse` must reproduce an IR that maps back to `J` semantically
  (formatting-equivalent, index-normalized). This is a property test
  over a corpus of real documents, not a best-effort goal. The `raw:`
  passthrough is what makes it achievable without modeling all of
  Google Docs.
- **Edit contract** (stated in the file header comment for LLM
  consumers): edit text, styles, and structure freely; never edit
  `raw:` blobs; never invent attributes outside the schema. Parsing
  validates against the schema and rejects violations with precise
  errors before any plan is computed.

### Push: same three-way plan as md mode

```
base tree   = from_doc_json(baseline JSON from CAS)
local tree  = parse(working .yaml file)
remote      = documents().get() + revisionId gate  (ADR 034 §2)
```

- Tree diff: align blocks (same anchored similarity machinery as
  ADR 034 — headings as anchors, similarity pairing in regions; tree
  nodes carry richer signals, e.g. table shape, which improve
  pairing).
- Within a changed paragraph: flatten runs to styled text, diff the
  text, re-derive minimal `deleteContentRange`/`insertText`/
  `updateTextStyle` mutations — run boundaries in the file are *not*
  semantic, so an LLM that merges or splits runs without changing
  text+style produces zero mutations.
- Style-only edits emit `updateTextStyle`/`updateParagraphStyle` with
  no delete/insert — comment anchors survive.
- Edits to attributes the translator does not support yet (and any
  edit to `raw:`) fail loudly at plan time, per ADR 034's invariant 3.
  Translator coverage grows incrementally and safely.

### CLI surface

```
gax doc clone <url> --format=tree    # produces .doc.gax.yaml
gax pull  <file.doc.gax.yaml>        # mode inferred from extension
gax diff  <file.doc.gax.yaml>        # same plan preview as md mode
gax push  <file.doc.gax.yaml>
```

No sidecars: each checkout is one self-contained file (or `.d/`
folder for multi-tab); the baseline lives in the CAS store for both
modes (ADR 034 §1). md and tree checkouts of the same document are
independent working copies.

## Implementation Outline

- **Phase A — Corpus experiment (before schema freeze).** Serialize
  ~10 real documents (markdown-native, human-authored, heavily
  formatted) via a prototype serializer in YAML/JSON. Measure token
  counts vs raw JSON and vs md. Verify round-trip identity. Freeze
  schema `doc-tree/v1` and the format decision on this evidence.
- **Phase B — IR enrichment.** Extend `ir.py` with the typed style
  fields and `raw:` passthrough; `from_doc_json` captures instead of
  drops. Markdown rendering unchanged. Round-trip property tests.
- **Phase C — Serializer/parser.** `serialize_tree`/`parse_tree` with
  default elision and schema validation.
- **Phase D — Tree plan front-end.** Tree diff → shared mutation
  translator; style-only mutation support (`updateTextStyle` color/
  font/size/alignment as the first coverage increment).
- **Phase E — CLI wiring.** `--format=tree`, extension-based dispatch
  through the existing `Resource.from_file` mechanism.

Prerequisites: ADR 034 Phases 1–2 (baseline store, three-way plan,
revision gate). Phase A can start immediately — it is evidence
gathering with no product surface.

## Alternatives Considered

### Raw indexed Docs JSON as the surface

Fully faithful, zero design work. **Rejected**: every text edit
invalidates the UTF-16 indexes of all subsequent elements; an LLM
editing indexed JSON corrupts it. Token count is 10–50× text. Kept
read-only as `gax get --json` for inspection.

### Naive object dump (dataclass → YAML with all fields)

The literal "yaml of the python objects". **Rejected as the file
format**: emits every default, unstable across refactors, and couples
the on-disk format to internal class layout. Retained as the *mental
model* — the schema is a stable, compacted projection of those same
objects.

### Annotated markdown (markdown + style attributes)

One surface for both audiences. **Rejected** in ADR 034: destroys
readability without reaching faithfulness; the two-mode split serves
each audience with the right tool.

### XML serialization

More redundant token-wise (closing tags, attribute syntax); LLMs
handle it well but no advantage over YAML/JSON for a tree of text
runs. Will be measured in Phase A only if YAML and JSON both
disappoint.

## Consequences

**Positive**

- AI agents can perform formatting edits (colors, alignment, fonts)
  surgically — impossible in md mode forever, by design.
- The md path's fidelity also improves (enriched baseline IR).
- Run boundaries are non-semantic: LLM run-merging sloppiness does
  not produce spurious mutations.
- One IR and one plan back-end to maintain; the surfaces are thin.

**Negative**

- Schema design and freeze is real work; `doc-tree/v1` becomes a
  compatibility surface we must version.
- The `raw:` passthrough hides content from the LLM (by design) —
  documents dominated by unmodeled features are readable but only
  partially editable in tree mode.
- Two checkout modes to document and support; users must pick
  (mitigation: md remains the default; tree is opt-in via
  `--format=tree`).

## References

- ADR 034: Faithful Surgical Push via Pull-Time Baseline (back-end,
  invariants, alignment)
- ADR 030: Markdown Strategy — Unified IR via Mistune (the IR this
  enriches)
- gax/gdoc/ir.py: current Block/Span IR
- gax/gdoc/diff_push.py: mutation translator to be shared
