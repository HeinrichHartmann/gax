# ADR 021: stdout Output for clone via `-o -`

**Status:** Accepted
**Date:** 2026-04-08

## Context

`gax clone <url>` always writes to a local `.gax` file. There is no way to get
resource content on stdout for piping or quick inspection without creating a file.

A `gax cat <url>` command was considered but rejected: the file-input case
(`gax cat file.gax`) would just be `gax pull` + `cat` — two shell commands that
already work. Adding a dedicated command for that is unnecessary.

The URL case is the real gap: fetching a resource to stdout without writing to disk.

## Decision

Extend the `-o/--output` flag on `gax clone` to accept `-` as a special value
meaning stdout. Progress and status messages go to stderr; only file content goes
to stdout.

```bash
gax clone -o - <url>
```

This follows the Unix convention used by `curl`, `tar`, `ffmpeg`, and others.

## Behavior

- `-o -` routes content to stdout instead of a file
- All progress/status messages (e.g. "Fetching…") go to stderr
- No file is created
- All existing URL patterns supported (same as `gax clone`)

## Examples

```bash
# Quick inspection
gax clone -o - https://docs.google.com/document/d/abc123 | less

# Grep a spreadsheet
gax clone -o - https://docs.google.com/spreadsheets/d/xyz789 | grep "Q1"

# Diff remote vs local
diff <(gax clone -o - https://docs.google.com/document/d/abc123) local.doc.gax

# Save snapshot with explicit name
gax clone -o - https://docs.google.com/document/d/abc123 > snapshot.doc.gax
```

## Rationale

**Why `-o -` rather than a new `cat` command?**

`gax cat file.gax` would just replicate `gax pull file.gax && cat file.gax` — there
is no reason to add a command for that. The only real gap is URL → stdout, and
`-o -` on `clone` covers it without adding a new top-level command or a new concept.

**Why `-` for stdout?**

Unix convention: `curl -o -`, `tar -o`, `ffmpeg -o -`. Users familiar with CLI tools
will expect it; no explanation needed.

**stdout / stderr discipline**

When `-o -` is active, all human-readable output (progress, counts, titles) must
be redirected to stderr so that stdout contains only the file content. This makes
the output safe to pipe or redirect.

## Related ADRs

- ADR 015: Unified Clone
- ADR 019: Clone vs Checkout Pattern
