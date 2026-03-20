# ADR 004: Gmail Sync

## Status

Proposed

## Context

Email is a key communication channel. We want to archive and reference email threads locally using the same multipart format as Google Docs.

Key differences from Docs/Sheets:
- Emails are **immutable** (no push back)
- Emails come in **threads** (conversations)
- Emails have **attachments** (binary files)
- Gmail uses **labels** instead of folders

## Decision

### Core Concepts

1. **Thread = Multipart Markdown**
   - Each thread becomes a `.mail.gax` file
   - Each message in the thread is a section
   - Single email = thread with one section

2. **No Push**
   - Email is read-only archival
   - No sync back to Gmail

3. **Attachments → Content-Addressable Storage (CAS)**
   - Binary attachments stored in `~/.gax/store/`
   - Referenced in markdown via `file://` URLs
   - Deduplication via content hashing

4. **Labels → Folders**
   - Gmail labels map to local directories
   - A thread can exist in multiple folders (symlinks or copies)

### Commands

```
gax mail pull <url-or-id>            # Pull thread to .mail.gax file
gax mail list [--label LABEL]        # List threads (future)
gax mail search <query>              # Search threads (future)
```

### File Format

Uses multipart YAML-markdown (ADR 002). Each message is a section:

```
---
title: Re: Project Update
source: https://mail.google.com/mail/u/0/#inbox/abc123
time: 2026-03-20T10:00:00Z
thread_id: abc123
section: 1
section_title: From alice@example.com
from: alice@example.com
to: bob@example.com
date: 2026-03-15T09:30:00Z
---
Hi Bob,

Here's the project update...

Best,
Alice
---
title: Re: Project Update
source: https://mail.google.com/mail/u/0/#inbox/abc123
time: 2026-03-20T10:00:00Z
thread_id: abc123
section: 2
section_title: From bob@example.com
from: bob@example.com
to: alice@example.com
date: 2026-03-15T10:45:00Z
attachments:
  - name: report.pdf
    size: 245678
    url: file://~/.gax/store/blob/sha256-a1b2c3...
---
Thanks Alice,

Please find the report attached.

Bob
```

### Attachment Storage (CAS)

Attachments are stored in a content-addressable store:

```
~/.gax/store/
  blob/
    sha256-a1b2c3d4...      # Raw file content
    sha256-e5f6g7h8...
  meta/
    sha256-a1b2c3d4.json    # Metadata (original name, mime type, etc.)
  ref/
    report.pdf -> ../blob/sha256-a1b2c3d4...   # Named reference (symlink)
```

**Metadata file (`meta/sha256-xxx.json`):**
```json
{
  "hash": "sha256-a1b2c3d4...",
  "size": 245678,
  "mime_type": "application/pdf",
  "original_name": "report.pdf",
  "imported_at": "2026-03-20T10:00:00Z",
  "source_message_id": "msg123"
}
```

**Benefits:**
- Deduplication: Same attachment in multiple emails stored once
- Integrity: Hash verifies content
- Offline access: Attachments available locally

### URL Parsing

`gax mail pull` accepts:
- Full Gmail URL: `https://mail.google.com/mail/u/0/#inbox/abc123`
- Message ID: `abc123`
- Thread ID: `thread-abc123`

### Output Location

Default: `<Subject>_<thread-id>.mail.gax` in current directory.

With `--output-dir`: Write to specified directory.

### Auth Scope

Add to `auth.py`:
```python
"https://www.googleapis.com/auth/gmail.readonly"
```

## Implementation

**Module structure:**
```
gax/
  mail.py              # Mail commands + Gmail API
  store.py             # CAS blob storage (shared utility)
```

**Core functions:**

```python
def pull_thread(thread_id: str) -> list[Section]:
    """Fetch thread from Gmail API."""

def store_attachment(data: bytes, metadata: dict) -> str:
    """Store attachment in CAS, return file:// URL."""

def extract_thread_id(url: str) -> str:
    """Extract thread ID from Gmail URL."""
```

## Consequences

**Positive:**
- Threads archived as self-contained markdown
- Attachments deduplicated and available offline
- Consistent with doc/sheet multipart format
- Labels provide natural organization

**Negative:**
- Read-only (no compose/reply)
- Attachments require local storage space
- Gmail API quota limits for bulk operations

## Future Extensions

- `gax mail send` - Compose and send (with attachment upload)
- `gax mail reply <file>` - Reply to archived thread
- `gax mail sync --label LABEL` - Sync all threads with label

## References

- ADR 002: Multipart YAML-Markdown Format
- Gmail API: https://developers.google.com/gmail/api
