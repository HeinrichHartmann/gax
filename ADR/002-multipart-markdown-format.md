# ADR 002: Multipart YAML-Markdown Format

## Status

Accepted

## Context

We need a file format that can represent documents with multiple sections (e.g., Google Docs with tabs), while remaining compatible with standard YAML frontmatter + markdown files.

### Design Constraints

1. **Backwards compatible**: A standard YAML+markdown file is a valid 1-section multipart document
2. **Concatenation**: Joining N YAML+markdown files yields a valid N-section multipart document (in the common case)
3. **Splittable**: Splitting a multipart yields N standalone YAML+markdown files (each section is self-contained)
4. **Fits in memory**: Sections are assumed to fit comfortably in memory (not a streaming/big-data format)
5. **Human readable**: Editable in any text editor

## Decision

### Basic Structure

A multipart document is a sequence of sections. Each section has:
- A YAML header (between `---` delimiters)
- A markdown body (until next `---` on its own line, or EOF)

```
---
<yaml header>
---
<markdown body>
---
<yaml header>
---
<markdown body>
```

### Single-Section Case (Standard Markdown)

A normal YAML frontmatter + markdown file is a valid 1-section multipart document:

```
---
title: My Document
author: Jane Doe
---
# Introduction

This is a standard markdown file with YAML frontmatter.
It is also a valid 1-section multipart document.
```

### Multi-Section Case

Multiple sections are concatenated. Each section repeats document-level metadata so it can stand alone when split:

```
---
title: My Document
source: https://docs.google.com/document/d/xxx/edit
section: 1
section_title: Overview
---
# Overview

Content of first section.
---
title: My Document
source: https://docs.google.com/document/d/xxx/edit
section: 2
section_title: Details
---
# Details

Content of second section.
```

**Split operation:** Extract each section as a standalone file.
**Join operation:** Concatenate files to create multipart.

### The Quoting Problem

If markdown content contains `---` on its own line, the parser cannot distinguish it from a section boundary:

```markdown
# My Document

Some text...

---

More text after horizontal rule...
```

### Solution: content-length

When content may contain `---`, add a `content-length` header specifying the byte size of the body:

```
---
section: 1
title: Overview
content-length: 142
---
# Overview

This content safely contains a horizontal rule:

---

Parser reads exactly 142 bytes, ignoring the above delimiter.
---
section: 2
title: Next Section
---
...
```

**Parsing with content-length:**
1. Parse header until `---`
2. If `content-length` present: read exactly that many bytes
3. If absent: scan for next `---` on its own line (or EOF)

**Writing with content-length:**
1. Encode body as UTF-8 bytes
2. Set `content-length` to byte count
3. Write header, `---\n`, then body bytes

### Header Fields

No required fields. Common conventions:

**Document-level (repeated in each section for splittability):**

| Field | Description |
|-------|-------------|
| `title` | Document title |
| `source` | Origin URL (for synced documents) |
| `time` | ISO 8601 timestamp of last sync |

**Section-level:**

| Field | Description |
|-------|-------------|
| `section` | Section number (1-based) |
| `section_title` | Title of this section/tab |
| `content-length` | Body size in bytes (optional, for quoting) |

## Parsing Algorithm

```python
def parse_multipart(text: str) -> list[Section]:
    sections = []
    pos = 0

    while pos < len(text):
        # Find header start
        if text[pos:pos+4] != '---\n':
            break
        pos += 4

        # Parse header until ---
        header_end = text.find('\n---\n', pos)
        header = parse_yaml(text[pos:header_end])
        pos = header_end + 5  # skip \n---\n

        # Read body
        if 'content-length' in header:
            length = header['content-length']
            body = text[pos:pos+length]
            pos += length
        else:
            # Scan for next section or EOF
            next_section = text.find('\n---\n', pos)
            if next_section == -1:
                body = text[pos:]
                pos = len(text)
            else:
                body = text[pos:next_section+1]  # include trailing \n
                pos = next_section + 1

        sections.append(Section(header, body))

    return sections
```

## Operations

### Split

Extract each section as a standalone file:

```bash
gax multipart split document.doc.gax
# Creates: document-1.md, document-2.md, ...
```

Each output file is a valid YAML+markdown file with all metadata intact.

### Join

Concatenate standalone files into a multipart:

```bash
gax multipart join section-*.md > document.doc.gax
# Or simply: cat section-1.md section-2.md > document.doc.gax
```

## File Extensions

| Extension | Use |
|-----------|-----|
| `.doc.gax` | Google Docs (multipart with tabs) |
| `.sheet.gax` | Google Sheets (single section) |
| `.md` | Standard markdown (1-section, compatible) |

## Consequences

**Positive:**
- Standard markdown files work unchanged
- Simple concatenation creates valid multipart
- `content-length` solves quoting unambiguously
- Human-readable and editable
- No special markers or escaping needed in common case

**Negative:**
- Manual edits with `content-length` require recalculating byte count
- Parser slightly more complex than delimiter-only approach

## References

- YAML frontmatter convention (Jekyll, Hugo, etc.)
- HTTP multipart (RFC 2046) - inspiration for content-length approach
