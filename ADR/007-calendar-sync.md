# ADR 007: Calendar Sync

## Status

Proposed

## Context

gax currently supports Google Docs, Sheets, and Gmail. Users also need calendar access for productivity workflows. The hub project uses `gcalcli` to fetch calendar data to TSV files, but this approach:

1. Requires a separate tool (`gcalcli`)
2. Produces TSV which isn't consistent with gax's formats
3. Is read-only (no push support)

This ADR proposes native Google Calendar API integration following gax patterns.

## Constraints

1. **Fits naturally into gax** - Consistent with mail command structure
2. **Human readable** - Easy to view in terminal
3. **AI readable** - Structured format that LLMs can parse and modify
4. **Pushable** - Support editing events with faithful round-trip
5. **Separate listing from editing** - Like `mail search` vs `mail clone/pull`

## Decision

### Conceptual Mapping

| Concept | Mail | Calendar |
|---------|------|----------|
| Container | Label | Calendar (Work, Personal) |
| Item | Thread | Event |
| List view | `search <query>` | `list [--days N]` |
| Edit unit | Thread (read-only) | Event (read-write) |

### CLI Structure

Follows `doc tab` / `sheet tab` pattern:

```
gax cal
├── calendars                           # List available calendars
├── list [--days N] [--cal NAME]        # View upcoming events
│        [--format md|tsv]              # Output format (default: md)
└── event
    ├── clone <id-or-url>               # Clone existing event → .cal.gax
    ├── new [--cal NAME]                # Create new event file → .cal.gax
    ├── pull <file>                     # Pull latest from API
    ├── push <file>                     # Push changes (diff + prompt)
    └── delete <file>                   # Delete event (with confirmation)
```

**Note:** `event new` creates a local file only. Edit the file, then `push` to create upstream. No direct API creation.

### Comparison with Mail

| Command | Mail | Calendar |
|---------|------|----------|
| List containers | `labels` | `calendars` |
| List/search items | `search <query>` (TSV) | `list [--days N]` (markdown) |
| Clone item | `clone <id>` | `event clone <id>` |
| Pull updates | `pull <file>` | `event pull <file>` |
| Push changes | ❌ read-only | `event push <file>` |

### List Output Format (Human/AI Readable)

`gax cal list` outputs markdown with org-mode inspired structure:

```markdown
# Work

## 2026-03-22 Sat

- 10:00-11:00 **Team Standup** `abc123`
  Zoom

- 14:00-14:30 **1:1 with Alice** `def456`
  Conference Room A

## 2026-03-23 Sun

- 09:00-10:00 **Project Review** `ghi789`
  @bob @charlie

# Personal

## 2026-03-22 Sat

- 12:00-13:00 **Lunch with Bob** `xyz789`
  Restaurant downtown

- (all-day) **Team Offsite** `jkl012` [tentative]
  Berlin Office
```

Features:
- Grouped by calendar (H1), then by date (H2)
- Event ID in backticks for easy `clone` command
- Location on second line
- Attendees with `@` prefix
- Status in brackets when not confirmed: `[tentative]`, `[cancelled]`
- All-day events shown as `(all-day)` instead of time range
- Human scannable, AI parseable

### Event File Format (.cal.gax)

YAML header only - all structured data, no markdown body:

```yaml
---
id: abc123
calendar: Work
source: https://calendar.google.com/calendar/event?eid=...
synced: 2026-03-22T10:00:00Z
title: Team Standup
start: 2026-03-22T10:00:00+01:00
end: 2026-03-22T11:00:00+01:00
timezone: Europe/Berlin
location: Zoom
recurrence: RRULE:FREQ=WEEKLY;BYDAY=MO
attendees:
  - alice@example.com
  - bob@example.com
status: confirmed           # confirmed | tentative | cancelled
conference:
  type: hangoutsMeet
  uri: https://meet.google.com/abc-defg-hij
description: |
  Weekly sync to discuss progress and blockers.

  Agenda:
  - Updates
  - Blockers
---
```

**Why YAML-only (no markdown body):**
- All event fields are structured (unlike email which has free-form body)
- Faithful round-trip requires preserving all fields exactly
- `description` field uses YAML multi-line (`|`) for free text
- Easier to parse, validate, and diff

### Field Mapping

| Field | Type | Editable | Notes |
|-------|------|----------|-------|
| `id` | string | No | Google event ID |
| `calendar` | string | No | Calendar name |
| `source` | URL | No | Link to event |
| `synced` | ISO8601 | No | Last sync time |
| `title` | string | Yes | Event summary |
| `start` | ISO8601 | Yes | Start datetime with offset |
| `end` | ISO8601 | Yes | End datetime with offset |
| `timezone` | string | Yes | IANA timezone |
| `location` | string | Yes | Location or video link |
| `recurrence` | RRULE | Yes | iCal recurrence rule |
| `attendees` | list | Yes | Email addresses |
| `status` | enum | Yes | confirmed, tentative, cancelled |
| `conference` | object | No | Video conference info (type, uri) |
| `description` | text | Yes | Multi-line description |

### OAuth Scope

Add to `auth.py`:

```python
"https://www.googleapis.com/auth/calendar"  # Read-write
```

### Why Not Multipart

Unlike mail (thread = multiple messages) or docs (document = multiple tabs), a single calendar event is atomic. No need for multipart format.

### Why Separate List and Event Commands

**List (`gax cal list`):**
- View-only, not round-trip safe
- Optimized for human scanning
- Markdown format, grouped by calendar/date
- Includes event IDs for easy cloning

**Event (`gax cal event clone/pull/push`):**
- Full YAML for faithful editing
- All fields preserved exactly
- Round-trip safe
- One file per event

## Consequences

### Positive

- **Consistent with mail** - Same command patterns
- **Human readable list** - Markdown with org-mode style
- **AI friendly** - Structured YAML for event editing
- **Safe push** - Single-event granularity limits blast radius
- **Faithful round-trip** - YAML preserves all fields

### Negative

- **New OAuth scope** - Users need to re-authenticate
- **One file per event** - No bulk editing
- **RRULE complexity** - Recurrence rules not human-friendly

### Future Extensions

- `gax cal agenda clone` - Export week view as read-only markdown
- Bulk operations: `gax cal clone <query> --to dir/`
- Support for `.ics` import/export

## References

- ADR 004: Mail Sync (command structure reference)
- ADR 002: Multipart YAML-Markdown Format
- Google Calendar API: https://developers.google.com/calendar/api
- RRULE spec: https://icalendar.org/iCalendar-RFC-5545/3-8-5-3-recurrence-rule.html
- Org-mode timestamps: https://orgmode.org/manual/Timestamps.html
