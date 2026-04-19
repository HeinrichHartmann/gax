# ADR 031: Google Tasks Sync

## Status

Proposed

## Context

gax covers Docs, Sheets, Forms, Gmail, Calendar, Contacts, and Drive. Google Tasks is the remaining core productivity API. Tasks are lightweight to-do items organized in task lists, with optional due dates, notes, and subtask nesting.

Use cases:

- LLM agents managing to-do lists through local files
- Syncing task lists for offline review and batch editing
- Round-tripping task changes (title, notes, status, due date) back to Google

The Google Tasks API is simple compared to Calendar or Docs. A task has ~10 fields. Task lists are flat containers (like calendars). Tasks within a list can be nested one level (parent/subtask).

## Decision

### Conceptual Mapping

| Concept | Calendar | Tasks |
|---------|----------|-------|
| Container | Calendar | Task List |
| Item | Event | Task |
| List containers | `cal calendars` | `task lists` |
| List items | `cal list` | `task list` |
| Item resource | `Event(Resource)` | `Task(Resource)` |
| Nesting | N/A | Subtasks (one level) |

### Data Model

```python
@dataclass
class TaskItem:
    id: str
    tasklist: str              # task list ID
    source: str                # URL to tasks.google.com
    synced: str                # ISO timestamp
    title: str
    status: str = "needsAction"  # needsAction | completed
    due: str = ""              # ISO date (date only, no time)
    notes: str = ""            # plain text description
    completed: str = ""        # ISO timestamp when completed
    parent: str = ""           # parent task ID (for subtasks)
    position: str = ""         # ordering position within list
    updated: str = ""          # ISO timestamp, last modified
```

### File Format

Single task (`.task.gax.yaml`):

```yaml
---
type: gax/task
id: MTIzNDU2Nzg5
tasklist: MDExMjIzMzQ0NQ
source: https://tasks.google.com/task/...
synced: 2026-04-19T10:00:00Z
---
title: Buy groceries
status: needsAction
due: 2026-04-21
notes: |
  Milk, eggs, bread.
  Check if we need coffee filters.
```

The YAML header contains only gax plumbing (type, id, source, synced). The YAML body contains the actual task data (title, status, due, notes). This separates sync metadata from user-editable content.

The `.gax.yaml` extension signals that both header and body are YAML, distinguishing it from `.gax.md` files where the body is markdown.

Task list file supports two formats via `--format md|yaml`:

**Markdown format** (default, `.tasks.gax.md`):

```markdown
---
type: gax/task-list
id: MDExMjIzMzQ0NQ
source: https://tasks.google.com/...
synced: 2026-04-19T10:00:00Z
title: Work
---
- [ ] Buy groceries `MTIzNDU2ODA5` due:2026-04-21
  - [ ] Milk `MTIzNDU2ODE5`
  - [ ] Bread `MTIzNDU2ODI5`
- [x] Write ADR `MTIzNDU2Nzk5` due:2026-04-20
- [ ] Review PR #38 `MTIzNDU2Nzg5`
```

Scannable, GitHub-rendered checkboxes. Pushable for quick operations: check off, rename, add, reorder, set due date. Does not include notes or timestamps -- those live in individual task files.

Parsing rules:
- `- [ ]` / `- [x]` = status (needsAction / completed)
- Backtick-wrapped = task ID
- `due:YYYY-MM-DD` = due date
- Indented items = subtasks
- No ID = new task (create on push)

**YAML format** (`--format yaml`, `.tasks.gax.yaml`):

```yaml
---
type: gax/task-list
id: MDExMjIzMzQ0NQ
source: https://tasks.google.com/...
synced: 2026-04-19T10:00:00Z
title: Work
---
- title: Buy groceries
  id: MTIzNDU2ODA5
  status: needsAction
  due: 2026-04-21
  updated: 2026-04-18T14:30:00Z
  notes: |
    Milk, eggs, bread.
    Check if we need coffee filters.
  subtasks:
    - title: Milk
      id: MTIzNDU2ODE5
      status: needsAction
    - title: Bread
      id: MTIzNDU2ODI5
      status: needsAction

- title: Write ADR
  id: MTIzNDU2Nzk5
  status: completed
  due: 2026-04-20
  completed: 2026-04-19T15:30:00Z
```

Full-fidelity format with notes, timestamps, and all fields. Pushable for detailed edits. Extension is `.tasks.gax.yaml` to reflect the YAML body.

### CLI Structure

Follows the Calendar pattern (`cal` group with `event` subgroup):

```
gax task
+-- lists                                # List available task lists
+-- list [--tasklist NAME] [--all]       # View tasks (default: incomplete only)
+-- clone <tasklist-url> [--format]      # Clone task list as single file
+-- checkout <tasklist-url>              # Checkout as folder of individual tasks
+-- pull <file-or-folder>                # Refresh
|
+-- new [--tasklist NAME] "Title"        # Create new task
+-- pull <file>                          # Refresh single task
+-- diff <file>                          # Diff single task
+-- push <file>                          # Push changes
+-- done <file>                          # Mark task completed
+-- delete <file>                        # Delete task
```

**`gax task lists`** -- List available task lists (like `cal calendars`).

**`gax task list`** -- Show tasks from a task list. Default: show only incomplete tasks. `--all` includes completed.

**`gax task clone <url> [--format md|yaml]`** -- Clone task list as single file. Default `md` creates `.tasks.gax.md` with checkbox body. `yaml` creates `.tasks.gax.yaml` with full-fidelity YAML body.

**`gax task checkout <url>`** -- Create `.tasks.gax.md.d/` folder with individual `.task.gax.yaml` files.

**`gax task new "Title"`** -- Create a new task on Google and write local `.task.gax.yaml` file. Uses `--tasklist` to select list (default: first/primary list).

**`gax task done <file>`** -- Shortcut for setting `status: completed` and pushing. Convenience command since this is the most common mutation.

### Checkout Layout

```
Work.tasks.gax.md.d/
+-- .gax.yaml
+-- Buy_groceries.task.gax.yaml
+-- Write_ADR.task.gax.yaml
+-- Milk.task.gax.yaml
+-- Bread.task.gax.yaml
```

Subtasks are kept flat in the folder (not nested directories). The `parent` field in each file preserves the hierarchy. This avoids the complexity of nested tab directories for a single level of nesting.

`.gax.yaml`:

```yaml
type: gax/task-checkout
tasklist_id: MDExMjIzMzQ0NQ
url: https://tasks.google.com/...
title: Work
checked_out: 2026-04-19T10:00:00Z
```

### Resource Classes

Two classes following the Calendar pattern:

**`TaskList`** -- Collection resource. Methods: `clone`, `pull`, `checkout`, `pull` (folder), `tab_list` (renamed: `lists`).

**`Task(Resource)`** -- Single task. Methods: `clone`, `new`, `pull`, `diff`, `push`, `delete`.

### Diff Format

Field-by-field diff like Calendar events:

```
~ title: "Review PR #38" -> "Review PR #38 — nested tabs"
~ status: "needsAction" -> "completed"
+ completed: "2026-04-19T15:30:00Z"
```

### Pushable Fields

| Field | Editable | Notes |
|-------|----------|-------|
| `id` | No | Server-assigned |
| `tasklist` | No | Cannot move tasks between lists via API |
| `source` | No | Generated URL |
| `synced` | No | Updated on pull |
| `title` | Yes | |
| `status` | Yes | needsAction or completed |
| `due` | Yes | Date only (no time component) |
| `notes` | Yes | In YAML body |
| `completed` | No | Set automatically when status changes |
| `parent` | Yes | Can reparent a subtask |
| `position` | Yes | Reorder within list |

### OAuth Scope

```python
"https://www.googleapis.com/auth/tasks"  # Read-write
```

Added to `auth.py` alongside existing scopes. Users need to re-authenticate once.

### API Helpers

```python
def get_tasks_service():
    """Build Tasks API v1 service."""

def list_tasklists(*, service=None) -> list[dict]:
    """List all task lists."""

def list_tasks(tasklist_id: str, *, show_completed=False, service=None) -> list[dict]:
    """List tasks in a task list. Handles pagination."""

def get_task(tasklist_id: str, task_id: str, *, service=None) -> dict:
    """Get a single task."""

def create_task(tasklist_id: str, body: dict, *, service=None) -> dict:
    """Create a task. Returns created task dict."""

def update_task(tasklist_id: str, task_id: str, body: dict, *, service=None) -> dict:
    """Update a task."""

def delete_task(tasklist_id: str, task_id: str, *, service=None) -> None:
    """Delete a task."""

def complete_task(tasklist_id: str, task_id: str, *, service=None) -> dict:
    """Mark task completed (convenience)."""
```

### URL Patterns

Google Tasks URLs are not as standardized as other Google products. Support:

- `https://tasks.google.com/task/<id>`
- Task list IDs passed directly via `--tasklist`
- Fallback: use default/primary task list

## Edge Cases

**Subtask ordering**: The Tasks API returns subtasks in `position` order under their parent. On pull, subtasks are placed after their parent in the TSV listing. On push, `position` and `parent` fields are respected.

**Completed tasks**: By default, `task list` and `clone` exclude completed tasks (API default). `--all` includes them. Completed tasks have `status: completed` and a `completed` timestamp.

**Empty notes**: If the task has no notes, the `notes` key is omitted from the YAML body.

**Task list deletion**: Not supported via CLI. Task lists are containers managed through the Google UI.

## Consequences

### Positive

- Completes gax coverage of core Google productivity APIs
- Simple resource with few fields -- low implementation complexity
- Follows established Calendar patterns -- no new concepts
- `done` command provides fast workflow for the most common operation
- TSV list format enables quick scanning and AI parsing

### Negative

- New OAuth scope requires re-authentication
- Google Tasks URLs are not well-standardized -- ID-based addressing may be needed more than URL-based
- Tasks API v1 has limited features (no labels, no priorities, no recurring tasks)

### Neutral

- Subtasks are flat in checkout folders (parent field preserves hierarchy without directory nesting)
- Task positions are opaque strings -- reordering requires reading current positions first

## Alternatives Considered

### 1. Nested directories for subtasks

Put subtasks in a subdirectory under the parent task.

**Rejected**: Subtasks are only one level deep and have minimal content. Directory nesting adds complexity for little benefit. The `parent` field is sufficient.

### 2. Single format for task lists

Use only YAML or only markdown for the list file.

**Rejected**: Markdown checkboxes are ideal for quick scanning and simple edits (check off, rename). YAML is needed for full-fidelity round-trips (notes, timestamps). Both formats are supported via `--format md|yaml`.

### 3. Skip task lists, work only with individual tasks

Only support single-task operations, no list/checkout.

**Rejected**: Most users think in terms of task lists, not individual tasks. The list view is essential for scanning and batch operations.

## References

- ADR 007: Calendar Sync (command structure reference)
- ADR 022: Simplified CLI Model
- ADR 026: Clone file / Checkout directory
- Google Tasks API: https://developers.google.com/tasks/reference/rest
