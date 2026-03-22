# ADR 006: Mail Draft Sync

## Status

Proposed

## Context

ADR 004 established Gmail sync as read-only archival. However, composing emails locally has clear benefits:

- **Local-first workflow**: Write emails in your editor, sync when ready
-  No automated sending. Prefer flow, where drafts are validated and send through UI
- **Markdown authoring**: Natural format for text emails

This extends ADR 004 with bidirectional draft sync.

## Decision

### Core Concepts

1. **Draft = Single-section Markdown**
   - Each draft is a `.draft.gax` file
   - Uses multipart format (ADR 002) but typically single section
   - Headers contain recipient, subject, and tracking IDs

2. **Push/Pull Workflow**
   - `push`: Local file → Gmail Draft
   - `pull`: Gmail Draft → Local file
   - Similar to `doc tab push/pull` (ADR 005)

3. **Separate from .mail.gax**
   - `.mail.gax` = read-only archived threads
   - `.draft.gax` = mutable drafts with push/pull

### File Format

```yaml
---
draft_id: r-1234567890123456789   # Empty until first push
subject: Meeting Notes
to: alice@example.com
cc: team@example.com              # Optional
bcc: manager@example.com          # Optional
thread_id: 18f1234567890abc       # Optional: for replies
in_reply_to: msg-xyz789           # Optional: message being replied to
source: https://mail.google.com/mail/u/0/#drafts/...
time: 2026-03-22T10:00:00Z
---
Hi Alice,

Here are my thoughts on the project...

Best,
Bob
```

### Commands

**Thread operations** (at `mail` level):
```
gax mail reply <file|url> [-o <file>]     # Reply to thread → .draft.gax
```

**Draft operations** (under `mail draft`):
```
gax mail draft new [--to <email>] [--subject <text>] [-o <file>]
gax mail draft clone <id|url> [-o <file>]
gax mail draft list [--limit N]
gax mail draft push <file> [-y]
gax mail draft pull <file>
```

#### reply (mail level)

Create a reply draft from a thread:

```bash
gax mail reply Project_Update.mail.gax
gax mail reply "https://mail.google.com/mail/u/0/#inbox/abc123"
gax mail reply thread.mail.gax -o my_reply.draft.gax
```

Extracts from the thread:
- `thread_id` for threading
- Last message's `from` → `to`
- Last message's `message_id` → `in_reply_to`
- Subject with "Re: " prefix (if not already)

Creates: `Re_<subject>.draft.gax`

#### new

Create a new draft from scratch:

```bash
gax mail draft new
gax mail draft new --to alice@example.com --subject "Hello"
gax mail draft new -o my_draft.draft.gax
```

Prompts for `to` and `subject` if not provided.

Creates: `<subject>.draft.gax`

#### clone

Clone an existing draft from Gmail:

```bash
gax mail draft clone r-1234567890123456789
gax mail draft clone "https://mail.google.com/mail/u/0/#drafts/..."
gax mail draft clone r-1234567890 -o my_draft.draft.gax
```

Creates: `<subject>.draft.gax`

#### list

List Gmail drafts (TSV output):

```bash
gax mail draft list
gax mail draft list --limit 50
```

Output:
```
draft_id	thread_id	date	to	subject
r-1234567890	18f123abc	2026-03-20	alice@example.com	Re: Project
r-9876543210		2026-03-21	bob@example.com	Meeting Notes
```

#### push

Push local draft to Gmail:

```bash
gax mail draft push my_draft.draft.gax      # Shows diff, prompts y/n
gax mail draft push my_draft.draft.gax -y   # Skip confirmation
```

**First push** (empty `draft_id`):
1. Creates draft via `drafts.create()`
2. Updates local file with `draft_id`, `source`, `time`

**Subsequent pushes** (has `draft_id`):
1. Fetches remote draft
2. Shows diff (body + header changes)
3. Prompts for confirmation (unless `-y`)
4. Updates via `drafts.update()`

#### pull

Pull remote draft to local file:

```bash
gax mail draft pull my_draft.draft.gax
```

1. Reads `draft_id` from local file
2. Fetches draft from Gmail
3. Warns if local changes will be overwritten
4. Updates local file with remote content

### OAuth Scope

Current scope (ADR 004):
```python
"https://www.googleapis.com/auth/gmail.readonly"
```

Required for drafts:
```python
"https://www.googleapis.com/auth/gmail.compose"
```

The `gmail.compose` scope allows:
- Creating/updating/deleting drafts
- Sending emails
- Does NOT allow reading existing emails (readonly still needed)

Users must re-authenticate after scope change:
```bash
gax auth logout && gax auth login
```

### Push Workflow Detail

```
┌─────────────────────────────────────────────────────┐
│                  gax mail draft push                │
├─────────────────────────────────────────────────────┤
│  1. Parse local .draft.gax                          │
│  2. Validate required fields (to, subject)          │
│                                                     │
│  3. If draft_id empty:                              │
│     └─ drafts.create() → update local with ID       │
│                                                     │
│  4. If draft_id exists:                             │
│     ├─ drafts.get() remote                          │
│     ├─ Compare local vs remote                      │
│     ├─ Show diff                                    │
│     ├─ Prompt: "Push these changes? [y/N]"          │
│     └─ drafts.update() if confirmed                 │
│                                                     │
│  5. Update local file timestamp                     │
└─────────────────────────────────────────────────────┘
```

### Error Handling

**Draft deleted remotely:**
```
$ gax mail draft pull my_draft.draft.gax
Error: Draft r-1234567890 no longer exists in Gmail.
The draft may have been sent or deleted.
```

**Missing required fields:**
```
$ gax mail draft push incomplete.draft.gax
Error: 'to' field is required
```

## Implementation

**Module structure:**
```
gax/
  draft.py             # Draft commands + Gmail Drafts API
  mail.py              # Existing mail sync (unchanged)
  auth.py              # Add gmail.compose scope
```

**Core functions:**
```python
@dataclass
class DraftConfig:
    draft_id: str = ""
    subject: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    thread_id: str = ""      # For replies
    in_reply_to: str = ""    # Message being replied to
    source: str = ""
    time: str = ""

def parse_draft(content: str) -> tuple[DraftConfig, str]:
    """Parse .draft.gax file into config and body."""

def format_draft(config: DraftConfig, body: str) -> str:
    """Format config and body as .draft.gax content."""

def create_draft(config: DraftConfig, body: str) -> dict:
    """Create new draft in Gmail."""

def update_draft(draft_id: str, config: DraftConfig, body: str) -> dict:
    """Update existing draft in Gmail."""

def get_draft(draft_id: str) -> tuple[DraftConfig, str]:
    """Fetch draft from Gmail."""
```

## Consequences

**Positive:**
- Local-first email composition
- Version control for drafts
- Offline editing capability
- Consistent with doc/sheet push/pull patterns

**Negative:**
- Requires broader OAuth scope (`gmail.compose`)
- Users must re-authenticate after upgrade
- No attachment support in V1

## Future Extensions

- `gax mail draft diff <file>` - Show local vs remote without pushing
- `gax mail draft delete <file>` - Delete remote draft
- `gax mail draft send <file>` - Send draft as email (if needed)
- Attachment support via CAS (like mail clone)

## References

- ADR 002: Multipart YAML-Markdown Format
- ADR 004: Gmail Sync (read-only)
- ADR 005: CLI Structure and Tab Operations
- Gmail Drafts API: https://developers.google.com/gmail/api/reference/rest/v1/users.drafts
