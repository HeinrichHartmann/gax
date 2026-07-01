# ADR 021: `gax get` — Fetch Remote Resource to stdout

**Status:** Accepted (supersedes earlier ADR 021 "stdout Output for clone via `-o -`")
**Date:** 2026-07-01

## Context

There is no way to print a Google resource's remote content to stdout without first
writing a tracking file to disk. This blocks common shell patterns:

```bash
gax clone https://docs.google.com/... | less        # doesn't work
diff <(gax ...) local.md                            # doesn't work
```

The original ADR 021 (2026-04-08) addressed the URL case by extending `gax clone`
with `-o -` for stdout. That was never implemented, and on reflection the design
has two problems:

1. `-o -` only covers the URL→stdout path. Using an existing tracking file as the
   source still requires a separate two-step flow.
2. `-o -` is not discoverable and doesn't appear in `gax --help` as a first-class verb.

## Decision

Add `gax get` as a top-level meta-command alongside `clone`, `pull`, `push`,
`checkout`. It fetches the **remote** state of a resource and prints it to stdout.
No file is created or modified.

```
gax get <url-or-file>
```

### Inputs

| Argument | Behaviour |
|----------|-----------|
| Google URL | Fetch remote resource, print content to stdout |
| Local `.gax.md` tracking file | Read `source:` URL from header, fetch remote, print to stdout |

Resource type is inferred from the URL, exactly as `gax clone` does.
For local files, the source URL is read from the YAML frontmatter — this is
equivalent to `gax pull -o /dev/stdout`.

### stdout / stderr discipline

- **stdout**: file content only (safe to pipe or redirect)
- **stderr**: all human-readable noise (progress, warnings)

This makes `gax get` composable:

```bash
gax get https://docs.google.com/document/d/abc123 | grep TODO
diff <(gax get https://docs.google.com/document/d/abc123) local.md
gax get report.doc.gax.md | pandoc -f markdown -t html
```

### No side effects

`gax get` never writes files, never modifies tracking files, never creates remote
resources. It is a pure read of remote state.

## Examples

```bash
# Quick inspection of a remote doc
gax get https://docs.google.com/document/d/abc123 | less

# Grep a remote spreadsheet
gax get https://docs.google.com/spreadsheets/d/xyz789 | grep "Q1"

# Diff remote vs local tracking file
diff <(gax get https://docs.google.com/document/d/abc123) doc.gax.md

# Fetch via local tracking file (reads source: URL from header)
gax get report.doc.gax.md

# Pipe into another tool
gax get meeting-notes.doc.gax.md | pbcopy
```

## Relation to `gax clone -o -`

The `-o -` extension on `gax clone` proposed in the original ADR 021 is **withdrawn**.
`gax get <url>` covers the same URL→stdout use case with a more discoverable interface.
The `-o` flag on `clone` continues to accept file paths only.

## Implementation

`gax get` is a meta-command in `gax/cli.py`. For URL input it dispatches via
`Resource.from_url()`. For file input it reads the `source:` header from the
YAML frontmatter and calls `Resource.from_url()` on that. Either way it calls
`resource.clone()` into a temporary directory, reads the resulting file(s), and
prints `section.content` to stdout. The temp directory is discarded on exit.

```python
@main.command("get")
@click.argument("source")  # URL or .gax.md file path
@gax_command
def get_cmd(source: str):
    """Fetch remote resource and print content to stdout."""
    ...
```

## Alternatives Considered

### Keep `-o -` on `gax clone`

**Rejected.** Not implemented after many months, not discoverable.

### `gax cat`

**Rejected.** `cat` implies printing local file content. `get` makes clear
the operation fetches from the remote source.

### Resource-specific `gax doc get`, `gax sheet get`, etc.

**Rejected.** The value of `get` is precisely that it works uniformly on any
resource without knowing the type.

## Related ADRs

- ADR 015: Unified Clone
- ADR 022: Simplified CLI Model (clone/pull/push/checkout as meta-commands)
- ADR 019: Clone vs Checkout Pattern
