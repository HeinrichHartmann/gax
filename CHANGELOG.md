# Changelog

## 2026-07-26

### Added
- **Faithful surgical push for Google Docs** (ADR 034/035): three-way
  plan with revision gate and run-level splicing; pull captures a
  baseline + revisionId (content-addressed store) as the diff anchor
- Plan-driven `gax diff`/`gax push` with `--force-replace` escape hatch
- **Pull guard**: `gax pull` refuses to overwrite unpushed local edits
  (`--force` to discard); **push guard** refuses when the remote moved
  since pull
- Sync header in frontmatter across all resources (doc, sheet, draft,
  thread, slides, drive, calendar, tasks, forms, contacts) with
  staleness warnings in push flows
- `gax pull -r/--recursive` — collect `.gax.md` files and `.gax.md.d/`
  folders across directory trees
- Mail: signature auto-append from `~/.config/gax/signature.md`;
  quoted reply history stripped from thread bodies; reply drafts set
  In-Reply-To/References (correct Gmail threading); drafts sent as
  multipart/alternative with HTML; HTML→markdown fallback when no
  text/plain part
- Live-API fidelity e2e suite for surgical push
- Tree IR (`doc-tree/v1`): formal schema validator, compression
  passes (experimental, `experiments/tree_ir_prototype/`)

### Changed
- **Single-editor sync model** (ADR 037): one push code path; the
  revision guard on push + unpushed-edit guard on pull replace all
  multi-editor drift machinery
- Non-interactive `pull`/`push` refuse to prompt without a TTY and
  point to `-y` instead of failing silently

### Fixed
- Revision guard no longer trips on your own push: after pushing a tab
  in a checkout folder, sibling tab files get the new revisionId
- Unified `gax pull` works on markdown-format forms and drive folders
  (previously died in the YAML-only plan/apply or diff path)
- Draft push writes local files atomically (no more lost `draft_id`)
- Quoted-text stripping hardened against false positives

### Internal
- ADRs 034–037 accepted (surgical push, Tree IR, compression pipeline,
  single-editor sync)
- Multi-agent tooling: `scripts/spawn.py`/`cspawn.py` (profiled agent
  spawning), worker/reviewer/architect/librarian profiles,
  squash-merge convention (one commit per bead on main)
- `wiki/` knowledge base (llm-wiki pattern) with `wiki/schema.md`
  discipline and `scripts/wikilint.py` gate

## [Unreleased]

### Added
- `gax pull` - Unified pull command that auto-detects file type from YAML header
- `gax mail list checkout FOLDER` - Materialize full threads to folder
- `gax mail thread` subgroup (clone/pull/reply)
- `gax mail list` subgroup (clone/pull/plan/apply/checkout) - replaces relabel
- `gax mail label clone [FILE]` - Clone labels to `.label.mail.gax.md` file
- `gax mail filter clone [FILE]` - Clone filters to `.filter.mail.gax.md` file
- `gax mail label` moved under mail
- `gax mail filter` moved under mail
- Frontmatter format for labels and filters files (header separated by `---`)
- Support for `.gax.md.yaml` header detection in unified pull
- `gax man` now shows positional arguments in command signatures
- `make readme` target to auto-generate README from `gax man`

### Changed
- `gax/relabel` type renamed to `gax/list`
- Labels/filters now use frontmatter format with `---` separator
- Labels default file: `labels.yaml` → `label.mail.gax.md`
- Filters default file: `filters.yaml` → `filter.mail.gax.md`
- OAuth scope: `documents.readonly` → `documents` (enables doc import)
- Default mail list limit: 100 → 20
- CLI consistency: all clone commands now use FILE/FOLDER as positional arg
- CLI consistency: all query options now use `-q` flag

### Fixed
- `gax pull` now works on labels and filters files (issue #1)
- Mail list CLI: positional query conflicted with subcommands, now uses `-q`

### Deprecated
- `gax label` - use `gax mail label`
- `gax filter` - use `gax mail filter`
- `gax mail relabel` - use `gax mail list`
- `gax mail search` - use `gax mail list`

---

## CLI Pattern

All resource commands follow this pattern:

```
clone [TARGET]     → create new .gax.md file
pull FILE          → update existing .gax.md file
plan FILE          → generate changeset (IaC resources)
apply PLAN         → apply changeset upstream
```

### Examples

```bash
# Labels (IaC)
gax mail label clone              # → labels.yaml
gax mail label pull labels.yaml
gax mail label plan labels.yaml   # → labels.plan.yaml
gax mail label apply labels.plan.yaml

# Filters (IaC)
gax mail filter clone             # → filters.yaml
gax mail filter pull filters.yaml
gax mail filter plan filters.yaml
gax mail filter apply filters.plan.yaml

# Mail list (IaC)
gax mail list clone inbox.gax -q "in:inbox"
gax mail list pull inbox.gax
gax mail list plan inbox.gax
gax mail list apply inbox.plan.yaml
gax mail list checkout Inbox/ -q "in:inbox"

# Mail threads
gax mail thread clone THREAD_ID
gax mail thread pull thread.mail.gax.md

# Docs
gax doc clone URL
gax doc pull doc.gax

# Sheets
gax sheet clone URL
gax sheet pull sheet.gax
```
