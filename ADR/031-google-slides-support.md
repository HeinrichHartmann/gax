# ADR 031: Google Slides Support

## Status

Proposed

## Context

gax supports Google Docs, Sheets, Forms, Calendar, Gmail, Contacts, and Drive
files. Google Slides is a notable gap — presentations are a common team artifact
stored in Drive, and the existing Drive folder checkout (ADR 028) already
encounters Slides files but has no native handler for them.

The Google Slides API v1 provides comprehensive read/write access.
Authentication infrastructure is already in place — the existing `drive` scope
provides basic access, though a dedicated `presentations` scope would be more
precise.

The key challenge is serialization format. Slides are inherently spatial
(shapes, positions, transforms) rather than linear like Docs. A pure-markdown
representation loses layout fidelity. A pure-JSON representation loses human
readability. The right trade-off depends on the use case: LLM agents primarily
need text content; designers need spatial fidelity.

## Decision

### Resource classes

Follow the dual-class pattern (Tab/Doc, SheetTab/Sheet):

- **`Slide(Resource)`** — single slide, stored as `.slides.gax.md`.
  The editing unit.
- **`Presentation`** — collection manager (not a Resource subclass), handles
  `.slides.gax.md.d/` checkout directories. Like Doc and Sheet.

No standalone `clone` command. Presentations are always multi-slide, so the
primary entry point is `checkout` which creates a directory. Individual
`.slides.gax.md` files within the checkout are the editing units for
pull/push.

### Serialization formats

Two formats: **markdown** (read-only, human/LLM friendly) and **JSON**
(read-write, full fidelity).

#### Markdown format (`.slides.gax.md`)

```yaml
---
type:        gax/slides
title:       "Q4 Review"
source:      https://docs.google.com/presentation/d/PRESENTATION_ID/edit
pulled:      2026-04-19T10:00:00Z
slide_index: 0
slide_id:    g1234abcd
layout:      TITLE_AND_BODY
---

# Q4 Revenue Summary

- Revenue up 15% YoY
- New markets: APAC, LATAM
- Churn reduced to 3.2%
```

Speaker notes go in a fenced block at the end of the slide:

    ```notes
    Remember to mention the APAC expansion timeline.
    ```

Markdown is **pull-only**. Push on a markdown checkout prints a warning:

    ⚠ Push is not supported for markdown format.
    Re-checkout with --format json to enable push:
      gax slides checkout <url> --format json

This avoids the lossy round-trip problem where spatial layout information
is destroyed by markdown serialization.

#### JSON format (`.slides.gax.md`)

```yaml
---
type:        gax/slides
title:       "Q4 Review"
source:      https://docs.google.com/presentation/d/PRESENTATION_ID/edit
format:      json
pulled:      2026-04-19T10:00:00Z
slide_index: 0
slide_id:    g1234abcd
---

{
  "objectId": "g1234abcd",
  "pageElements": [...],
  "slideProperties": {...},
  "notesPage": {...}
}
```

JSON format preserves full slide structure and supports push.

### Checkout directory structure

```
Q4_Review.slides.gax.md.d/
├── .gax.yaml
├── 00_Title_Slide.slides.gax.md
├── 01_Q4_Revenue_Summary.slides.gax.md
├── 02_Market_Expansion.slides.gax.md
└── 03_Next_Steps.slides.gax.md
```

### .gax.yaml format

```yaml
type:            gax/slides-checkout
presentation_id: PRESENTATION_ID
url:             https://docs.google.com/presentation/d/PRESENTATION_ID/edit
title:           Q4 Review
format:          md
checked_out:     2026-04-19T10:00:00Z
```

### Pull pipeline (remote → local)

1. Fetch presentation via `presentations.get(presentationId)` — returns full
   JSON with all slides.
2. For each slide, extract content depending on format:

   **Markdown mode** — extract text from page elements:
   - `Shape` with `textContent` → extract text runs, map formatting to markdown
   - `Table` → markdown table
   - `Image` → `![alt](contentUrl)` reference (no download)
   - Other elements (lines, charts, diagrams) → skip
   - Title extraction: first `TITLE` or `CENTERED_TITLE` placeholder, or
     first text element
   - Speaker notes: extract from `slide.notesPage.pageElements`

   **JSON mode** — write the raw slide JSON as-is.

3. Write per-slide `.slides.gax.md` files with zero-padded index prefix
   for ordering.

### Push pipeline (local → remote)

Push is **JSON-format only**. Markdown checkouts refuse push with a warning.

For JSON format:

1. Parse each slide's JSON body
2. Build `batchUpdate` requests from the delta between pulled and current JSON
3. Apply via `presentations.batchUpdate`
4. Preserve slide ordering — slide index from filename prefix

Diff-based push is deferred. Full-replace of the JSON page elements is the
initial approach.

### Text extraction from page elements

Slides page elements are nested:
`Presentation → Slide → PageElement → Shape → TextContent → TextRun`.

Text extraction walks this tree:

```python
def _extract_slide_text(slide: dict) -> list[Block]:
    """Extract text blocks from a slide's page elements."""
    blocks = []
    for element in slide.get("pageElements", []):
        shape = element.get("shape", {})
        text_content = shape.get("text", {})
        if not text_content:
            continue
        placeholder_type = shape.get("placeholder", {}).get("type", "")
        text_runs = text_content.get("textElements", [])
        # Map placeholder type → markdown heading level
        # TITLE/CENTERED_TITLE → H1, SUBTITLE → H2, BODY → paragraphs
        ...
```

### Operations

**Checkout** (`gax slides checkout URL [-o DIR] [--format md|json]`):
All slides to `.slides.gax.md.d/` directory. Default format: `md`.

**Pull** (`gax pull <path>`):
Refresh from remote. Works for both single slide file and checkout directory.

**Push** (`gax push <path>`):
Push local changes. JSON format only. Markdown checkouts print a warning
and exit.

### CLI commands

```
gax slides checkout <url> [-o DIR] [--format md|json]
gax slides pull <path>
gax slides push <path>
```

### OAuth scope

Add `https://www.googleapis.com/auth/presentations` to `SCOPES` in `auth.py`.
The existing `drive` scope provides some access, but the dedicated scope is
needed for `batchUpdate` write operations.

### Drive folder integration

Register `application/vnd.google-apps.presentation` → `"slides"` in
`WORKSPACE_MIME_TYPES` in `gdrive.py`, so Drive folder checkout dispatches
Slides files to the native resource.

### Scope boundaries — what this ADR does NOT cover

- **Image download/upload** — images stay as URL references. A future ADR can
  add `--with-images` for local caching.
- **Layout/theme changes** — preserved as-is, not editable.
- **Animation and transitions** — preserved, not exposed.

### Unresolved: slide creation and deletion

Push currently only modifies existing slides. Adding or removing slides is
deferred but has a natural path in JSON mode:

- New file (no `slide_id` in frontmatter) → `createSlide` API call. Requires
  choosing a layout — could require a `layout` field in frontmatter, default
  to BLANK, or duplicate an existing slide.
- Deleted file (present in remote, no local file) → `deleteObject` API call.
- Reordered files (changed index prefix) → `updateSlidesPosition` API call.

This is a JSON-only concern since markdown checkouts don't support push.
Needs its own design pass before implementation.

## Consequences

**Positive:**

- Completes the Google Workspace coverage (Docs, Sheets, Forms, Slides)
- LLM agents can read presentation text content through local markdown files
- JSON format enables full-fidelity round-tripping for programmatic edits
- Drive folder checkout handles Slides files natively instead of skipping them
- No clone command — simpler surface area, one entry point (`checkout`)

**Negative:**

- Markdown format is read-only, which may surprise users expecting parity with
  Docs where markdown push works
- JSON push is full-replace initially, so concurrent remote edits could be lost
- Adds a new OAuth scope, requiring re-authentication for existing users

**Neutral:**

- JSON format preserves full fidelity but is not human-editable
- Speaker notes are a first-class concept (unlike Docs where they don't exist),
  adding a new serialization concern

## References

- [Google Slides API v1](https://developers.google.com/slides/api/reference/rest)
- ADR 026: Clone/File/Checkout directory pattern
- ADR 028: Drive folder sync (WORKSPACE_MIME_TYPES integration point)
- ADR 030: Markdown strategy / Block-Span IR (potential reuse for text extraction)
- GitHub issue #33
