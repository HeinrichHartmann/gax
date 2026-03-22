# ADR 009: Mail Relabel

## Status

Proposed

## Context

Apply labels to multiple email threads in bulk. Fetch a set of threads, edit labels, apply changes.

## Decision

### Commands

```
gax mail relabel fetch [QUERY] [-o FILE] [--limit N]
gax mail relabel plan FILE [-o PLAN]
gax mail relabel apply PLAN [-y]
```

### Workflow

```bash
# 1. Fetch threads
$ gax mail relabel fetch "in:inbox"
Wrote 12 threads to relabel.yaml

# 2. Edit: add labels
$ vim relabel.yaml

# 3. Generate plan
$ gax mail relabel plan relabel.yaml
Wrote 3 changes to relabel.plan.yaml

# 4. Apply
$ gax mail relabel apply relabel.plan.yaml
```

### relabel.yaml (editable)

```yaml
# Fetched: 2026-03-22T10:00:00Z
# Query: in:inbox

threads:
  - id: abc123
    labels: []
    from: alice@work.com
    subject: Q1 Planning Meeting
    date: 2026-03-22
    snippet: Let's discuss the roadmap...

  - id: def456
    labels: []
    from: bob@gmail.com
    subject: Dinner Friday?
    date: 2026-03-22
    snippet: Are you free this weekend?
```

After editing:

```yaml
threads:
  - id: abc123
    labels: [Work]
    from: alice@work.com
    subject: Q1 Planning Meeting
    date: 2026-03-22
    snippet: Let's discuss the roadmap...

  - id: def456
    labels: [Personal]
    from: bob@gmail.com
    subject: Dinner Friday?
    date: 2026-03-22
    snippet: Are you free this weekend?
```

### relabel.plan.yaml (changeset)

```yaml
# Source: relabel.yaml
# Generated: 2026-03-22T10:05:00Z

changes:
  - id: abc123
    add: [Work]
    archive: true

  - id: def456
    add: [Personal]
    archive: true
```

### Label syntax

- `labels: []` → no change (skip)
- `labels: [Work]` → add label, archive
- `labels: [Work, Urgent]` → multiple labels, archive
- `labels: [-]` → archive only
- `labels: [!]` → trash

### Commands Detail

**fetch**
```
gax mail relabel fetch [QUERY] [-o FILE] [--limit N]
```
- QUERY: Gmail search syntax (default: `in:inbox`)
- `--limit`: max threads (default: 50)
- Default output: `relabel.yaml`

**plan**
```
gax mail relabel plan FILE [-o PLAN]
```
- Reads relabel.yaml
- Outputs threads where `labels` is non-empty
- Validates label names exist
- Default output: `relabel.plan.yaml`

**apply**
```
gax mail relabel apply PLAN [-y]
```
- Reads plan file
- Shows summary, confirms (unless `-y`)
- Executes: add labels, archive/trash

## Consequences

- Simple bulk label operation
- YAML format easy to edit
- Separate plan step allows review
- No hidden state or magic labels
