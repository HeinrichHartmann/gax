# ADR 030: Markdown Strategy — Unified IR via Mistune

## Status

Proposed

## Context

gax converts between Google Docs and local markdown files. Today this conversion uses three separate pipelines with different intermediate representations:

**Pull** (remote → local): The Drive API markdown export (`files().export(mimeType="text/markdown")`) returns all tabs concatenated as one markdown string. A regex-based splitter (`split_doc_by_tabs`) scans for H1 headers matching known tab titles to cut it into per-tab content. This is fragile — an H1 inside a code block or a tab title collision can cause false splits. The raw export also requires normalization (unescape `\->#~`, convert `* ` bullets to `- `, strip h6 italic markers, etc.).

**Push full-replace** (`md2docs.py`): Parses markdown via mistune into a custom `Node` AST (`Heading`, `Paragraph`, `ListItem`, `Table`, `CodeBlock` with `Text` spans), then generates Docs API `batchUpdate` requests. Destroys all non-markdown formatting on every push.

**Push diff-based** (`diff_push.py`, experimental `--patch`): Pulls the remote state via *both* the Drive API markdown export and `documents().get()` JSON. Parses the pulled markdown into the `Node` AST, classifies the doc JSON into `DocElement` types, then runs a fuzzy alignment step (`align()`) to pair AST nodes with doc elements by walking both structures in parallel. This alignment has an unsafe fallback path that can target wrong indices when the Drive API export doesn't match the JSON structure. See ADR 027.

**Problems with the current architecture:**

1. **Two sources of truth on pull.** The Drive API markdown export and the Docs API JSON represent the same document differently. The Drive export merges consecutive paragraphs, flattens nested lists, and renders code blocks as blockquotes. These discrepancies force the fuzzy alignment step in diff-push, which is the most fragile part of the pipeline.

2. **Tab splitting is regex-based.** `split_doc_by_tabs` does line-level `startswith("# ")` matching. This can false-match on H1 headers inside code blocks, blockquotes, or content that happens to match another tab's title. Nested tabs (issue #34) make this worse — the Drive API uses H1 for all tabs regardless of nesting depth, so there is no structural signal for the hierarchy.

3. **Three AST representations.** Mistune dict tokens, custom `Node` dataclasses, and `DocElement` structs serve overlapping purposes. The conversion between them introduces bugs and maintenance burden.

4. **Normalization is defensive guesswork.** The Drive API export escapes characters, wraps h6 in italic markers, and uses `* ` for bullets. We reverse these with regexes. When Google changes their export format, our normalizations break silently.

## Decision

Replace the Drive API markdown export with a custom serialization pipeline that reads from the Docs API JSON directly:

```
Google Docs JSON  ←→  Block/Span tree  ←→  Mistune AST tokens  ←→  Markdown text
```

### The IR: Block and Span

A `Block` is a block-level document element. A `Span` is an inline text segment with formatting flags.

```python
@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    strikethrough: bool = False
    url: str | None = None

@dataclass
class Block:
    doc_range: tuple[int, int] | None = None  # Google Docs startIndex/endIndex

class Heading(Block):    level: int;  spans: list[Span]
class Paragraph(Block):  spans: list[Span]
class ListItem(Block):   spans: list[Span]; ordered: bool; depth: int
class CodeBlock(Block):  code: str; language: str
class Table(Block):      rows: list[list[list[Span]]]
```

Each `Block` carries an optional `doc_range` — the Google Docs index range it occupies. This field is populated when loaded from the Docs API JSON and is `None` when parsed from local markdown.

### Four conversions

**1. `from_doc_json(body_content) → list[Block]`**
Walk the `body.content[]` array from `documents().get()`. Classify each element by `paragraphStyle.namedStyleType` (heading vs normal), `paragraph.bullet` (list item), or `table` presence. Extract inline formatting from `textRun.textStyle` (bold, italic, strikethrough, link). Set `doc_range` from `startIndex`/`endIndex`.

This replaces `diff_push.walk_doc_body()`, `classify_doc_element()`, and the Drive API markdown export.

**2. `from_mistune_tokens(tokens) → list[Block]`**
Convert mistune AST tokens to our Block/Span types. This is what `md2docs.parse_markdown()` already does, refactored to produce Block types instead of Node types.

**3. `to_tokens(blocks) → list[dict]`**
Convert our tree back to mistune AST tokens. Each Span wraps its text in the appropriate mistune token nesting (strong > emphasis > text, etc.). These tokens can be rendered to markdown by mistune's `MarkdownRenderer`.

**4. `to_docs_requests(blocks) → list[dict]`**
Generate Docs API `batchUpdate` requests from the tree. This is what `md2docs.generate_requests()` already does, refactored to consume Block types.

### Markdown serialization via Mistune MarkdownRenderer

Mistune v3 ships a `MarkdownRenderer` that serializes AST tokens back to markdown text. It round-trips faithfully with two adjustments for Google Docs conventions:

- **Ordered lists**: Always emit `1.` for every item (Google renumbers on import, always exports `1.`).
- **Table separators**: Use `:----` (4 dashes) to match Google's export convention.

The `MarkdownRenderer` does not natively support `table` or `strikethrough` tokens (plugin renderers only auto-register for `NAME == "html"`). We register custom render functions via `renderer.register()`.

Validated against the e2e rich formatting fixture (`tests/fixtures/e2e_rich_formatting.md`): perfect round-trip — zero diff on all 182 lines covering headings, bold/italic/strikethrough, ordered/unordered lists, 7 tables with emoji and inline formatting, hyperlinks, and special characters.

### Tab handling without splitting

With `includeTabsContent=True`, the Docs API returns each tab's `body.content[]` separately, with its own index space (starting from 1). Nested tabs are available via `childTabs[]` arrays on each tab object.

Pull iterates the tab tree and calls `from_doc_json()` on each tab's body independently. No concatenation, no splitting, no H1 matching. Nested tab support (issue #34) becomes a recursive traversal of `childTabs`, producing a `TabNode` tree:

```python
@dataclass
class TabNode:
    tab_id: str
    title: str
    depth: int = 0
    children: list[TabNode]
    blocks: list[Block]
```

### Diff-based push without alignment

The current diff-push (ADR 027) requires a fuzzy alignment step to pair markdown AST nodes with doc JSON elements. This alignment is fragile because the Drive API markdown export and the doc JSON represent the same document differently.

With the new approach, the remote state is loaded via `from_doc_json()`, producing Block nodes that already carry `doc_range`. The local state is loaded via mistune + `from_mistune_tokens()`, producing Block nodes without `doc_range`. The diff operates on two lists of the same types:

```
remote = from_doc_json(doc_body)        # each Block has doc_range
local  = from_mistune_tokens(parse(md)) # each Block has doc_range=None

ops = diff(remote, local)               # SequenceMatcher on block keys
mutations = translate(ops)              # update ops inherit doc_range from remote side
```

No alignment step. No unsafe fallback path. Each diff operation inherits the index information it needs from the remote tree.

## Consequences

**Positive:**

- One IR for all directions — Block/Span tree is the single intermediate representation
- Tab splitting disappears — each tab is read independently from the Docs API JSON
- Fuzzy alignment disappears — `doc_range` comes from construction, not from post-hoc matching
- No Drive API markdown export dependency — we own the serialization, immune to Google changing their export format
- Nested tab support becomes trivial — recursive traversal of `childTabs`
- Markdown serialization is correct by construction — mistune's `MarkdownRenderer` handles the syntax, we just configure it for Google's conventions

**Negative:**

- `from_doc_json()` must handle all Google Docs element types we support. The Drive API handled this conversion for us. This is new code to write and maintain, but the element types are well-documented and the conversion is straightforward for the types we support (headings, paragraphs, lists, tables, inline formatting).
- Elements we don't yet support (images, footnotes, equations, drawings) will be silently dropped until we add handlers. The Drive API export also handled these poorly, so the practical gap is small.
- One API call (`documents().get` with `includeTabsContent=True`) replaces the Drive API export. This returns more data (full tab content as JSON, not just markdown) but eliminates the second API call that diff-push currently makes.
- The `to_tokens()` path (Block → mistune AST → MarkdownRenderer → markdown) must stay in sync with `from_mistune_tokens()` (markdown → mistune AST → Block). We mitigate this by running round-trip tests against the e2e fixture.

**Migration:**

- `native_md.export_doc_markdown()` — retired (replaced by `from_doc_json()` + MarkdownRenderer)
- `native_md.split_doc_by_tabs()` — retired (tabs read independently)
- `native_md.get_doc_tabs()` — kept but returns `TabNode` tree instead of flat list of dicts
- `md2docs.parse_markdown()` — refactored to produce Block types (`from_mistune_tokens()`)
- `md2docs.generate_requests()` — refactored to consume Block types (`to_docs_requests()`)
- `diff_push.walk_doc_body()` + `classify_doc_element()` — folded into `from_doc_json()`
- `diff_push.align()` — retired
- `diff_push.ast_diff()` — kept, operates on Block lists instead of Node lists
- `diff_push.diff_to_mutations()` — simplified, no alignment parameter

## References

- ADR 023: Markdown-to-Google-Docs Conversion and Testing Strategy
- ADR 027: Diff-Based Document Push
- Issue #34: Nested Tab support (Google Docs)
- `tests/fixtures/e2e_rich_formatting.md`: Round-trip test fixture (validated with MarkdownRenderer)
- Mistune v3 documentation: `MarkdownRenderer`, AST token format, plugin system
