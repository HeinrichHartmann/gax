# ADR 020: Mail Command Structure Cleanup

**Status:** Accepted
**Date:** 2026-03-25

## Context

The original `gax mail` command structure grew organically and had several issues:

1. **Deep nesting** - `gax mail thread clone`, `gax mail list clone` felt verbose
2. **Inconsistent with clone/checkout pattern** - `mail list checkout` fetched full threads (not exploding a local file)
3. **Conflation** - Mixed individual threads, thread collections, drafts, labels, and filters under one namespace
4. **Unclear scope** - `gax mail label` and `gax mail filter` weren't clearly Gmail-specific in name

The key insight: **thread collections (searches/mailbox) are fundamentally different from individual threads**.

## Decision

### New Flat Structure

Flatten all mail-related commands to top-level with clear semantic separation:

```bash
gax mailbox       # Thread collections/searches
gax mail          # Individual threads
gax draft         # Draft operations
gax mail-label    # Gmail label management
gax mail-filter   # Gmail filter management
```

### Mailbox vs Mail

**`mailbox` = collections of threads (search results)**
- Lists/searches produce metadata (thread_id, from, subject, labels)
- `clone` saves the metadata index
- `fetch` retrieves full thread content based on query
- Supports bulk operations (relabeling)

**`mail` = individual threads**
- Single thread operations
- Pull/clone a specific thread URL
- Creates multipart `.mail.gax` files
- Can be exploded to individual messages

### Mail-Label and Mail-Filter

Prefix with `mail-` to clarify scope:
- `gax mail-label` - clearly Gmail labels (not filesystem labels, etc.)
- `gax mail-filter` - clearly Gmail filters (not other filter types)
- Flattened to top-level (no nesting under `mail`)

## Command Details

### gax mailbox

```bash
# List/search (TSV to stdout)
gax mailbox                           # Show inbox
gax mailbox -q "from:alice"           # Search query
gax mailbox --limit 50                # More results

# Clone - save metadata
gax mailbox clone                     # → mailbox.gax (inbox metadata)
gax mailbox clone -q "label:urgent"   # → mailbox.gax (filtered)
gax mailbox clone -o urgent.gax       # Custom output

# Fetch - retrieve full threads
gax mailbox fetch                     # → mailbox.gax.d/ (full threads)
gax mailbox fetch -q "from:alice"     # → mailbox.gax.d/ (filtered)
gax mailbox fetch -o alice.gax.d/     # Custom output

# Bulk relabeling workflow
gax mailbox clone -o inbox.gax        # Save metadata
# Edit inbox.gax (change labels column)
gax mailbox plan inbox.gax            # → inbox.plan.yaml
gax mailbox apply inbox.plan.yaml     # Apply label changes

# Update
gax mailbox pull inbox.gax            # Re-fetch metadata
```

**File format:**
```yaml
---
type: gax/mailbox
query: in:inbox
limit: 20
pulled: 2026-03-25T10:00:00Z
---
id	sys	cat	labels	from	subject	date	snippet
abc123	I,U		Work	alice@...	Project Update	2026-03-25	...
```

### gax mail

```bash
# Pull/clone single thread
gax mail <thread-url>                 # → thread.mail.gax
gax mail <thread-url> -o custom.mail.gax

# Update existing thread
gax mail pull thread.mail.gax

# Reply to thread
gax mail reply <thread-url>           # → Re_subject.draft.gax
gax mail reply thread.mail.gax        # From local file

# Explode thread to messages
gax explode thread.mail.gax           # → thread.mail.gax.d/
                                      #    - msg1.mail.gax
                                      #    - msg2.mail.gax
```

**File format (multipart):**
```yaml
---
type: gax/mail
thread-id: 18def...
subject: Project Update
participants: alice@..., bob@...
pulled: 2026-03-25T10:00:00Z
---
--- # Message 1
from: alice@example.com
to: bob@example.com
date: 2026-03-25T09:00:00Z
subject: Project Update
---
Message body...

--- # Message 2
...
```

### gax draft

```bash
# Create new draft
gax draft new                         # → new_draft.draft.gax
gax draft new -o reply.draft.gax

# Clone existing draft
gax draft clone <draft-id>            # → draft.gax
gax draft list                        # List all drafts (TSV)

# Update
gax draft pull draft.gax

# Push to Gmail
gax draft push draft.gax              # Creates/updates draft in Gmail
```

### gax mail-label

```bash
# List labels
gax mail-label list                   # TSV output

# Clone - save all labels
gax mail-label clone                  # → mail-labels.gax
gax mail-label clone -o labels.gax

# Edit workflow
gax mail-label clone -o labels.gax
# Edit labels.gax (add/remove/rename labels)
gax mail-label plan labels.gax        # → labels.plan.yaml
gax mail-label apply labels.plan.yaml # Create/delete/update labels

# Update
gax mail-label pull labels.gax
```

### gax mail-filter

```bash
# List filters
gax mail-filter list                  # TSV output

# Clone - save all filters
gax mail-filter clone                 # → mail-filters.gax
gax mail-filter clone -o filters.gax

# Edit workflow
gax mail-filter clone -o filters.gax
# Edit filters.gax (add/remove filters)
gax mail-filter plan filters.gax      # → filters.plan.yaml
gax mail-filter apply filters.plan.yaml # Create/delete filters

# Update
gax mail-filter pull filters.gax
```

## Rationale

### Why Separate mailbox from mail?

**Different data types:**
- Mailbox: metadata index (thread_id, from, subject, labels)
- Mail: full message content (headers, body, attachments)

**Different workflows:**
- Mailbox: search → bulk relabel → apply
- Mail: pull thread → read → reply

**Different performance:**
- Mailbox: fast metadata fetch
- Mail: slow full content download

**Breaks clone/checkout pattern otherwise:**
- `mail list checkout` fetched full threads (not exploding local file)
- Separating mailbox makes `fetch` clearly "go get from server"
- `mail` can follow the pattern: clone → explode → individual messages

### Why Flatten?

**Consistency with ADR 019:**
- Top-level resources: `cal`, `sheet`, `contacts`, `mailbox`, `mail`
- No deep nesting

**Discoverability:**
- `gax <TAB>` shows all resources
- `gax mailbox <TAB>` shows mailbox commands
- `gax mail <TAB>` shows mail commands

**Clarity:**
- `gax mail <url>` is simple and direct
- No need for `gax mail thread clone <url>`

### Why mail-label and mail-filter?

**Scope clarity:**
- `gax label` could mean filesystem labels, issue labels, etc.
- `gax mail-label` unambiguous - Gmail labels only

**Namespace organization:**
- Groups mail-related commands together
- `gax mail<TAB>` shows: `mail`, `mailbox`, `mail-label`, `mail-filter`

**Future expansion:**
- Could add `gax calendar-label`, `gax sheet-label` if needed
- Pattern established

### Why fetch instead of checkout for mailbox?

**Semantic accuracy:**
- `checkout` implies "explode a local file" (per ADR 019)
- `fetch` implies "go get from server"
- Mailbox doesn't explode - it queries and downloads

**Clarity:**
- `mailbox clone` = save metadata
- `mailbox fetch` = retrieve full content
- Different operations, different names

## Migration Notes

### Breaking Changes

Old structure → New structure:

```bash
# Threads
gax mail thread clone <url>     → gax mail <url>
gax mail thread pull <file>     → gax mail pull <file>
gax mail thread reply <url>     → gax mail reply <url>

# Mailbox
gax mail list                   → gax mailbox
gax mail list clone             → gax mailbox clone
gax mail list checkout          → gax mailbox fetch
gax mail list plan              → gax mailbox plan
gax mail list apply             → gax mailbox apply
gax mail list pull              → gax mailbox pull

# Draft (unchanged commands, just flattened)
gax mail draft new              → gax draft new
gax mail draft clone            → gax draft clone
gax mail draft pull             → gax draft pull
gax mail draft push             → gax draft push
gax mail draft list             → gax draft list

# Labels
gax mail label list             → gax mail-label list
gax mail label clone            → gax mail-label clone
gax mail label pull             → gax mail-label pull
gax mail label plan             → gax mail-label plan
gax mail label apply            → gax mail-label apply

# Filters
gax mail filter list            → gax mail-filter list
gax mail filter clone           → gax mail-filter clone
gax mail filter pull            → gax mail-filter pull
gax mail filter plan            → gax mail-filter plan
gax mail filter apply           → gax mail-filter apply
```

### File Type Changes

```bash
# Old
*.mail.gax         # Both individual threads and lists (ambiguous)

# New
*.mail.gax         # Individual threads only
*.mailbox.gax      # Thread metadata lists
*.mailbox.gax.d/   # Fetched full threads
*.draft.gax        # Drafts
mail-labels.gax    # Labels
mail-filters.gax   # Filters
```

## Implementation Status

- ⏳ Rename `mail thread` → `mail`
- ⏳ Rename `mail list` → `mailbox`
- ⏳ Rename `mail list checkout` → `mailbox fetch`
- ⏳ Flatten `mail draft` → `draft`
- ⏳ Rename `mail label` → `mail-label`
- ⏳ Rename `mail filter` → `mail-filter`

## Related ADRs

- ADR 002: Multipart Markdown Format
- ADR 004: Mail Sync
- ADR 006: Mail Draft Sync
- ADR 008: Gmail Filters and Labels IaC
- ADR 009: Mail Relabel
- ADR 019: Clone vs Checkout Pattern
