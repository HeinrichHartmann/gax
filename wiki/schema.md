# Wiki Schema — Structure and Maintenance Discipline

This file is the schema of the gax knowledge wiki (llm-wiki pattern:
raw sources → wiki → schema). It defines the structure, the page
format, and the exact procedures for updating the wiki.

**This schema MUST be obeyed by every agent editing the wiki, and it
is validated by the librarian agent** (`.agents/profiles/librarian.md`).
`scripts/wikilint.py` runs the basic mechanical checks (frontmatter
schema, links resolve, index coverage); the librarian enforces
everything the linter cannot check.

## Purpose

The wiki is the **current-state synthesis** of the project, written
for agents (and humans) getting up to speed. It answers "what is true
now" — the ADRs answer "why we decided it", the code answers "how it
is done exactly". The wiki adds value only if it is kept current;
a stale wiki is worse than no wiki.

## Layers

| Layer | Location | Who writes |
|---|---|---|
| Raw sources | `ADR/`, GitHub issues, code, beads | humans + agents (outside wiki discipline) |
| Wiki | `wiki/` | agents, following this schema |
| Schema | `wiki/schema.md` (this file) | humans; agents only with explicit user approval |

The wiki NEVER replaces its sources. It summarizes and links to them.
On any conflict: code > accepted ADR > wiki. If you find a conflict,
fix the wiki page (or mark it `stale`), never the source.

## Directory Layout

```
wiki/
├── schema.md             # this file — the schema (no frontmatter)
├── index.md              # catalog of all pages (no frontmatter)
├── log.md                # append-only change log (no frontmatter)
├── product/              # PRD layer: problem, users, value, positioning
├── solution/             # functional spec: CLI surface, file formats, UX
├── technical/            # architecture: how it is structured
└── implementation/       # gotchas, invariants, API quirks
```

The four category directories answer four distinct questions:

| Directory | Question | Example page |
|---|---|---|
| `product/` | Why does gax exist? What problem, for whom? | `positioning.md` |
| `solution/` | What does the user see and type? | `cli-model.md` |
| `technical/` | How is it structured internally? | `tree-ir.md` |
| `implementation/` | What must I watch out for when coding? | `google-api-quirks.md` |

Place a page by the question it answers, not by the feature it
describes. A feature may have pages in several categories.

## Page Rules

1. One topic per page. If a page needs more than ~150 lines, split it.
2. Filenames: lowercase kebab-case, `.md` extension (`cli-model.md`).
   No spaces, no underscores, no uppercase.
3. No subdirectories below the four categories.
4. Every page carries YAML frontmatter (spec below). `schema.md`,
   `index.md`, and `log.md` are the only exceptions.
5. No horizontal rules (`---`) in page bodies. Structure with headings.
6. Link to sources with relative paths (`../../ADR/025-....md`) or
   issue refs (`HeinrichHartmann/gax#60`). Link between wiki pages
   with relative paths (`../technical/tree-ir.md`).
7. Do not duplicate CLAUDE.md content (build commands, agent roles).
   Link to it instead.

## Frontmatter Spec

```yaml
---
title: CLI Model            # human-readable page title
description: One line shown in index.md and used for relevance triage
status: current             # current | draft | stale
updated: 2026-07-26         # date of last substantive edit (YYYY-MM-DD)
sources:                    # non-empty list: what this page is derived from
  - ADR/025-directory-only-collections.md
  - HeinrichHartmann/gax#60
---
```

Status semantics:

- `current` — verified against sources at `updated` date
- `draft` — written but not yet reviewed/verified
- `stale` — known or suspected to be outdated; body may be wrong.
  Set this the moment you notice a discrepancy you cannot fix now.

## index.md Rules

- Catalog of every page: one bullet per page, grouped by category.
  Format (copy the frontmatter description):
  `- [Title](<category>/<page>.md) — <description>`
- Updated in the same edit session as any page add/rename/removal.
- Agents getting up to speed read `index.md` first, then drill into
  relevant pages. Keep descriptions specific enough for that triage.

## log.md Rules

- Append-only. Never edit or delete existing entries.
- New entries at the bottom. Entry format:

```
## [2026-07-26] ingest | ADR 035 accepted — updated tree-ir, cli-model
```

- Operations: `create` (new page), `ingest` (new source folded in),
  `update` (page revised), `lint` (lint pass performed), `prune`
  (page removed/merged).
- One line of detail below the heading is allowed; more is not.
  Details belong in the pages themselves.

## Procedures

### When to update the wiki

Update the wiki in the same session when you:

- accept, supersede, or reject an ADR → update every page listing that
  ADR in `sources`, plus `technical/adr-map.md`
- change the CLI surface (commands, flags, file naming) → `solution/`
- learn a non-obvious API behavior or invariant the hard way →
  `implementation/`
- close a GitHub issue or epic that changes product scope → `product/`

Routine code changes that do not change any statement in the wiki
require no wiki edit. Do not log no-ops.

### Ingest procedure (new source → wiki)

1. Read the source fully.
2. Grep the wiki for affected pages (`grep -ril <topic> wiki/`), and
   check `index.md` for related pages.
3. Update existing pages before creating new ones. Create a new page
   only for a topic with no natural home.
4. In every touched page: revise the body, update `updated`, extend
   `sources` if a new source informed it.
5. Update `index.md` if pages were added, renamed, or re-described.
6. Append one `log.md` entry covering the whole ingest.
7. Run `direnv exec . scripts/wikilint.py` — must pass before you
   consider the edit done.

### Query procedure (answering from the wiki)

1. Read `index.md`, open only relevant pages.
2. Verify load-bearing claims against sources when the answer will be
   acted on (a `current` status is a claim, not a guarantee).
3. If the answer produced durable insight (a comparison, a synthesis,
   a resolved contradiction), file it back as a page update or a new
   page — with a normal ingest log entry.

### Lint procedure (periodic health check)

Mechanical (every wiki edit): `direnv exec . scripts/wikilint.py`.

Semantic (on request, or when the wiki feels off) — check for:

- contradictions between pages, or between pages and sources
- `current` pages whose sources changed after `updated` → fix or
  mark `stale`
- concepts referenced on 3+ pages that have no page of their own
- pages that should be merged (heavy overlap) or split (>150 lines)

Record findings as fixes (preferred) or `stale` markers, append one
`lint` entry to `log.md`.

### Review discipline

- Agents write the wiki; the user reads it. Substantive rewrites of
  existing `current` pages should be surfaced in the session handoff.
- Changes to this schema require explicit user approval.
- Wiki edits are committed with the work that motivated them, same
  commit or same PR — an uncommitted wiki edit is invisible to other
  agents (workers see only committed state on `main`).
