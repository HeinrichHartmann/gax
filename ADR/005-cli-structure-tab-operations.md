# ADR 005: CLI Structure and Tab-Level Operations

## Status

Proposed

## Context

The initial gax CLI (ADR 001, 003, 004) established separate commands for Sheets, Docs, and Mail. As the tool matured, several issues emerged:

1. **Inconsistent output handling** - `sheet clone` wrote to stdout while `doc clone` wrote to file
2. **No push for docs** - ADR 003 deferred push to "v2" due to complexity
3. **Granularity mismatch** - Full-document push is risky (destroys formatting), but tab-level push is safe

This ADR establishes the unified CLI structure with tab-level operations.

## Decision

### CLI Structure

```
gax
├── auth
│   ├── login
│   ├── logout
│   └── status
│
├── sheet
│   ├── clone <url>              # All tabs → .sheet.gax (multipart)
│   ├── pull <file>              # Pull all tabs
│   └── tab
│       ├── list <url>           # List tabs (TSV)
│       ├── clone <url> <tab>    # Single tab → .sheet.gax
│       ├── pull <file>          # Pull single tab
│       └── push <file>          # Push single tab
│
├── doc
│   ├── clone <url>              # All tabs → .doc.gax (multipart)
│   ├── pull <file>              # Pull all tabs
│   └── tab
│       ├── list <url>           # List tabs (TSV)
│       ├── import <url> <file>  # Create NEW tab from markdown
│       ├── clone <url> <tab>    # Clone existing tab → .tab.gax
│       ├── pull <file>          # Pull single tab
│       ├── diff <file>          # Show local vs remote diff
│       └── push <file>          # Push single tab (diff + y/n prompt)
│
└── mail
    ├── clone <url|query>        # Thread(s) → .mail.gax
    ├── pull <file|dir>          # Pull updates
    ├── labels                   # List labels (TSV)
    └── search <query>           # Search threads (TSV)
```

### Design Principles

#### 1. Symmetry between sheet and doc

Both services follow the same pattern:
- `clone/pull` at document level (all tabs, multipart format)
- `tab clone/pull/push` at tab level (single tab)

#### 2. Push only at tab level

| Level | Push allowed | Rationale |
|-------|--------------|-----------|
| Full document | No | Too risky - could destroy formatting, comments, images |
| Single tab | Yes | Scoped risk - only affects one tab |

#### 3. Safety mechanisms for doc push

Doc tabs contain rich text that loses formatting when converted to markdown. Push requires extra safety:

| Command | Behavior |
|---------|----------|
| `doc tab diff` | Show diff, no changes (read-only) |
| `doc tab push` | Show diff, prompt y/n, then apply |
| `doc tab push -y` | Apply without prompt (for scripts) |

#### 4. Import vs Clone for docs

| Command | Creates new tab | Tracks existing |
|---------|-----------------|-----------------|
| `doc tab import` | Yes | Creates .tab.gax for future push |
| `doc tab clone` | No | Clones existing tab to .tab.gax |

Re-importing the same file creates a NEW tab (prompts to overwrite .tab.gax).

### File Extensions

| Extension | Description | Round-trip safe |
|-----------|-------------|-----------------|
| `.sheet.gax` | Sheet data (single or multipart) | Yes |
| `.doc.gax` | Full document (all tabs, multipart) | Read-only |
| `.tab.gax` | Single doc tab | Yes (with diff+prompt) |
| `.mail.gax` | Email thread | Read-only |

### Tab File Format (.tab.gax)

Single doc tab with tracking metadata:

```yaml
---
title: Project Notes
source: https://docs.google.com/document/d/abc123/edit
tab_id: t.xyz789
tab_title: AI Summary
time: 2026-03-20T10:00:00Z
---
# AI Summary

Content managed by gax...
```

The `tab_id` enables push to update the correct tab.

### Multipart Sheet Format

Full sheet clone produces multipart format (like docs):

```yaml
---
title: Q1 Report
source: https://docs.google.com/spreadsheets/d/abc123
section: 1
tab: Revenue
format: csv
---
Month,Amount
Jan,10000
Feb,12000

---
title: Q1 Report
source: https://docs.google.com/spreadsheets/d/abc123
section: 2
tab: Expenses
format: csv
---
Month,Amount
Jan,8000
Feb,9000
```

## Consequences

### Positive

- **Consistent UX** - Same verbs (clone/pull/push) across services
- **Safe push** - Tab-level granularity limits blast radius
- **Clear mental model** - Full doc is read-only, tabs are read-write
- **Scriptable** - `-y` flag enables automation
- **Recoverable** - Diff preview before destructive operations

### Negative

- **More commands** - `tab` subcommand adds depth to CLI
- **Two file types for docs** - `.doc.gax` (full) vs `.tab.gax` (single)
- **Formatting loss on doc push** - Rich text → markdown → plain text

### Migration

Existing `.sheet.gax` files (single tab) remain compatible. The new `sheet clone` (full document) produces multipart format with multiple sections.

## Supersedes

- ADR 001: Updates CLI structure for sheets (adds `tab` subcommand)
- ADR 003: Implements deferred "v2" push via tab-level operations

## References

- ADR 001: gax - Google Access CLI
- ADR 002: Multipart YAML-Markdown Format
- ADR 003: Google Docs Sync
- ADR 004: Mail Sync
