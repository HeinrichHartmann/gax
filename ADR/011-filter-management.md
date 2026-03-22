# ADR 011: Declarative Gmail Filter Management

## Status

Proposed

## Context

Gmail filters automatically process incoming emails: add labels, archive, forward, etc.
Managing filters through the Gmail UI is tedious. We want IaC-style declarative management
like we have for labels (ADR 010) and relabeling (ADR 009).

Gmail API constraints:
- Filters can only be created and deleted (no update)
- "Update" requires delete + recreate
- Filters have criteria (matching) and actions (what to do)

## Decision

### Commands

```
gax filter list              # List filters (TSV)
gax filter pull [-o FILE]    # Export to YAML
gax filter plan FILE         # Generate plan
gax filter apply FILE [-y]   # Execute plan
```

### Workflow

```bash
# 1. Export current filters
$ gax filter pull
Wrote 15 filters to filters.yaml

# 2. Edit: add/modify/remove rules
$ vim filters.yaml

# 3. Generate plan
$ gax filter plan filters.yaml
Plan:
  Create: 2
  Update: 1 (delete+recreate)
  Delete: 0
Wrote plan to filters.plan.yaml

# 4. Apply
$ gax filter apply filters.plan.yaml -y
```

### filters.yaml (state file)

```yaml
type: gax/filters
pulled: 2026-03-22T17:30:00Z
filters:
  # Simple: label emails from Alice
  - name: Alice emails
    criteria:
      from: alice@example.com
    action:
      label: Work/Alice
      archive: true

  # Match by subject
  - name: Meeting invites
    criteria:
      subject: "Meeting:"
    action:
      label: Calendar
      star: true

  # Complex query (Gmail search syntax)
  - name: Newsletters
    criteria:
      query: "list:* OR from:newsletter"
    action:
      label: Newsletters
      markRead: true
      neverSpam: true

  # Forward and delete
  - name: Forward invoices
    criteria:
      from: billing@vendor.com
      subject: Invoice
    action:
      forward: accounting@mycompany.com
      trash: true
```

### Criteria Fields

| Field | Type | Description |
|-------|------|-------------|
| `from` | string | Sender address/name |
| `to` | string | Recipient address |
| `subject` | string | Subject contains |
| `query` | string | Full Gmail query syntax |
| `negatedQuery` | string | Exclude matches |
| `hasAttachment` | bool | Has attachment |
| `size` | int | Message size (bytes) |
| `sizeComparison` | string | "larger" or "smaller" |

### Action Fields

| Field | Type | Description |
|-------|------|-------------|
| `label` | string | Add label (auto-create if needed) |
| `removeLabel` | string | Remove label |
| `archive` | bool | Skip inbox |
| `markRead` | bool | Mark as read |
| `star` | bool | Star message |
| `forward` | string | Forward to address |
| `trash` | bool | Move to trash |
| `neverSpam` | bool | Never mark as spam |
| `important` | bool | Mark important |
| `neverImportant` | bool | Never mark important |
| `category` | string | primary/social/updates/forums/promotions |

### Plan Output

```yaml
type: gax/filters-plan
source: filters.yaml
generated: 2026-03-22T17:35:00Z
create:
  - name: Alice emails
    criteria: {from: alice@example.com}
    action: {label: Work/Alice, archive: true}
update:
  - id: ABC123
    name: Newsletters
    criteria: {query: "list:*"}
    action: {label: Newsletters, markRead: true}
delete:
  - id: XYZ789
    criteria: {from: old@example.com}
```

### Matching Strategy

Filters are matched by **criteria hash** since:
- Filter IDs change on delete/recreate
- Short time between pull/push (no drift concern)
- `name` field is for human readability only

Matching algorithm:
1. Hash criteria fields (from, to, subject, query, etc.)
2. Same hash = same filter
3. Different hash = new filter (old deleted if not in file)

### Label Auto-Creation

When a filter references a label that doesn't exist:
1. Create the label automatically
2. Handle nested labels (create parents first)
3. Log the creation

```yaml
action:
  label: Projects/NewClient  # Created if missing
```

### Update = Delete + Create

Gmail API doesn't support filter updates. When criteria or action changes:
1. Delete old filter by ID
2. Create new filter with updated config
3. Plan shows this as "update" for clarity

### Safety

- `--delete` flag required to delete filters not in file
- Without flag, deletions shown but not applied
- `-y` to auto-confirm execution
- Plan always generated before apply

### Commands Detail

**list**
```
gax filter list
```
- TSV output: id, from, to, subject, labels, actions
- Quick overview without full YAML

**pull**
```
gax filter pull [-o FILE]
```
- Default output: `filters.yaml`
- Exports all filters with criteria and actions
- Adds human-readable `name` based on criteria

**plan**
```
gax filter plan FILE [-o PLAN] [--delete]
```
- Computes diff between file and Gmail
- Default plan output: `filters.plan.yaml`
- `--delete` includes filter deletions

**apply**
```
gax filter apply FILE [-y]
```
- Executes plan file
- Creates missing labels automatically
- `-y` auto-confirms

## Consequences

- **Declarative**: Define filters in YAML, tool syncs to Gmail
- **Version controlled**: Track filter changes in git
- **Portable**: Export/import between accounts
- **Safe**: Preview changes before applying
- **Integrated**: Auto-creates labels, works with ADR 010

## Future Considerations

- Filter templates for common patterns
- Import from other email clients
- Validation of criteria syntax
- Conflict detection (overlapping filters)
