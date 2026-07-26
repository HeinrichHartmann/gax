# ADR 036: Tree IR Compression — Making the YAML Readable

## Status

Proposed (extends ADR 035; drives gax-cvi.12/13/14)

## The Problem, by Example

This is what `gax/gdoc/tree.py` produces today for one bullet point of
the SREcon "Co-chair Responsibilities" doc:

```yaml
- ul:
    items:
    - runs:
      - bg: '#ffffff'
        font: Calibri
        t: 'Following the '
      - bg: '#ffffff'
        font: Calibri
        t: co-chair timetable
      style:
        line_spacing: 100
        _raw_direction: LEFT_TO_RIGHT
        space_above: 2
        _raw_pageBreakBefore: false
      _bullet:
        listId: kix.18o6dcun6xyq
        textStyle:
          backgroundColor: {color: {rgbColor: {red: 1, green: 1, blue: 1}}}
          weightedFontFamily: {fontFamily: Calibri, weight: 400}
      _indent_start: {magnitude: 36, unit: PT}
      _indent_first: {magnitude: 18, unit: PT}
```

Twenty-two lines for the sentence "Following the co-chair timetable".
Nothing is *wrong* — it is faithful — but a human or LLM reading this
drowns in repetition. The whole document uses Calibri on white; Google
reports that on every single run, and we currently write it down every
single time.

## The Goal, by Example

The same bullet point after the compression proposed here:

```yaml
- ul:
    items:
    - Following the co-chair timetable
```

And the whole document shrinks to roughly:

```yaml
kind: doc-tree/v1
styles:
  s1: {font: Calibri, bg: '#ffffff'}          # discovered, most common
  s2: {font: Calibri, b: true, color: '#4f81bd', size: 13}
default: s1                                    # applies where nothing is said
body:
- h2: {s: s2, t: "The Co-chairs' Responsibilities"}
- ul:
    items:
    - Collaborating with USENIX on the planning schedule
    - Following the co-chair timetable
    - {b: true, t: Writing, organizing and promoting the CFP}
    - ...
appendix:
  r1: {_bullet: {listId: kix.18o6dcun6xyq, textStyle: {...}}}
```

Everything is still recoverable — nothing is deleted, only *factored
out*. Measured target: the Co-chair doc goes from ratio 0.72 (vs raw
JSON) to ≤ 0.35.

## The Compression, Step by Step

Five steps, always in this order. Each step is a small transformation
with an exact inverse ("undo"), tested per step.

### Step 1 — Drop attributes that say nothing

Google attaches values that are simply *what you get anyway when the
attribute is absent*: `_raw_direction: LEFT_TO_RIGHT`,
`_raw_pageBreakBefore: false`, `line_spacing: 100`, white background.
Delete them. Putting them back is automatic — absent means default.

```yaml
# before                                # after
- bg: '#ffffff'                         - font: Calibri
  font: Calibri                           t: 'Following the '
  t: 'Following the '
```

### Step 2 — Glue split runs back together

Google fragments text arbitrarily (an edit history artifact):
`"E"` + `"ngaging with the PC…"` as two runs with identical style.
After Step 1 removed the noise, "identical style" is easy to see —
merge them:

```yaml
# before                                # after
- {font: Calibri, t: 'Following the '}  - {font: Calibri, t: Following the co-chair timetable}
- {font: Calibri, t: co-chair timetable}
```

This must run *after* Step 1: two runs may differ only in attributes
Step 1 deletes.

### Step 3 — Build the style table

Count which style combinations recur. Each combo that *earns its
place* gets a short name in a `styles:` table; runs reference it:

```yaml
styles:
  s1: {font: Calibri, bg: '#ffffff'}
body:
- p: {s: s1, t: Some styled text}
```

"Earns its place" is plain arithmetic, not a magic threshold: put a
combo in the table **iff the characters saved by all its references
exceed the characters the table entry costs**. A fat style pays off
at 2 uses; `{b: true}` never does (inline `b: true` is already
shorter than a reference).

Rules of the table, learned from formats that got this wrong:

- **Flat.** No style-inherits-from-style. (Word's `basedOn` chains
  with toggle logic are the cautionary tale.)
- **Overrides allowed, simple semantics**: `{s: s1, b: true}` means
  "s1, plus bold". Plain merge, last value wins.
- **Editable like CSS classes**: change `s1`'s color and every
  referencing run changes — that's a feature, especially for LLM
  edits. Unknown reference (`s: s9`) is a validation error naming the
  exact spot.

### Step 4 — Promote the most common style to "default"

If nearly every run is `s1`, saying so once (`default: s1`) makes all
those references disappear. That is how a plain bullet collapses to
just its text — its entire style is the document default. Same
arithmetic as Step 3: the most frequent combo costs zero per use as
the default, so it always wins.

### Step 5 — Always produce the same bytes

The three-way diff compares serialized files; if the same content can
print two different ways, diffs show phantom changes. So the output
is pinned down completely: fixed key order per node type (text last,
style keys first, as in the examples), fixed quoting rules, style
names numbered by frequency (`s1` = most common), appendix refs
numbered in document order. Test: parse then re-serialize any file →
byte-identical.

## Why a Fixed Order Instead of "Apply Rules Until Done"

Because the steps feed each other: Step 2 only works after Step 1
(noise hides equality), Step 3 only works after Step 2 (fragmented
runs would inflate the counts), Step 4 is Step 3's bookkeeping. A
fixed pipeline with one test per step is simple and debuggable. We
looked at the heavier machinery from compiler research (rule systems
with proven order-independence, e-graphs that try all orders and pick
the cheapest) and rejected it: our steps don't compete with each
other, so there is nothing for that machinery to decide.

## What Does NOT Change

- **Faithfulness.** Every step has an inverse; `expand(compress(doc))
  == doc` stays property-tested per step and for the whole pipeline.
  Anything unhandled still goes verbatim to the appendix (one
  section, grouped by kind, at the very end).
- **The schema version.** `styles:`, `default:`, `s:` are additive
  optional keys — a file without them is still valid doc-tree/v1.
- **The edit contract.** Appendix stays opaque and immutable; the
  style table is editable but validated.

## Ticket Mapping

- **gax-cvi.12** — Steps 1–2 + the pipeline scaffolding (one inverse
  + test per step).
- **gax-cvi.13** — Steps 3–4 (style table + default), with the
  savings arithmetic above.
- **gax-cvi.14** — Step 5 (canonical output + byte-identity tests).
- Benchmark for all three: the SREcon Co-chair doc, target ratio
  ≤ 0.35 with round-trip identity green.

## References (background only)

The design borrows from: staged transformation pipelines (nanopass
compilers; Stratego), dictionary/grammar compression for the "name
repeated things" step (Re-Pair, Sequitur — including its "delete
table entries used once" cleanup), minimum-description-length for the
"does it earn its place" arithmetic, canonical XML for byte-stable
output, and DOCX/CSS precedent for the style-table semantics (flat +
overrides, no inheritance). Rejected as overkill: e-graphs/equality
saturation, confluence completion, bit-level tree encodings.
