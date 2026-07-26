---
title: CLI Model
description: Command structure, operation patterns, and resource dispatch
status: current
updated: 2026-07-26
sources:
  - gax/cli.py
  - gax/resource.py
  - README.md
---

## Command structure

gax has three layers of commands:

1. **Unified top-level commands** that work on any resource type:
   `clone`, `checkout`, `pull`, `push`, `diff`, `get`.
2. **Resource-specific groups** (`doc`, `sheet`, `mail`, `cal`, `draft`,
   `contacts`, `form`, `slides`, `task`, `file`, `mailbox`,
   `mail-label`, `mail-filter`) for operations specific to one
   resource type.
3. **Utility commands**: `auth`, `issue`, `upgrade`, `man`.

## Unified commands

| Command | Input | Effect |
|---|---|---|
| `clone <url>` | URL | Fetch remote to a single `.gax.md` file |
| `checkout <url>` | URL | Fetch remote to a `.gax.md.d/` folder (one file per item) |
| `pull <files>` | File(s) or folder(s) | Refresh local from remote |
| `push <files>` | File(s) or folder(s) | Push local edits to remote (with diff + confirmation) |
| `diff <files>` | File(s) or folder(s) | Preview changes without pushing |
| `get <url-or-file>` | URL or file | Fetch remote content to stdout (read-only) |

`pull` and `push` accept globs and work recursively on `.gax.md.d/`
folders. `push` always shows a diff and requires confirmation (skip
with `-y`).

## Resource dispatch

The `Resource` base class (`gax/resource.py`) provides two dispatch
mechanisms:

- **`Resource.from_url(url)`** — matches the URL against each
  registered subclass's `URL_PATTERN` regex.
- **`Resource.from_file(path)`** — matches by file extension
  (`FILE_EXTENSIONS`), YAML `type:` header (`FILE_TYPE`), or checkout
  folder metadata (`CHECKOUT_TYPE` in `.gax.yaml`).

Registration is explicit: each subclass calls `Resource.register(cls)`
after class definition. CLI modules are imported in `gax/cli.py` at
startup to populate the dispatch table.

Resources with ambiguous URL patterns (e.g. `Doc` vs `Tab` on the
same domain) set `HAS_GENERIC_DISPATCH = False` and are only reached
via explicit subclass calls.

## Operation patterns

### Clone/Checkout (ADR 019)

- **`clone`** creates a single `.gax.md` file (one tab/item). For
  multi-tab resources, clones the first tab and hints about others.
- **`checkout`** creates a `.gax.md.d/` directory with individual
  files per item, plus a `.gax.yaml` metadata file.

### Pull/Push with confirmation

`push` is always preceded by a diff preview. The user confirms before
any remote mutation. `--yes`/`-y` skips the prompt for scripting.

### Plan/Apply (bulk resources)

Labels, filters, and mailbox operations use a two-phase workflow:
`plan` generates a changeset file for review, `apply` executes it.
Sheets use a similar internal `PushPlan` pattern.

## Standard resource operations

Every `Resource` subclass may implement:

| Method | Signature | Purpose |
|---|---|---|
| `clone` | `(output?) -> Path` | Fetch remote to local file |
| `checkout` | `(output?) -> Path` | Fetch remote to local folder |
| `pull` | `() -> None` | Refresh local from remote |
| `diff` | `() -> str or None` | Preview changes |
| `get` | `() -> str` | Fetch remote as string (read-only) |
| `push` | `() -> None` | Push local to remote (unconditional) |

Unimplemented operations raise `NotImplementedError`.
