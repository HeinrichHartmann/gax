# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Knowledge Wiki

`wiki/` is the current-state knowledge base (llm-wiki pattern). To get
up to speed, read `wiki/index.md` and drill into relevant pages. When
editing the wiki, obey `wiki/schema.md` (the discipline; validated by
the librarian agent) and run `direnv exec . scripts/wikilint.py`
before finishing. Precedence on conflict: code > accepted ADR > wiki.

## Multi-Agent Workflow

Agent roles are defined in `.agents/profiles/` and spawned with
`scripts/spawn.py` (uv self-running; `claude` has no `--profile` flag —
the script passes the profile file as the system prompt).

### Roles

- **architect** (`architect.md`): owns ADRs, PRDs, and the beads
  breakdown; writes tests and experiment specs, never implementation
  code. Runs in the main checkout.
- **worker** (`worker.md`): claims ready beads and implements them,
  one bead per commit, in an isolated worktree.
- **reviewer** (`reviewer.md`): dormant for now. Whoever issues the
  work reviews it: the architect reviews worker branches from
  `review` beads (running acceptance criteria live), then
  squash-merges or files `revisions` beads.

### Spawning

```bash
./scripts/spawn.py --profile worker --beads "gax-cvi.1 gax-75t"
./scripts/spawn.py --profile worker --beads "gdoc"        # by label
```

`spawn.py` forks a worktree `../gax-<profile>-<id>` on branch
`<profile>/<id>` from `main` (skip with `--no-fork`), scopes the
session to the given beads/label, and sets the cmux tab title.

### Conventions

- Workers see only committed state on `main` — commit profiles, ADRs,
  and specs before spawning.
- Experiments (`experiments/`) are always committed, one commit per
  bead, referencing the bead ID.
- Handoff between roles happens exclusively through beads
  (`review`/`revisions`/`approved` labels); never through shared
  uncommitted files.
- Merge discipline: **workers rebase** their branch onto main (before
  each ticket and before review); the **architect squash-merges**
  approved branches (`git merge --squash worker/<id>`) — one commit
  per bead on main, micro-commits stay on the worker branch.

## Build & Test

```bash
direnv exec . python -m pytest tests/ -x --tb=short   # unit + surface
direnv exec . python -m pytest tests/ -m e2e          # live-API e2e
```

All shell commands must be prefixed with `direnv exec .` (direnv/nix).

## Architecture Overview

_Add a brief overview of your project architecture_

## Conventions & Patterns

_Add your project-specific conventions here_
