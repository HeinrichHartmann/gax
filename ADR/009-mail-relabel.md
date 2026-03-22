# ADR 009: Mail Relabel

## Status

Accepted

## Context

Apply labels to multiple email threads in bulk. Need a workflow that:
1. Works well with AI assistants (token efficient)
2. Shows current state, allows editing to desired state
3. Computes and applies diffs

## Decision

### Core Primitive: Editable State Representation

The fundamental abstraction is a **state representation** that can be edited.

- `fetch` outputs current state (labels each thread has now)
- User edits to declare desired state
- `plan` computes diff between current and desired state
- `apply` executes the diff

This is the IaC (Infrastructure as Code) pattern applied to email labels.

### Format: TSV

TSV chosen over YAML for AI/token efficiency:
- Tabular data compresses well
- Easy to parse and generate
- `-q` flag omits comments for machine processing

### Commands

```
gax mail relabel fetch [QUERY] [-o FILE] [--limit N] [-q] [--all]
gax mail relabel plan FILE [-o PLAN]
gax mail relabel apply PLAN [-y]
```

### Workflow

```bash
# 1. Fetch threads (shows current labels)
$ gax mail relabel fetch "in:spam newer_than:10d"
Wrote 8 threads to relabel.tsv

# 2. View current state
$ cat relabel.tsv
# Query: in:spam newer_than:10d
# Labels: inbox, archive, trash, spam, or label name
id	from	subject	date	labels
19d0ad64...	alice@docusign.net	Signature requested	2026-03-20	MyLabel
19cffd14...	spam@fake.com	Academic Journal	2026-03-18

# 3. Edit: declare desired state
# Change labels column to what you WANT

# 4. Generate plan (computes diff)
$ gax mail relabel plan relabel.tsv
Wrote 3 changes to relabel.plan.yaml
  Move to inbox: 2
  Remove labels: 1

# 5. Apply
$ gax mail relabel apply relabel.plan.yaml
```

### relabel.tsv (editable state)

Current state from fetch:
```tsv
id	from	subject	date	labels
19d0ad64...	alice@docusign.net	Signature	2026-03-20	MyLabel
19cffd14...	spam@fake.com	Journal	2026-03-18
```

After editing (desired state):
```tsv
id	from	subject	date	labels
19d0ad64...	alice@docusign.net	Signature	2026-03-20	inbox
19cffd14...	spam@fake.com	Journal	2026-03-18
```

### relabel.plan.yaml (computed diff)

```yaml
source: relabel.tsv
generated: 2026-03-22T10:05:00Z
changes:
  - id: 19d0ad64...
    unspam: true
    remove:
      - MyLabel
```

### Special Labels (Actions)

These trigger special Gmail operations:
- `inbox` → move from spam to inbox (add INBOX, remove SPAM)
- `archive` → remove from inbox (remove INBOX)
- `trash` → move to trash
- `spam` → mark as spam (add SPAM, remove INBOX)

Can be combined with regular labels:
```
inbox,Work,Urgent
```

### Regular Labels

Any label name not in the special list is treated as a user label.
The plan computes add/remove operations to reach desired state.
**Labels are auto-created if they don't exist** at apply time.

### Flags

- `-q, --quiet`: Skip header comments (for machine processing)
- `--all`: Include system labels (INBOX, SPAM, UNREAD, etc.)
- `--limit N`: Maximum threads to fetch (default: 50)
- `-y, --yes`: Skip confirmation on apply

### Commands Detail

**fetch**
```
gax mail relabel fetch [QUERY] [-o FILE] [--limit N] [-q] [--all]
```
- QUERY: Gmail search syntax (default: `in:inbox`)
- Outputs current labels for each thread
- Default output: `relabel.tsv`
- `--all` includes system labels (normally filtered)

**plan**
```
gax mail relabel plan FILE [-o PLAN]
```
- Reads TSV with desired state
- Re-fetches current state from Gmail
- Computes diff (add/remove labels)
- Validates label names exist
- Default output: `relabel.plan.yaml`

**apply**
```
gax mail relabel apply PLAN [-y]
```
- Reads plan file
- Shows summary, confirms (unless `-y`)
- Executes changes via Gmail API

## Consequences

- **State-based**: fetch shows what IS, user declares what SHOULD BE
- **Diff-based**: plan computes minimal changes
- **Token efficient**: TSV format, optional comments
- **AI friendly**: clear input/output contracts
- **Reversible**: plan shows exact changes before apply
- **No magic**: explicit special labels, clear semantics
