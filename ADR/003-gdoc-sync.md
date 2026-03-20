# ADR 003: Google Docs Sync

## Status

Proposed

## Context

gax currently supports Google Sheets sync via `gax gsheet pull/push/init`. Google Docs is the next logical addition, enabling the same local-first workflow for documents.

Key differences from Sheets:
- Docs have **tabs** (multiple sections within one document)
- Docs are **rich text**, not tabular data
- Push is harder (requires mapping markdown back to Docs API structures)

## Decision

### Commands

Following the gsheet pattern:

```
gax gdoc init <url>              # Create new .doc.gax file from URL
gax gdoc pull <file>             # Re-fetch from source URL in frontmatter
gax gdoc push <file>             # Future: upload changes (not in v1)
```

### File Format

Uses the multipart YAML-markdown format (ADR 002). Each tab becomes a self-contained section with repeated document metadata:

**Single-tab document:**

```
---
title: Design Spec
source: https://docs.google.com/document/d/abc123/edit
time: 2026-03-20T10:00:00Z
section: 1
section_title: Design Spec
---
# Introduction

This document describes...
```

**Multi-tab document:**

```
---
title: Project Plan
source: https://docs.google.com/document/d/xyz789/edit
time: 2026-03-20T10:00:00Z
section: 1
section_title: Overview
---
# Overview

High-level project description.

---
title: Project Plan
source: https://docs.google.com/document/d/xyz789/edit
time: 2026-03-20T10:00:00Z
section: 2
section_title: Timeline
---
# Timeline

| Phase | Date |
|-------|------|
| Alpha | Q1   |
| Beta  | Q2   |

---
title: Project Plan
source: https://docs.google.com/document/d/xyz789/edit
time: 2026-03-20T10:00:00Z
section: 3
section_title: Risks
content-length: 156
---
# Risks

This section contains a horizontal rule for emphasis:

---

The content-length field handles this safely.
```

**Key properties:**
- Each section is self-contained (can be split out as standalone file)
- Document metadata (`title`, `source`, `time`) repeated in every section
- `content-length` added only when section content contains `---`
- Single-tab docs look like standard YAML+markdown files

### Implementation

**Module structure (v1 - single file):**
```
gax/
  gdoc.py            # All gdoc logic + CLI group
```

**Core functions:**

```python
def pull_doc(document_id: str, source_url: str) -> list[Section]:
    """Fetch doc from API and return list of sections."""

def format_multipart(sections: list[Section]) -> str:
    """Assemble sections into multipart markdown string."""

def parse_multipart(content: str) -> list[Section]:
    """Parse .doc.gax file into sections."""

def extract_doc_id(url: str) -> str:
    """Extract document ID from Google Docs URL."""
```

**Auth scopes:** Add to `auth.py`:
```python
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",  # NEW
]
```

### Conversion Strategy

Google Docs API returns a structured document with paragraphs, headings, tables, etc.

**v1 approach:**
1. Fetch document with `includeTabsContent=True` via Docs API
2. For each tab, convert body to markdown
3. Assemble as multipart with repeated metadata

**Heading mapping:**

| Google Docs Style | Markdown |
|-------------------|----------|
| HEADING_1 | `#` |
| HEADING_2 | `##` |
| HEADING_3 | `###` |
| HEADING_4 | `####` |
| NORMAL_TEXT | plain text |

**Limitations (v1):**
- Tables rendered as `*(table omitted)*` or simplified markdown
- Images stripped or placeholder text
- Complex formatting (columns, etc.) simplified

### Quoting Strategy

When writing sections:
1. Check if content contains `\n---\n`
2. If yes, compute `content-length` and add to header
3. If no, omit `content-length` (cleaner output)

This keeps simple documents clean while handling edge cases safely.

### Push (Future)

`gax gdoc push` is significantly harder than pull:
- Need to map markdown back to Docs API structures
- Handle insertions, deletions, formatting
- Conflict resolution

**Recommendation:** Defer push to v2. For v1, treat gdoc files as read-only local copies.

## Consequences

**Positive:**
- Consistent CLI surface with gsheet (`init`, `pull`, `push`)
- Local-first workflow for docs
- Tab structure preserved as multipart sections
- Self-describing files enable easy re-sync
- Each tab can be split out as standalone file
- Single-tab docs are valid standard markdown

**Negative:**
- Push not available in v1
- Some formatting loss in conversion
- Additional OAuth scope required (users may need to re-auth)
- Repeated metadata adds some verbosity for multi-tab docs

## Dependencies

- `markdownify` - HTML to markdown conversion
- `google-api-python-client` - already used for Sheets

## References

- ADR 002: Multipart YAML-Markdown Format
