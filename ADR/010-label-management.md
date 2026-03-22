# ADR 010: Declarative Label Management

## Status

Proposed

## Context

Gmail labels need to be managed: created, renamed, deleted, organized into hierarchies.
Currently `gax mail relabel` handles applying labels to threads. This ADR covers
managing the labels themselves in a declarative, IaC-style workflow.

Related: ADR 009 (Mail Relabel) handles thread labeling.

## Decision

### Commands

```
gax label pull [-o FILE]      # Export current labels
gax label push FILE [-y]      # Apply label changes
gax label list                # Quick list (existing command)
```

### Workflow

```bash
# 1. Export current labels
$ gax label pull
Wrote 42 labels to labels.yaml

# 2. Edit: add/rename/delete/reorganize
$ vim labels.yaml

# 3. Apply changes
$ gax label push labels.yaml
Plan:
  Create: 2
  Rename: 1
  Delete: 0
Apply? [y/N]
```

### labels.yaml (state file)

```yaml
# Labels state file
# Generated: 2026-03-22T12:00:00Z

labels:
  # Simple label
  - name: Work

  # Nested labels (parent/child)
  - name: Projects
  - name: Projects/Active
  - name: Projects/Archive

  # With display settings
  - name: Urgent
    color:
      text: "#ffffff"
      bg: "#ff0000"
    visible: true        # labelListVisibility
    show_in_list: true   # messageListVisibility
```

### Operations

**Create**: Labels in file but not in Gmail are created.
- Nested labels: parents created first automatically
- Color/visibility settings applied

**Rename**: Use special syntax or separate mapping:
```yaml
labels:
  - name: NewName
    rename_from: OldName
```

**Delete**: Labels in Gmail but not in file are candidates for deletion.
- Requires explicit `--delete` flag (safety)
- System labels cannot be deleted

**Reorganize/Nest**: Change hierarchy by renaming:
```yaml
# Move "ClientA" under "Projects"
- name: Projects/ClientA
  rename_from: ClientA
```

### Nesting Support

Gmail API requires parent labels to exist before children.
The push command handles this automatically:

1. Parse all label names
2. Sort by depth (parents first)
3. Create parents before children
4. For renames, handle order carefully

### Safety

- `--delete` flag required to actually delete labels
- Without flag, deletions are shown but not applied
- System labels (INBOX, SPAM, etc.) are read-only
- Dry-run by default, `-y` to auto-confirm

### Color Support

Gmail label colors use hex codes:

```yaml
- name: Important
  color:
    text: "#000000"
    bg: "#16a765"
```

Available background colors are limited by Gmail's palette.

### Visibility Settings

```yaml
- name: Archive
  visible: false           # Hide from label list
  show_in_list: false      # Hide in message list
```

Maps to:
- `labelListVisibility`: show/hide in sidebar
- `messageListVisibility`: show/hide on messages

### Filtering

```bash
# Only pull user labels (default)
$ gax label pull

# Include system labels (read-only, for reference)
$ gax label pull --all
```

### Commands Detail

**pull**
```
gax label pull [-o FILE] [--all]
```
- Exports user labels to YAML
- Default output: `labels.yaml`
- `--all` includes system labels (marked read-only)

**push**
```
gax label push FILE [-y] [--delete]
```
- Computes diff between file and Gmail
- Shows plan: create/rename/delete counts
- `--delete` enables actual deletion
- `-y` auto-confirms

**list** (existing)
```
gax label list
```
- Quick TSV listing of all labels

## Consequences

- **Declarative**: Define desired state, tool computes diff
- **Safe**: Deletions require explicit flag
- **Hierarchical**: Handles nested labels correctly
- **Portable**: YAML file can be version controlled
- **Complements relabel**: Labels managed separately from thread labeling

## Future Considerations

- Label templates for common setups
- Import/export between accounts
- Batch color assignment
- Integration with filters (ADR 008)
