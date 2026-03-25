# ADR 018: Calendar Date Ranges

## Status

Proposed

## Context

`gax cal list` currently only shows future events. The `--days` flag controls how far ahead to look, but there is no way to view past events or specify an arbitrary date range. This limits use cases like reviewing last week's meetings, generating timesheets, or auditing calendar history.

## Decision

### New Options

Add `--from` and `--to` options to `gax cal list`:

```
gax cal list --from 2026-03-01 --to 2026-03-15
gax cal list --from 2026-03-01 --to 2026-03-15 -c Work
gax cal list --from 2026-01-01 --to 2026-03-31 -f tsv
```

Both accept ISO 8601 date strings (`YYYY-MM-DD`). When omitted:

- `--from` defaults to now (current behavior)
- `--to` defaults to 7 days after `--from`

### Mutual Exclusivity with `--days`

`--from`/`--to` and `--days` are mutually exclusive. Using them together is an error:

```
# ERROR: Cannot combine --days with --from/--to
gax cal list --days 14 --from 2026-03-01
```

### Examples

```bash
# Future events (unchanged behavior)
gax cal list                              # Next 7 days (default)
gax cal list -d 14                        # Next 14 days

# Date ranges
gax cal list --from 2026-03-01 --to 2026-03-15   # Specific range
gax cal list --from 2026-03-01                     # 7 days from March 1st
gax cal list --to 2026-03-25                       # From now until March 25th

# Combined with other options
gax cal list --from 2026-03-01 --to 2026-03-31 -c Work -f tsv
gax cal list --from 2026-03-01 --to 2026-03-31 clone march.cal.gax
gax cal list --from 2026-03-01 --to 2026-03-31 checkout March/
```

### API Changes

`list_events()` signature changes from:

```python
def list_events(*, days: int = 7, calendar_ids=None, service=None)
```

to:

```python
def list_events(*, time_min: datetime, time_max: datetime, calendar_ids=None, service=None)
```

Callers compute `time_min`/`time_max` from CLI options. The function no longer owns date logic.

### File Header Persistence

When cloning a cal-list file with `--from`/`--to`, the header stores the range for pull:

```yaml
---
type: gax/cal-list
content-type: text/tab-separated-values
from: "2026-03-01"
to: "2026-03-15"
pulled: 2026-03-15T10:00:00Z
---
```

When `--days` is used (relative mode), the header stores `days` as before. On pull, relative mode re-anchors to now; absolute mode preserves the original range.

## Consequences

### Positive

- Past events become accessible
- Arbitrary date ranges enable timesheets, audits, reporting
- Clean separation: `--days` for relative, `--from`/`--to` for absolute
- Backwards compatible: default behavior unchanged

### Negative

- Three options (`--days`, `--from`, `--to`) with mutual exclusivity adds validation logic
- Absolute ranges in saved files may become stale (by design — use pull to refresh)
