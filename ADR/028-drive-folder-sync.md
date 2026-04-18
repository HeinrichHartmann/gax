# ADR 028: Google Drive Folder Sync

## Status

Proposed

## Context

gax supports single-file Google Drive operations via `gax file clone/pull/push` — download a file, track it with a sidecar `.gax.md` file, and push changes back. There is no way to sync an entire Drive folder.

Teams use shared Drive folders as document repositories. Being able to clone a folder locally enables:

- Bidirectional sync of shared project assets
- LLM agents reading and modifying folder contents through local files
- Offline access with tracked provenance

Other gax resources already have folder checkout patterns: Sheet creates `.sheet.gax.md.d/` folders with per-tab files, Doc creates `.doc.gax.md.d/` folders. Drive folder sync follows the same pattern.

## Decision

### Folder checkout model

A Drive folder becomes a local directory with the suffix `.drive.gax.md.d/`. Each file in the folder keeps a per-file sidecar (the existing `.gax.md` tracking file pattern). A `.gax.yaml` metadata file at the root tracks the Drive folder ID.

```
Project_Assets.drive.gax.md.d/
├── .gax.yaml                      # folder metadata
├── logo.png                       # actual file
├── logo.png.gax.md                # sidecar (file_id, size, etc.)
├── budget.xlsx
├── budget.xlsx.gax.md
└── notes/                         # subfolder (recursive)
    ├── meeting.pdf
    ├── meeting.pdf.gax.md
    └── ideas.txt
    └── ideas.txt.gax.md
```

### .gax.yaml format

```yaml
type: gax/drive-checkout
folder_id: 1AbCdEfGhIjKlMnOpQrStUvWxYz
url: https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
title: Project Assets
recursive: false           # true if checked out with -R
checked_out: 2026-04-18T10:00:00Z
```

### Resource classes

Following the dual-class pattern (File/Folder like SheetTab/Sheet, Event/Cal):

- **`File(Resource)`** — already exists. Single file. Unchanged.
- **`Folder`** — new class. Not a Resource subclass (collection manager, like Mailbox and Sheet). Methods: `checkout`, `pull`, `diff`, `push`.

### Operations

**Checkout** (`gax file checkout <folder-url> [-R]`):
1. Extract folder ID, fetch folder name
2. Create `<name>.drive.gax.md.d/`, write `.gax.yaml`
3. List files in folder (flat by default, recursive with `-R`)
4. Download each file + create sidecar
5. With `-R`: create local subdirectories for Drive subfolders
6. Skip files that already exist locally (incremental)

**Pull** (`gax pull folder.drive.gax.md.d`):
1. List remote folder contents
2. Compare against local sidecars
3. Download new files, refresh changed files
4. Report additions/deletions

**Diff** (`gax file diff folder.drive.gax.md.d`):
Compare local folder structure against remote. Report new remote files, deleted files, size/timestamp changes, new local files (no sidecar).

**Push** (`gax push folder.drive.gax.md.d`):
1. Walk local tree
2. Files with sidecar → update on Drive if changed
3. Files without sidecar → upload as new to Drive folder
4. Show plan, confirm, execute

### API additions

```python
def extract_folder_id(url_or_id: str) -> str:
    """Extract folder ID from Drive folder URL."""

def list_folder(folder_id: str, *, recursive: bool = True) -> list[dict]:
    """List files in a Drive folder.
    
    Returns flat list of dicts, each with:
      id, name, mimeType, size, path (relative), is_folder
    Handles pagination internally.
    """
```

### CLI commands

```
gax file checkout <folder-url> [-o FOLDER] [-R]   # new command
gax pull <folder>.drive.gax.md.d                   # works via unified pull
gax push <folder>.drive.gax.md.d                   # works via unified push
```

`-R` enables recursive traversal of subfolders. Without it, only files directly in the folder are cloned. The flag is recorded in `.gax.yaml` so subsequent `pull` operations respect the original depth.

The unified `gax pull`/`gax push` dispatch in `cli_helper.py` gains a `gax/drive-checkout` case.

### Google Workspace files

When a file in the Drive folder is a Google Workspace type, clone it using the native gax resource instead of binary download:

| Drive MIME type | gax resource | Local file |
|----------------|-------------|------------|
| `application/vnd.google-apps.document` | `Doc().clone()` | `<name>.doc.gax.md` |
| `application/vnd.google-apps.spreadsheet` | `SheetTab().clone()` | `<name>.sheet.gax.md` |
| `application/vnd.google-apps.form` | `Form().clone()` | `<name>.form.gax.md` |
| `application/vnd.google-apps.presentation` | skip + warning | — |
| everything else | `File().clone()` | `<name>.<ext>` + sidecar |

This means a checkout folder can contain a mix of binary files (with sidecars) and gax resource files (self-tracking, no sidecar needed). Pull/push dispatches per-file based on extension, same as the unified `gax pull` already does.

## Edge cases

**Filename conflicts**: Drive allows duplicate filenames in the same folder; local filesystems do not. Resolve by appending `_<id[:8]>` suffix to the second file.

**Large folders**: Use progress spinner. Consider a `--limit` flag for initial testing.

**Permissions**: Shared folders may contain files the user cannot download. Catch errors per-file and continue.

## Consequences

### Positive

- Enables full folder sync for Drive, completing gax's coverage of Google Workspace
- Per-file sidecars allow individual file operations (`gax file push report.pdf`) within a checked-out folder
- Follows established checkout patterns (Sheet, Doc) — no new concepts for users to learn

### Negative

- Sidecar files double the file count in the local tree (each file gets a `.gax.md` companion)
- Recursive folder listing requires multiple API calls (one per subfolder)
- Binary files cannot be meaningfully diffed — diff is structural only (new/deleted/size changes)

### Neutral

- Google Workspace files in the folder are cloned via their native gax resource — the checkout folder becomes a mixed tree of binary files (with sidecars) and gax resource files (self-tracking)

## Alternatives considered

### 1. Manifest-only tracking (no per-file sidecars)

Store all file IDs in `.gax.yaml` as a files list. Cleaner local tree but breaks the established File pattern — can't use `gax file push` on individual files within the folder.

**Rejected**: Consistency with existing File resource is more valuable than a cleaner directory listing.

### 2. Recursive by default

Always recurse into subfolders. Matches user expectations for "clone everything."

**Rejected**: Flat by default is safer — avoids accidentally downloading large nested trees. Users opt in to recursion with `-R`.

### 3. Export Workspace files as PDF/docx

When encountering a Google Doc or Sheet in a Drive folder, export it as PDF or docx.

**Rejected**: gax already has native resource handlers for these types. Using them produces editable, round-trippable files instead of static exports.
