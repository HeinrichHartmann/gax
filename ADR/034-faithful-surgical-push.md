# ADR 034: Faithful Surgical Push via Pull-Time Baseline

## Status

Accepted (2026-07-26). Amends ADR 032: patch-first flow retained;
stateless patch becomes the no-baseline degradation path; `--bulk`
is renamed `--force-replace`. Validated by two prototype rounds in
`experiments/tree_ir_prototype/` (see REPORT.md).

## Product Requirement

Pull a Google Doc so a human or an AI can read and edit its content as
markdown. On push, apply the user's edits as precise, surgical mutations
that keep **all** existing formatting alive — colors, fonts, alignment,
comments, suggestions, images, footnotes — including on documents that
gax's markdown cannot faithfully represent.

The markdown file does not need to be the only local state. gax may keep
additional fidelity data in the local store, sidecar files, or a
database. What matters: readable markdown surface, non-destructive push.

## Context

### Where formatting is destroyed today

1. **Loss happens at pull.** `ir.from_doc_json` (gax/gdoc/ir.py)
   captures only bold/italic/strikethrough/links, headings, lists, and
   tables. Colors, fonts, sizes, underline, alignment, indentation,
   footnotes, merged cells, positioned objects, and custom styles are
   silently dropped. The rendered markdown is the **only** local state;
   the raw Docs JSON is discarded.
2. **Bulk push regenerates the whole document** (`doc.py`
   `update_tab_content`: `deleteContentRange` 1→end, then re-insert from
   lossy markdown). Every push destroys all non-markdown formatting,
   even on untouched paragraphs. ADR 032 makes patch the default and
   demotes bulk to a fallback, which mitigates but does not fix this.
3. **The patch path (ADR 027/032) is the right algorithm, half built.**
   It re-fetches the remote at push time, renders it to markdown, diffs
   block-by-block against the local file, and only touches changed
   blocks. Two structural deficits remain:
   - **No baseline.** Diffing local markdown against *remote-rendered*
     markdown cannot distinguish "the user edited this" from "the
     representation renders this differently" or "a collaborator
     changed this since pull". There is no revision gate; concurrent
     remote edits are silently mixed into the diff.
   - **Paragraph-granularity clobbering.** An update is
     `deleteContentRange` + `insertText` of the whole paragraph.
     Editing one word in a paragraph destroys the colors, fonts, and
     comment anchors of the entire paragraph.

### Alternatives on the representation level

The option space for "AI collaboration on complex docs" was reviewed:

- **Richer markdown** (HTML spans, attribute annotations): can encode
  more, but the file stops being readable and still cannot represent
  suggestions, positioned objects, etc. Worst of both worlds.
- **HTML/DOCX as the surface** (Drive export/import): export is
  faithful, but import is a whole-document replace that clobbers
  comments, suggestions, and smart chips — and LLMs edit markdown
  more reliably than HTML.
- **Raw Docs JSON as the surface**: faithful but unreadable and
  hostile to both human and LLM editing (index arithmetic).
- **Pure API tools, no files** (MCP, ADR 033): good for point edits,
  loses the file/diff/git workflow that is gax's core value.
  Complement, not replacement.

Conclusion: **markdown is not the problem — discarding the source of
truth is.** Keep markdown as the lossy, readable *projection*; keep the
faithful document state locally; compute edits against the projection;
apply them surgically against the faithful state.

## Decision

Three pillars, building directly on the ADR 027/032 patch machinery:

### 1. Persist a pull-time baseline

On every pull (and after every successful push), store per tab:

- the **raw Docs JSON** of the tab body (plus `inlineObjects`, `lists`,
  `footnotes` sections), and
- the document **`revisionId`**,

in the existing content-addressed store (`~/.gax/store/`, gax/store.py
— same mechanism as image blobs). The tracking file references the
baseline in its per-section frontmatter:

```yaml
source: https://docs.google.com/document/d/...
baseline: sha256:ab12…      # CAS key of raw tab JSON at pull time
revision: ALm37BW…          # revisionId at pull time
```

The markdown body stays exactly as readable as today. Single-file
workflows keep working; the fidelity data lives in the store, not in
the working directory. A missing baseline (old files, hand-made files)
degrades gracefully to today's stateless patch behavior with a warning.

### 2. Three-way plan

Push computes a plan from three states:

- **Base**: markdown rendered deterministically from the stored
  baseline JSON (same `from_doc_json` + `render_markdown` pipeline).
- **Local**: the working markdown file.
- **Remote**: current `documents().get()` + `revisionId`.

Then:

- **User edits** = diff(base, local). Only these become mutations.
  Representation gaps (things the renderer drops) are identical in
  base and local and therefore produce **zero** mutations by
  construction — the no-op-push invariant falls out of the design.
- **Drift check** = remote `revisionId` vs stored `revision`. If they
  differ, compare diff(base, remote): if the drift is disjoint from
  the user's edits, rebase indexes onto the remote JSON and proceed;
  if it overlaps, abort with "remote changed — pull first" listing the
  conflicting blocks. Never silently merge overlapping edits.
- Mutations are mapped onto the **remote** JSON's `startIndex`/
  `endIndex` (as `diff_push.py` does today), applied in reverse index
  order in one `batchUpdate`.

#### Block alignment: anchored similarity matching

The reliability of diff(base, local) rests on correctly pairing base
blocks with local blocks — deciding *update* (surgical splice) vs.
*insert+delete* (block replace). Plain `SequenceMatcher` on
(type, text) keys misclassifies heavily rewritten paragraphs,
duplicate blocks, and moves. The alignment is therefore hierarchical:

1. **Heading anchors.** Pair headings that match with high confidence:
   same level, same text, **unique** within both documents, and
   order-preserving (longest-increasing-subsequence over candidate
   pairs, patience-diff style). Duplicate heading texts are never
   anchors. Anchors are *synchronization points*, not hard walls.
2. **Regions.** Consecutive anchor pairs partition base and local into
   corresponding regions. All remaining blocks — including non-anchor
   headings — are aligned within their region only.
3. **Similarity pairing within a region.** Unique-and-equal blocks
   pair first; remaining blocks pair by text-similarity ratio with a
   threshold (≈0.5): above ⇒ update (run-level splice), below ⇒
   insert + delete. Heading blocks only pair with headings of a
   compatible level, so a *renamed* heading is an update and its
   section content stays surgical.
4. **Graceful cases.** A *new* heading is simply an inserted block
   within its region — blocks beneath it still pair with their
   originals, so splitting a section produces one insert, not a
   section rewrite. A deleted heading is one block delete. If no
   confident anchors exist (tiny doc, all headings rewritten), the
   whole document is one region — i.e. degrade to flat alignment,
   never worse than the status quo.

Misalignment cost is bounded by construction: a wrongly unpaired block
becomes a block-level delete+insert (today's behavior, localized to
that block), never corruption elsewhere — and it is visible in the
plan preview before push.

### 3. Run-level splicing

Within a changed paragraph, do not delete and re-insert the whole
paragraph. Instead:

- diff the paragraph's text at run/character level (base vs local),
- `deleteContentRange`/`insertText` only the changed spans,
- untouched runs keep their `textStyle` (colors, fonts, links) and
  comment anchors untouched — because they are never deleted,
- newly inserted text inherits style from the insertion point (Docs
  API default) and then receives any markdown-specified styles
  (bold/italic/link) via `updateTextStyle` on the inserted range only.
  Prototype finding: this second pass is mandatory whenever the
  inserted text's intended style differs from the insertion context —
  insertion-point inheritance alone is not sufficient.

Implementation note (prototype finding): the Docs API places
`startIndex`/`endIndex` on the structural element *wrapper*, not
inside the `paragraph` dict — table-cell mutation code must read
indexes from the wrapper.

### Invariants (the definition of "surgical")

1. **No-op push ⇒ zero mutations.** Pull, change nothing, push:
   `plan()` is empty for any document, no matter how complex.
2. **Untouched content is untouched.** After pushing an edit to block
   A, every other block's JSON (text, styles, comment anchors) is
   unchanged, verified against the post-push `documents().get()`.
3. **Unrepresentable ⇒ untouchable, never destroyed.** Edits the
   translator cannot perform (table shape changes, footnote edits)
   fail loudly at plan time with a precise message — they are never
   approximated by bulk-replacing surrounding content.
4. **One plan for diff/push.** `diff` renders the plan; `push` applies
   the same plan after confirmation (GitHub issue #56's
   plan/apply architecture; terraform model).

Bulk full-replace remains available as an explicit escape hatch only
(`--force-replace`, renaming ADR 032's `--bulk` to signal
destructiveness). The interactive "fall back to bulk?" prompt on patch
failure is kept but must show *what* will be destroyed.

## Implementation Outline

Phased; each phase is independently shippable and lands behind the
existing patch-default flow.

- **Phase 0 — Fidelity spec suite.** E2E fixture doc with colors,
  fonts, alignment, comments, merged cells, footnotes, images.
  Tests for invariants 1–3 (no-op push zero mutations; single-block
  edit leaves sibling JSON byte-identical; unsupported edits fail at
  plan time). Adversarial alignment cases: duplicate paragraphs,
  an ~80%-rewritten paragraph, a renamed heading, a new heading
  splitting an existing section, a moved section, paragraph
  split/merge, emoji/surrogate-pair offsets (UTF-16). These are
  written first and fail against current code.
- **Phase 1 — Baseline persistence.** Pull/push write tab JSON +
  revisionId to the CAS store and reference them in frontmatter.
  Deterministic base rendering. Graceful degradation without baseline.
- **Phase 2 — Three-way plan.** diff(base, local) replaces
  diff(remote-rendered, local) as the edit source; revision gate and
  disjoint-drift rebase; conflict abort.
- **Phase 3 — Run-level splicing.** Sub-paragraph mutation
  granularity; style inheritance for insertions.
- **Phase 4 — CLI unification.** `gax doc diff`/`push` (and top-level
  `gax diff`) driven by the shared plan; `--bulk` → `--force-replace`.

Scope: gdoc first. The baseline-in-CAS pattern is designed to extend
to slides and sheets later (sheets already resolve fidelity at fetch
time via `valueRenderOption=FORMULA`; a baseline would additionally
enable three-way drift detection there).

## Planned Extension: Faithful Editable Surface (Tree IR)

The plan/mutation back-end is deliberately surface-agnostic:

```
md three-way diff    ──┐
                       ├──→ plan (mutations) → preview → batchUpdate
tree three-way diff  ──┘
```

There are exactly **two checkout modes**, each a self-contained
editing surface (no sidecar files; the baseline lives in the CAS store
in both modes):

- **md** (today's surface): lossy, compact, human-readable. Use to
  read documents and to write/push early drafts and text edits.
- **tree** (new, e.g. `.doc.gax.yaml`): faithful, verbose,
  LLM-friendly. A human would not read it. Serialization format (YAML
  vs JSON vs XML) to be settled in ADR 035.

The tree surface is diff-based like the markdown path, so it inherits
the same invariants: unchanged subtrees produce zero mutations, and
edits to attributes the mutation translator does not yet support fail
loudly at plan time ("un-editable = loud failure", the surface-level
analogue of "unrepresentable = untouchable"). Unlike the markdown
surface, it can express edits outside markdown's vocabulary (colors,
alignment, fonts).

Design of the Tree IR and its serialization: **ADR 035**. Sequencing:
markdown surface first (Phases 1–4, covers text-centric
collaboration), tree surface as Phase 5.

A related cheap addition: `gax get --json` — stateless faithful
inspection (raw doc JSON to stdout) for answering formatting/comments
questions without any round-trip machinery.

## Alternatives Considered

### Stateless patch only (status quo, ADR 032)

Harden the current remote-vs-local markdown diff without storing a
baseline. **Rejected**: cannot distinguish user edits from collaborator
drift or representation gaps; paragraph-level clobbering is inherent;
no concurrency safety. It remains the graceful-degradation path when
no baseline exists.

### Sidecar files in the working directory

Store baseline JSON next to the tracking file (`.doc.gax.md.d/` or
`file.json`). **Rejected**: keeps working directories clean,
single-file workflows intact, and reuses existing dedup/GC machinery
in the CAS store. Two files representing one document also invites
"which file wins" ambiguity. Each checkout mode (md or tree, see
Planned Extension) is a single self-contained surface; the baseline
lives in the CAS store for both.

### SQLite state database

A `~/.gax/state.db` mapping document → baseline/revision. **Rejected
for now**: the CAS + frontmatter reference achieves the same with less
machinery and no schema migrations. Revisit if per-document metadata
grows (sync timestamps, multi-account state).

### Richer surface formats (annotated markdown, HTML, DOCX, raw JSON)

See Context. All rejected: they either destroy readability, cannot be
imported non-destructively, or are hostile to human/LLM editing. Note:
a *designed* faithful surface (index-free Tree IR, ADR 035) is not in
this bucket — it is a planned extension, since diff-based push makes
it surgical and safe. What remains rejected is raw indexed Docs JSON
as an editing surface (index maintenance is hostile to LLM edits) and
whole-document import formats (HTML/DOCX).

### Block ID markers in the markdown

Embed stable per-block IDs in the file (e.g. `<!-- id:b7 -->` before
each block, or pandoc-style `{#id}` attributes) so local↔base linking
is exact rather than fuzzy. **Rejected as default**:

- Pollutes the readable surface — the product's primary requirement —
  and markdown has no invisible syntax.
- Creates a new corruption class (humans/LLMs duplicate IDs via
  copy-paste, drop markers, reorder across them), so validation plus
  a fuzzy fallback is required anyway; the fuzzy path would exist but
  be less exercised and less tested.
- Does not solve baseline↔remote alignment: the Docs JSON has no
  native block IDs, so collaborator-drift matching stays fuzzy
  regardless.
- Misalignment cost without IDs is already bounded and previewable
  (see alignment section).

**Revisit on evidence**: if the Phase 0 adversarial alignment cases
show the anchored similarity matcher failing in practice, introduce
IDs as an opt-in mode (e.g. `--anchors`), starting with heading-only
`{#id}` attributes (lowest noise, highest anchoring value).

### Full merge (OT/CRDT) for concurrent edits

**Rejected** (as in ADR 027): the Docs API has no revision-gated
writes. The revisionId check plus disjoint-drift rebase covers the
realistic collaboration case; overlapping edits abort safely.

## Consequences

**Positive**

- Complex, collaboratively-formatted documents survive gax round-trips;
  the no-op invariant is guaranteed by construction, not by converter
  completeness.
- Editing one word no longer destroys a paragraph's formatting or
  comment anchors.
- Concurrent collaborator edits are detected instead of silently mixed
  in.
- `diff` becomes trustworthy: it shows exactly the plan `push` applies.
- The IR does not need to grow toward full Docs fidelity — it stays a
  small, readable projection.

**Negative**

- Local state beyond the markdown file: the CAS store becomes
  load-bearing for push quality (though never for correctness — a lost
  baseline degrades to today's behavior, it does not corrupt).
- Push after pull-on-another-machine lacks a baseline unless the store
  is shared; degradation warning required.
- Run-level splicing adds index arithmetic complexity (UTF-16 offsets
  within paragraphs) and needs careful testing.
- Baselines accumulate in the store; needs GC (`ref` symlink pruning)
  eventually.

## References

- ADR 027: Diff-Based Document Push (alignment + mutation machinery)
- ADR 030: Markdown Strategy — Unified IR via Mistune
- ADR 032: Patch-First Push with Bulk Fallback
- GitHub issues: #56 (plan/apply architecture), #60 (clone/checkout
  simplification), #61 (recursive pull)
- gax/store.py: content-addressed store (baseline storage mechanism)
- gax/gdoc/diff_push.py: current patch pipeline
- gax/gdoc/ir.py: markdown IR (the lossy projection)
