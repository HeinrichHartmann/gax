# Changelog

## [Unreleased]

### Added
- `gax pull` - Unified pull command that auto-detects file type from YAML header
- `gax mail list checkout FOLDER` - Materialize full threads to folder
- `gax mail thread` subgroup (clone/pull/reply)
- `gax mail list` subgroup (clone/pull/plan/apply/checkout) - replaces relabel
- `gax mail label clone` - Clone labels to labels.yaml
- `gax mail filter clone` - Clone filters to filters.yaml
- `gax mail label` moved under mail
- `gax mail filter` moved under mail
- Frontmatter format for labels and filters files (header separated by `---`)
- Support for `.gax.yaml` header detection in unified pull

### Changed
- `gax/relabel` type renamed to `gax/list`
- Labels/filters now use frontmatter format with `---` separator
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
clone [TARGET]     → create new .gax file
pull FILE          → update existing .gax file
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
gax mail thread pull thread.mail.gax

# Docs
gax doc clone URL
gax doc pull doc.gax

# Sheets
gax sheet clone URL
gax sheet pull sheet.gax
```
