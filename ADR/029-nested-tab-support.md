# ADR 029: Nested Tab Support (Google Docs)

**Status:** Proposed
**Date:** 2026-04-17

## Context

Google Docs supports nested tabs (child tabs under parent tabs). The Docs API exposes these via `childTabs` arrays on each tab object. Currently, `get_doc_tabs()` and `get_tabs_list()` only iterate top-level tabs, silently dropping children.

The Drive API markdown export uses H1 for top-level tabs, H2 for children, H3 for grandchildren, etc.

Per ADR 026, `gax clone` produces a single file (first tab) and `gax checkout` produces a directory of tab files. Nested tabs extend the checkout model naturally: child tabs become subdirectories.

## Decision

### Tab enumeration

Replace the flat loop in `get_doc_tabs()` and `get_tabs_list()` with a recursive traversal of `childTabs`. Each tab gets a `depth` field and a `path` field (slash-separated ancestor titles).

### Checkout directory structure

`gax doc checkout` maps the tab tree to the filesystem:

```
Project.doc.gax.md.d/
  .gax.yaml
  Overview.tab.gax.md              # top-level tab (no children)
  Design/                           # top-level tab with children
    Design.tab.gax.md               # the parent tab's own content
    Frontend.tab.gax.md             # child tab
    Backend/                         # child tab with its own children
      Backend.tab.gax.md
      API.tab.gax.md                 # grandchild tab
```

Rules:
- A tab with no children is a file in its parent directory.
- A tab with children becomes a subdirectory. Its own content lives as `Name.tab.gax.md` inside that subdirectory.
- Depth is unlimited (follows API structure).

### Clone warning

`gax doc clone` already warns when there are multiple tabs. When nested tabs are present, add a specific warning:

```
⚠ Document has nested tabs. Use 'gax doc checkout' for full structure.
```

### Markdown splitting

`split_doc_by_tabs()` currently only matches H1 headers as tab boundaries. Extended to match H1..H6 based on tab depth. Tab titles are matched with their depth-appropriate header level.

### .gax.yaml

The metadata file stores the full tab tree so push can resolve filesystem paths back to `tabId`:

```yaml
type: gax/doc-checkout
document_id: abc123
url: https://docs.google.com/document/d/abc123/edit
title: Project
tabs:
  - id: t.1
    title: Overview
    path: Overview.tab.gax.md
  - id: t.2
    title: Design
    path: Design/Design.tab.gax.md
    children:
      - id: t.3
        title: Frontend
        path: Design/Frontend.tab.gax.md
      - id: t.4
        title: Backend
        path: Design/Backend/Backend.tab.gax.md
        children:
          - id: t.5
            title: API
            path: Design/Backend/API.tab.gax.md
```

### tab list output

`gax doc tab list` indents nested tabs:

```
0  t.1  Overview
1  t.2  Design
2  t.3    Frontend
3  t.4    Backend
4  t.5      API
```

## Implementation

### Changes

1. **`native_md.get_doc_tabs()`** -- recurse `childTabs`, return flat list with `id`, `title`, `depth`, `path` fields
2. **`gdoc.get_tabs_list()`** -- same recursive approach
3. **`native_md.split_doc_by_tabs()`** -- match headers at depth-appropriate level (H1 for depth 0, H2 for depth 1, etc.)
4. **`gdoc.checkout()`** -- create subdirectories for tabs with children; write tab files at correct nesting level
5. **`gdoc.clone()`** -- add nested tab warning
6. **`gdoc.tab_list()`** -- indent output by depth
7. **`.gax.yaml`** -- include tab tree with paths

### What stays the same

- `.tab.gax.md` file format (YAML header + markdown body)
- Push logic (resolves to `tabId`)
- Single-tab clone
- Diff-push / patch mode

## Consequences

### Positive

- Documents with nested tabs are fully supported
- Directory structure mirrors document structure intuitively
- No new commands or flags needed

### Negative

- Deeper nesting creates deeper directory trees
- Tab titles containing `/` need sanitization (already handled by existing `re.sub`)
