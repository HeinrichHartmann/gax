# ADR 032: `get` Command for Stateless Remote Inspection

**Status:** Proposed
**Date:** 2026-05-31

## Context

`gax pull` refreshes local files from remote state. This requires:
1. A local file to already exist (with source metadata)
2. Overwriting local state (destructive)
3. Multiple tool calls for an AI agent (pull, then read the file)

There is no way to **inspect** remote state without side effects. The closest
is `gax clone -o -` (ADR 021), but that:
- Only works from URLs, not from existing local files
- Isn't implemented yet
- Is discoverable only if you know the `-o -` convention

For AI tool use, the dominant pattern is "read something, reason, decide."
The read step should be fast, stateless, and produce output directly on
stdout without touching the filesystem.

### Current state of `-o` on pull

`gax pull` has no `-o/--output` flag. It always writes to the existing file.
`gax clone` has `-o` but accepts only `Path`, not the `-` stdout convention.

### Consistency concern

The CLI currently has these read operations scattered across resources:

| Command | Output | Stateless? |
|---------|--------|------------|
| `gax doc tab list <url>` | stdout | Yes |
| `gax sheet tab list <url>` | stdout | Yes |
| `gax draft list` | stdout | Yes |
| `gax label list` | stdout | Yes |
| `gax mailbox list` | stdout | Yes |
| `gax clone -o - <url>` | stdout (proposed) | Yes |
| `gax pull <file>` | file (overwrite) | **No** |

The `list` commands are already stateless stdout operations. What's missing
is the equivalent for **content** — "show me what's in this resource right
now, without changing anything."

## Decision

Add `gax get` as a top-level command that fetches remote content to stdout.

```bash
gax get <file-or-url>
```

### From an existing local file

```bash
gax get Budget.sheet.gax.md.d/
gax get report.doc.gax.md
gax get inbox.mailbox.gax.md
```

Reads the source URL from the file's metadata, fetches current remote
content, prints to stdout. **Does not modify the local file.**

### From a URL

```bash
gax get https://docs.google.com/spreadsheets/d/abc123
gax get https://docs.google.com/document/d/xyz789
```

Equivalent to `gax clone -o -` — fetches and prints without creating a file.

### Output format

- Same format as the local file would contain (markdown tables, markdown
  content, etc.)
- Progress/status goes to stderr
- Content goes to stdout
- No YAML frontmatter by default (just content); add `--full` for the
  complete file including headers

## Behavior

```
gax get <target>        # Content only to stdout
gax get --full <target> # Full file with YAML frontmatter
gax get --tab <name> <target>  # Single tab (for multi-tab resources)
```

The command is:
- **Read-only** — never modifies local files or remote state
- **Stateless** — no local file needed (when using URL)
- **Pipeable** — clean stdout, no spinners/progress on stdout

## Examples

```bash
# Quick inspection
gax get Budget.sheet.gax.md.d/ | less

# Diff local vs remote without pulling
diff <(gax get report.doc.gax.md) report.doc.gax.md

# Grep remote sheet
gax get Budget.sheet.gax.md.d/ --tab Revenue | grep "Q2"

# AI agent reads remote state
gax get notes.doc.gax.md  # stdout → agent context

# From URL (no local file needed)
gax get https://docs.google.com/document/d/abc123
```

## Implementation

### Resource interface

Add `get()` to the Resource base class:

```python
def get(self, **kw) -> str:
    """Fetch remote content and return as string. Read-only."""
    raise NotImplementedError(f"{self.name} does not support get")
```

For most resources, `get()` is the same as the internal fetch logic used by
`pull()`, but returns the formatted content as a string instead of writing
to a file.

### CLI entry point

```python
@main.command("get")
@click.argument("target")
@click.option("--full", is_flag=True, help="Include YAML frontmatter")
@click.option("--tab", help="Specific tab name (multi-tab resources)")
def unified_get(target, full, tab):
    ...
```

### Relationship to existing commands

| Operation | Reads remote | Writes local | Writes remote |
|-----------|-------------|-------------|--------------|
| `get`     | Yes         | No          | No           |
| `pull`    | Yes         | Yes         | No           |
| `diff`    | Yes         | No          | No           |
| `push`    | Yes (for diff) | No       | Yes          |
| `clone`   | Yes         | Yes (new)   | No           |

`get` is the pure-read operation. `diff` also reads remote but formats
as a comparison. `get` returns the raw content.

### Phased rollout

**Phase 1:** Implement for sheets and docs (highest-value for AI use).
`get` on a `.sheet.gax.md.d/` folder prints all tabs as markdown tables.
`get` on a `.doc.gax.md` file prints the document content.

**Phase 2:** Extend to remaining resources (mail, cal, contacts, etc.).

**Phase 3:** Add `--format` flag for structured output (JSON, CSV).

## Alternatives Considered

### Alternative 1: `gax pull -o /dev/stdout`

**Description:** Add `-o` to pull, support `/dev/stdout` or `-`.

**Rejected because:**
- `-o` on pull changes semantics (pull normally refreshes in-place)
- Inconsistent: `-o` on clone means "output path", on pull it would mean "don't update file"
- Not discoverable for AI agents
- Doesn't work from URLs (pull requires existing file)

### Alternative 2: `gax cat <file>`

**Description:** Dedicated cat command.

**Rejected in ADR 021 because** `cat file.gax` already works for local
content. But `gax cat` that fetches **remote** content is different from
Unix `cat`. The name is misleading.

### Alternative 3: `gax show <file>`

**Description:** Use `show` (git-inspired: `git show`).

**Viable alternative.** `show` implies displaying something. But `get` is
more standard for HTTP/REST semantics (GET = read without side effects)
and shorter to type.

### Alternative 4: `gax read <file>`

**Description:** Explicit "read remote."

**Viable but verbose.** `get` is the standard verb for idempotent reads
(HTTP GET, REST conventions). Shorter, widely understood.

## Consequences

### Positive

- AI agents can inspect remote state in one tool call
- No accidental overwrites from "just checking"
- Composable with Unix pipes (`grep`, `diff`, `jq`, `wc`)
- Consistent with REST semantics familiar to developers

### Negative

- Another top-level command to document
- Content formatting must work without file context (no local file for
  format hints when using URL mode)

### Neutral

- `pull` remains the command for "update my local copy"
- `clone` remains for "create a new local copy"
- `get` fills the gap: "show me what's there, touch nothing"

## Related ADRs

- ADR 012: Unified Pull (get is the read-only sibling)
- ADR 021: stdout via `-o -` on clone (get subsumes this for the file case)
- ADR 022: Simplified CLI Model (get completes the operation matrix)
