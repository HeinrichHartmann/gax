# ADR 008: Gmail Filters and Labels as Code

## Status

Proposed

## Context

Gmail filters and labels are configuration that users want to:
1. Back up and version control
2. Edit in bulk (add/remove/modify multiple items)
3. Sync across accounts or restore after changes

Current gax patterns work on individual items (one doc tab, one email thread). For filters/labels, we want **Infrastructure-as-Code** style: pull entire config → edit → push changes.

## Constraints

1. **Gmail API limitations:**
   - Filters: Can list, create, delete. **Cannot update** (must delete + recreate)
   - Labels: Can list, create, update, delete
   - System labels (INBOX, SENT, etc.) are read-only

2. **ID handling:**
   - Google generates IDs (e.g., `Label_7975972390520003711`)
   - We need to match by content, not by ID
   - New items get IDs on creation

3. **Safe by default:**
   - Show diff before applying changes
   - Don't delete unless explicitly confirmed

## Decision

### File Formats

#### Filters File (`.gmail-filters.yaml`)

```yaml
# Gmail Filters - managed by gax
# Generated: 2026-03-22T10:00:00Z

filters:
  - criteria:
      from: notifications@github.com
    actions:
      addLabelIds: [GitHub]
      skipInbox: true
      markRead: false

  - criteria:
      from: noreply@linear.app
      subject: "assigned to you"
    actions:
      addLabelIds: [Linear, Todo]
      star: true

  - criteria:
      query: "list:dev@company.com"
    actions:
      addLabelIds: [Dev List]
      skipInbox: true
```

**Criteria fields:** from, to, subject, query, hasAttachment, excludeChats, size, negatedQuery

**Action fields:** addLabelIds, removeLabelIds, forward, skipInbox (archive), markRead, star, trash, neverSpam, neverMarkImportant

**Note:** `addLabelIds` uses label **names** (not IDs) for readability. gax resolves names → IDs on push.

#### Labels File (`.gmail-labels.yaml`)

```yaml
# Gmail Labels - managed by gax
# Generated: 2026-03-22T10:00:00Z

labels:
  - name: GitHub
    color:
      background: "#16a765"
      text: "#ffffff"

  - name: Linear
    color:
      background: "#4285f4"
      text: "#ffffff"

  - name: Todo
    color:
      background: "#fb4c2f"
      text: "#ffffff"

  - name: Clients/Acme
    # Nested label (/ separator)

  - name: Clients/BigCorp
```

**Fields:** name, color (background, text), messageListVisibility, labelListVisibility

### CLI Commands

```
gax mail filter
├── pull [-o FILE]              # Pull all filters → .gmail-filters.yaml
├── push FILE [-y]              # Push changes (diff + confirm)
├── diff FILE                   # Show what would change
└── validate FILE               # Check syntax, resolve label names

gax mail label
├── pull [-o FILE]              # Pull user labels → .gmail-labels.yaml
├── push FILE [-y]              # Push changes (diff + confirm)
├── diff FILE                   # Show what would change
└── create NAME [--color BG]    # Quick create single label
```

### Push Behavior

#### Filter Push

Since filters can't be updated, push does:

1. Pull current filters from Gmail
2. Compare local ↔ remote (by criteria+actions content, not ID)
3. Show diff:
   ```
   Filters to DELETE (2):
     - from:old@example.com → [OldLabel]
     - subject:spam → trash

   Filters to CREATE (1):
     + from:new@example.com → [NewLabel], archive

   Unchanged: 15 filters

   Proceed? [y/N]
   ```
4. Delete removed filters, create new filters

#### Label Push

Labels can be updated, so push does:

1. Pull current user labels from Gmail
2. Compare local ↔ remote (by name)
3. Show diff:
   ```
   Labels to DELETE (1):
     - OldProject

   Labels to CREATE (2):
     + NewClient
     + NewClient/Subproject

   Labels to UPDATE (1):
     ~ Todo: color #ffffff → #fb4c2f

   Unchanged: 8 labels

   Proceed? [y/N]
   ```
4. Apply changes

### Matching Logic

**Filters:** Match by `(criteria, actions)` tuple. If both match exactly → unchanged. Otherwise → delete old + create new.

**Labels:** Match by `name`. Compare other fields for updates.

### Safety Features

1. **Dry-run by default:** `push` shows diff and prompts
2. **Skip flag:** `-y` to skip confirmation
3. **Diff command:** Preview changes without applying
4. **Validate command:** Check file syntax before push
5. **No system label deletion:** Refuse to delete INBOX, SENT, etc.

## Consequences

### Positive

- **Version control:** Filters/labels in git
- **Bulk editing:** Edit YAML, push all changes at once
- **Backup/restore:** Pull config, restore later
- **Account sync:** Pull from one account, push to another
- **AI-friendly:** LLM can generate/modify filter rules

### Negative

- **Filter recreation:** Updates require delete+create (may reorder)
- **Label name as key:** Renaming = delete + create (loses messages' label?)
- **No partial push:** All-or-nothing for filters

### Edge Cases

- **Label rename:** Warn user that renaming creates new label (old one must be explicitly deleted)
- **Nested labels:** `Parent/Child` format, create parent if missing
- **Filter ordering:** Gmail doesn't guarantee order, we preserve file order on create
- **Missing labels in filters:** Validate that referenced labels exist before push

## Examples

### Backup filters
```bash
gax mail filter pull -o ~/dotfiles/gmail-filters.yaml
git add ~/dotfiles/gmail-filters.yaml
git commit -m "Backup Gmail filters"
```

### Add new filter
```bash
# Edit file, add new filter
vim .gmail-filters.yaml

# Preview changes
gax mail filter diff .gmail-filters.yaml

# Apply
gax mail filter push .gmail-filters.yaml
```

### Sync to new account
```bash
# On old account
gax mail filter pull -o filters.yaml
gax mail label pull -o labels.yaml

# Switch to new account
gax auth logout && gax auth login

# Push to new account (creates labels first)
gax mail label push labels.yaml -y
gax mail filter push filters.yaml -y
```

## References

- Gmail API Filters: https://developers.google.com/gmail/api/reference/rest/v1/users.settings.filters
- Gmail API Labels: https://developers.google.com/gmail/api/reference/rest/v1/users.labels
- ADR 004: Mail Sync
