# ADR 019: Clone vs Checkout Pattern

**Status:** Accepted
**Date:** 2026-03-25

## Context

gax works with collections of Google resources (calendar events, sheet tabs, contacts, email threads). Users need two distinct workflows:

1. **Snapshot view** - Quick read-only view of all items in a single file
2. **Working directory** - Individual editable files for each item to modify and push back

We need a consistent pattern across all resources that makes these two modes clear and easy to use.

## Decision

### Core Pattern

We adopt a Git-inspired two-command pattern:

```bash
gax <resource> clone [SOURCE] [OPTIONS]    # Single multipart file
gax <resource> checkout [SOURCE] [OPTIONS] # Folder of individual files
```

Both commands share:
- Same source selector (calendar name, sheet URL, etc.)
- Same filtering options (--days, --tabs, etc.)
- Smart defaults based on resource name
- `-o/--output` flag to override default

### File Naming Convention

**Clone output (single file):**
- Pattern: `<name>.<type>.gax`
- Examples: `calendar.cal.gax`, `Budget-2026.sheet.gax`, `contacts.jsonl.gax`

**Checkout output (folder):**
- Pattern: `<name>.<type>.gax.d/`
- Contains: Individual `<item>.<type>.gax` files
- Examples: `calendar.cal.gax.d/`, `Budget-2026.sheet.gax.d/`

The `.gax.d` suffix makes it clear:
- It's a gax working directory
- Mirrors the `.git.d`, `.terraform.d` convention
- Visually paired with the corresponding `.gax` file

### Multipart Files

Clone produces multipart files using the format from ADR 002:

```
---
type: gax/cal-list
days: 7
calendar: Work
pulled: 2026-03-25T10:00:00Z
---
<TSV or multipart sections>
```

These are:
- Read-only snapshots
- Fast to fetch and view
- Can be re-pulled with `gax pull`
- Contain metadata for reproducible pulls

### Individual Files

Checkout produces individual files:

```
calendar.cal.gax.d/
  2026-03-25_Team-Meeting.cal.gax
  2026-03-26_Review.cal.gax
  2026-03-27_1-1.cal.gax
```

These are:
- Editable working copies
- Can be modified locally
- Synced via plan+apply workflow
- Skip already-present files on re-checkout

### The Explode Command

`gax explode <file.gax>` splits a multipart file into individual files:

```bash
gax explode calendar.cal.gax
# Creates calendar.cal.gax.d/
#   - event1.cal.gax
#   - event2.cal.gax
#   - etc.
```

**Key characteristic:** Explode is an **offline operation**
- Parses the existing multipart file
- Splits into individual `.gax` files
- No API calls to Google
- Preserves exact snapshot data (including any local edits)
- Fast and works offline

**Use case:**
```bash
gax cal clone                    # Quick snapshot → calendar.cal.gax
# (view, realize you want to edit something)
gax explode calendar.cal.gax     # Split offline → calendar.cal.gax.d/
# edit individual files
gax cal event plan event.cal.gax # Plan changes
gax cal event apply plan.yaml    # Push changes
```

**Why not re-fetch?**
- If you want fresh data, just run `gax checkout` directly
- Explode preserves local edits or historical snapshots
- Matches the "explode"/"extract" metaphor (like tar -x, unzip)
- Fast and offline

## Examples by Resource

### Calendars

```bash
# Clone to single file
gax cal clone                        # → calendar.cal.gax
gax cal clone Work -d 30             # → work.cal.gax
gax cal clone -o events.cal.gax      # → events.cal.gax

# Checkout to folder
gax cal checkout                     # → calendar.cal.gax.d/
gax cal checkout Work -d 30          # → work.cal.gax.d/
gax cal checkout -o Week/            # → Week/

# Explode existing file
gax explode calendar.cal.gax         # → calendar.cal.gax.d/
```

### Sheets

```bash
# Clone to single file
gax sheet clone <url>                # → Budget-2026.sheet.gax
gax sheet clone <url> --tabs A,B     # → Budget-2026.sheet.gax

# Checkout to folder
gax sheet checkout <url>             # → Budget-2026.sheet.gax.d/
gax sheet checkout <url> --tabs A,B  # → Budget-2026.sheet.gax.d/

# Explode existing file
gax explode Budget-2026.sheet.gax    # → Budget-2026.sheet.gax.d/
```

### Contacts

```bash
# Clone to single file
gax contacts clone                   # → contacts.md.gax
gax contacts clone -f jsonl          # → contacts.jsonl.gax

# No checkout for contacts (apply works on the full JSONL file)
```

## Rationale

### Why Two Commands?

**Different user intent:**
- `clone` = "Give me a quick snapshot to view"
- `checkout` = "I want to edit individual items"

**Different workflows:**
- Clone → view → done
- Checkout → edit → plan → apply

**Clear from command name:**
- Users know what they're getting
- No flag confusion (--split, -x, etc.)

### Why .gax.d Convention?

**Familiarity:**
- Mirrors `.git`, `node_modules.d`, etc.
- Directory suffix pattern is well-known

**Visual pairing:**
- `calendar.cal.gax` and `calendar.cal.gax.d/` clearly related
- Same base name makes the relationship obvious

**Type safety:**
- Hard to confuse file vs directory
- Shell completion works naturally

### Why Explode is Offline?

**Preserves snapshots:**
- Historical data might not exist upstream anymore
- Local edits preserved
- Works on disconnected/edited files

**Performance:**
- No API quota consumed
- Instant operation
- Works offline

**Clear alternative:**
- Want fresh data? Just run `checkout` instead
- Explode is for "I have this file, split it"

## Implementation Status

- ✅ Calendars: clone, checkout (list-based)
- ✅ Calendars: individual event clone/pull/plan/apply
- ✅ Sheets: clone (multipart)
- ✅ Contacts: clone, plan, apply
- ⏳ Sheets: checkout (to implement)
- ⏳ Explode command (to implement)

## Related ADRs

- ADR 002: Multipart Markdown Format
- ADR 012: Unified Pull
- ADR 015: Unified Clone
- ADR 016: Resource Abstraction (DRAFT)
