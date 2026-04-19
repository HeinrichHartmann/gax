"""Google Tasks sync for gax.

Resource module -- follows the gcal.py reference pattern.

Task list viewing and task editing (ADR 031).

Module structure
================

  Data class           -- TaskItem
  API helpers          -- service, list/get/create/update/delete tasks
  Inverse pairs        -- api_to_task / task_to_api_body
  Task file format     -- task_to_yaml / yaml_to_task (split header/body YAML)
  List format          -- markdown checkboxes and YAML list formats
  Resolution helpers   -- resolve_tasklist_id
  TaskList             -- collection resource (lists, clone, pull, checkout)
  Task(Resource)       -- single task resource (clone, new, pull, diff, push, done, delete)

Design decisions
================

Same conventions as gcal.py (see its docstring for full rationale).
Additional notes specific to tasks:

  Split YAML format: header has gax metadata (type, id, tasklist, source, synced),
  body has user-editable task data (title, status, due, notes). Extension: .task.gax.yaml.

  Two list formats via --format:
    md (default)  -- markdown checkboxes, .tasks.gax.md
    yaml          -- full-fidelity YAML list, .tasks.gax.yaml

  Task push handles both create (no ID) and update (has ID).
  After creating, the local file is updated with the new task ID.

  done() is a convenience shortcut: set status=completed + push.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from .resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# Data class
# =============================================================================


@dataclass
class TaskItem:
    """A Google Tasks task."""

    id: str
    tasklist: str  # task list ID
    source: str  # URL
    synced: str  # ISO timestamp
    title: str
    status: str = "needsAction"  # needsAction | completed
    due: str = ""  # ISO date (YYYY-MM-DD, no time)
    notes: str = ""
    completed: str = ""  # ISO timestamp when completed
    parent: str = ""  # parent task ID (subtasks)
    position: str = ""  # ordering within list
    updated: str = ""  # ISO timestamp, last modified


# =============================================================================
# API helpers
# =============================================================================


def get_tasks_service(*, service=None):
    """Get authenticated Tasks API v1 service."""
    if service is not None:
        return service
    creds = get_authenticated_credentials()
    return build("tasks", "v1", credentials=creds)


def list_tasklists(*, service=None) -> list[dict]:
    """List all task lists. Returns list of {id, title} dicts."""
    service = get_tasks_service(service=service)

    result = service.tasklists().list().execute()
    tasklists = []
    for tl in result.get("items", []):
        tasklists.append(
            {
                "id": tl["id"],
                "title": tl.get("title", tl["id"]),
            }
        )
    return tasklists


def list_tasks(
    tasklist_id: str, *, show_completed: bool = False, service=None
) -> list[dict]:
    """List tasks in a task list. Handles pagination."""
    service = get_tasks_service(service=service)

    tasks = []
    page_token = None

    while True:
        kwargs: dict[str, Any] = {
            "tasklist": tasklist_id,
            "maxResults": 100,
        }
        if show_completed:
            kwargs["showCompleted"] = True
            kwargs["showHidden"] = True
        if page_token:
            kwargs["pageToken"] = page_token

        result = service.tasks().list(**kwargs).execute()

        for task in result.get("items", []):
            tasks.append(task)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return tasks


def get_task(tasklist_id: str, task_id: str, *, service=None) -> dict:
    """Get a single task."""
    service = get_tasks_service(service=service)
    return service.tasks().get(tasklist=tasklist_id, task=task_id).execute()


def create_task(
    tasklist_id: str, body: dict, *, parent: str = "", service=None
) -> dict:
    """Create a task. Returns created task dict."""
    service = get_tasks_service(service=service)
    kwargs: dict[str, Any] = {"tasklist": tasklist_id, "body": body}
    if parent:
        kwargs["parent"] = parent
    return service.tasks().insert(**kwargs).execute()


def update_task(tasklist_id: str, task_id: str, body: dict, *, service=None) -> dict:
    """Update a task. Returns updated task dict."""
    service = get_tasks_service(service=service)
    return (
        service.tasks().update(tasklist=tasklist_id, task=task_id, body=body).execute()
    )


def delete_task(tasklist_id: str, task_id: str, *, service=None) -> None:
    """Delete a task."""
    service = get_tasks_service(service=service)
    service.tasks().delete(tasklist=tasklist_id, task=task_id).execute()


# =============================================================================
# Inverse pair: TaskItem <-> API
# =============================================================================


def api_to_task(task: dict, tasklist_id: str) -> TaskItem:
    """Convert API task dict to TaskItem dataclass."""
    task_id = task.get("id", "")

    # Due date: API returns RFC 3339 with time, store as date-only
    due_raw = task.get("due", "")
    due = due_raw[:10] if due_raw else ""

    # Completed timestamp
    completed = task.get("completed", "")

    return TaskItem(
        id=task_id,
        tasklist=tasklist_id,
        source=task.get("selfLink", ""),
        synced=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        title=task.get("title", ""),
        status=task.get("status", "needsAction"),
        due=due,
        notes=task.get("notes", ""),
        completed=completed,
        parent=task.get("parent", ""),
        position=task.get("position", ""),
        updated=task.get("updated", ""),
    )


def task_to_api_body(task: TaskItem) -> dict:
    """Convert TaskItem to API request body (for create/update)."""
    body: dict[str, Any] = {
        "title": task.title,
        "status": task.status,
    }

    if task.due:
        body["due"] = f"{task.due}T00:00:00.000Z"

    if task.notes:
        body["notes"] = task.notes

    return body


# =============================================================================
# Task file format -- split header/body YAML (.task.gax.yaml)
# =============================================================================


def task_to_yaml(task: TaskItem) -> str:
    """Serialize TaskItem to split YAML (header + body)."""
    header: dict[str, Any] = {
        "type": "gax/task",
        "id": task.id,
        "tasklist": task.tasklist,
        "source": task.source,
        "synced": task.synced,
    }

    body: dict[str, Any] = {
        "title": task.title,
        "status": task.status,
    }
    if task.due:
        body["due"] = task.due
    if task.notes:
        body["notes"] = task.notes
    if task.completed:
        body["completed"] = task.completed
    if task.parent:
        body["parent"] = task.parent
    if task.updated:
        body["updated"] = task.updated

    header_str = yaml.dump(
        header, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    body_str = yaml.dump(
        body, default_flow_style=False, allow_unicode=True, sort_keys=False
    )

    return f"---\n{header_str}---\n{body_str}"


def yaml_to_task(content: str) -> TaskItem:
    """Parse split YAML content to TaskItem."""
    if not content.startswith("---"):
        raise ValueError("Expected YAML frontmatter (---)")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid split YAML format")

    header = yaml.safe_load(parts[1])
    body = yaml.safe_load(parts[2])

    if header is None:
        raise ValueError("Empty YAML header")
    if body is None:
        body = {}

    if header.get("type") != "gax/task":
        raise ValueError(f"Expected type gax/task, got {header.get('type')}")

    return TaskItem(
        id=header.get("id", ""),
        tasklist=header.get("tasklist", ""),
        source=header.get("source", ""),
        synced=header.get("synced", ""),
        title=body.get("title", ""),
        status=body.get("status", "needsAction"),
        due=str(body["due"]) if body.get("due") else "",
        notes=body.get("notes", ""),
        completed=body.get("completed", ""),
        parent=body.get("parent", ""),
        position=body.get("position", ""),
        updated=body.get("updated", ""),
    )


# =============================================================================
# List format -- markdown checkboxes
# =============================================================================


def format_tasks_md(tasks: list[TaskItem]) -> str:
    """Format tasks as markdown checkboxes.

    Root tasks first, subtasks indented below their parent.
    Format: - [ ] Title `ID` due:YYYY-MM-DD
    """
    # Group subtasks under parents
    root_tasks = [t for t in tasks if not t.parent]
    children: dict[str, list[TaskItem]] = {}
    for t in tasks:
        if t.parent:
            children.setdefault(t.parent, []).append(t)

    lines = []
    for task in root_tasks:
        lines.append(_format_task_md_line(task, indent=0))
        for child in children.get(task.id, []):
            lines.append(_format_task_md_line(child, indent=1))

    return "\n".join(lines) + "\n" if lines else ""


def _format_task_md_line(task: TaskItem, indent: int = 0) -> str:
    """Format a single task as a markdown checkbox line."""
    prefix = "  " * indent
    check = "x" if task.status == "completed" else " "
    parts = [f"{prefix}- [{check}] {task.title}"]
    if task.id:
        parts.append(f"`{task.id}`")
    if task.due:
        parts.append(f"due:{task.due}")
    return " ".join(parts)


def parse_tasks_md(content: str) -> list[dict]:
    """Parse markdown checkbox content to list of task dicts.

    Returns list of dicts with: title, id, status, due, is_subtask.
    """
    pattern = re.compile(r"^(\s*)- \[([ xX])\] (.+)$")
    tasks = []

    for line in content.strip().split("\n"):
        m = pattern.match(line)
        if not m:
            continue

        indent = len(m.group(1))
        checked = m.group(2).lower() == "x"
        rest = m.group(3).strip()

        # Extract ID from backticks (from right)
        task_id = ""
        id_match = re.search(r"`([^`]+)`", rest)
        if id_match:
            task_id = id_match.group(1)
            rest = rest[: id_match.start()].strip()
            # Check for due: after the backtick
            after = m.group(3)[id_match.end() :].strip()
            due_match = re.search(r"due:(\S+)", after)
        else:
            due_match = re.search(r"due:(\S+)", rest)

        # Extract due date
        due = ""
        if due_match:
            due = due_match.group(1)
            if not task_id:
                rest = rest[: due_match.start()].strip()

        # Remaining text is the title
        # Remove trailing due: if it was in the rest
        title = re.sub(r"\s*due:\S+", "", rest).strip()

        tasks.append(
            {
                "title": title,
                "id": task_id,
                "status": "completed" if checked else "needsAction",
                "due": due,
                "is_subtask": indent >= 2,
            }
        )

    return tasks


# =============================================================================
# List format -- YAML
# =============================================================================


def format_tasks_yaml(tasks: list[TaskItem]) -> str:
    """Format tasks as YAML list body with nested subtasks."""
    root_tasks = [t for t in tasks if not t.parent]
    children: dict[str, list[TaskItem]] = {}
    for t in tasks:
        if t.parent:
            children.setdefault(t.parent, []).append(t)

    items = []
    for task in root_tasks:
        item = _task_to_yaml_dict(task)
        subs = children.get(task.id, [])
        if subs:
            item["subtasks"] = [_task_to_yaml_dict(s) for s in subs]
        items.append(item)

    return yaml.dump(
        items, default_flow_style=False, allow_unicode=True, sort_keys=False
    )


def _task_to_yaml_dict(task: TaskItem) -> dict:
    """Convert TaskItem to dict for YAML list format."""
    d: dict[str, Any] = {
        "title": task.title,
        "id": task.id,
        "status": task.status,
    }
    if task.due:
        d["due"] = task.due
    if task.notes:
        d["notes"] = task.notes
    if task.completed:
        d["completed"] = task.completed
    if task.updated:
        d["updated"] = task.updated
    return d


# =============================================================================
# Resolution helpers
# =============================================================================


def resolve_tasklist_id(tasklist: str | None) -> tuple[str, str]:
    """Resolve tasklist name or index to (id, title).

    Supports: name, full ID, numeric index (1-based).
    Returns first list if tasklist is None.
    """
    all_lists = list_tasklists()
    if not all_lists:
        raise ValueError("No task lists found")

    if tasklist is None:
        tl = all_lists[0]
        return tl["id"], tl["title"]

    # Try numeric index (1-based)
    try:
        idx = int(tasklist) - 1
        if 0 <= idx < len(all_lists):
            tl = all_lists[idx]
            return tl["id"], tl["title"]
    except ValueError:
        pass

    # Try by name
    for tl in all_lists:
        if tl["title"] == tasklist:
            return tl["id"], tl["title"]

    # Try by ID
    for tl in all_lists:
        if tl["id"] == tasklist:
            return tl["id"], tl["title"]

    available = ", ".join(tl["title"] for tl in all_lists)
    raise ValueError(f"Task list not found: {tasklist}. Available: {available}")


def _safe_filename(title: str) -> str:
    """Convert title to safe filename."""
    safe = re.sub(r"[^\w\s-]", "", title)[:40].strip()
    return re.sub(r"\s+", "_", safe)


# =============================================================================
# TaskList -- collection resource
# =============================================================================


class TaskList:
    """Task list collection resource."""

    name = "task-list"

    def lists(self, out) -> None:
        """List available task lists to file descriptor."""
        tasklists = list_tasklists()
        for tl in tasklists:
            out.write(f"{tl['title']}\n")
            out.write(f"  {tl['id']}\n")

    def list(
        self,
        out,
        *,
        tasklist: str | None = None,
        show_all: bool = False,
        fmt: str = "md",
    ) -> None:
        """List tasks from a task list to stdout."""
        tl_id, tl_title = resolve_tasklist_id(tasklist)
        api_tasks = list_tasks(tl_id, show_completed=show_all)
        items = [api_to_task(t, tl_id) for t in api_tasks]

        out.write(f"# {tl_title}\n")
        if fmt == "yaml":
            out.write(format_tasks_yaml(items))
        else:
            out.write(format_tasks_md(items))

    def clone(
        self,
        *,
        tasklist: str | None = None,
        output: Path | None = None,
        fmt: str = "md",
        show_all: bool = False,
    ) -> Path:
        """Clone task list to .tasks.gax.md or .tasks.gax.yaml file."""
        tl_id, tl_title = resolve_tasklist_id(tasklist)
        api_tasks = list_tasks(tl_id, show_completed=show_all)
        items = [api_to_task(t, tl_id) for t in api_tasks]

        ext = ".tasks.gax.yaml" if fmt == "yaml" else ".tasks.gax.md"
        file_path = output or Path(f"{_safe_filename(tl_title)}{ext}")
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        self._write_list_file(file_path, tl_id, tl_title, items, fmt, show_all)
        logger.info(f"Tasks: {len(items)}")
        return file_path

    def pull(self, path: Path) -> None:
        """Pull latest tasks to existing list file."""
        content = path.read_text()
        if not content.startswith("---"):
            raise ValueError("File must start with YAML header (---)")

        header = yaml.safe_load(content.split("---", 2)[1])
        tl_id = header.get("id", "")
        tl_title = header.get("title", "")
        fmt = header.get("format", "md")
        show_all = header.get("show_all", False)

        if not tl_id:
            raise ValueError("No task list ID in file header")

        api_tasks = list_tasks(tl_id, show_completed=show_all)
        items = [api_to_task(t, tl_id) for t in api_tasks]

        self._write_list_file(path, tl_id, tl_title, items, fmt, show_all)

    def checkout(
        self,
        *,
        tasklist: str | None = None,
        output: Path | None = None,
        show_all: bool = False,
    ) -> tuple[int, int]:
        """Checkout task list as folder of .task.gax.yaml files.

        Returns (cloned, skipped).
        """
        tl_id, tl_title = resolve_tasklist_id(tasklist)
        folder = output or Path(f"{_safe_filename(tl_title)}.tasks.gax.md.d")
        folder.mkdir(parents=True, exist_ok=True)

        api_tasks = list_tasks(tl_id, show_completed=show_all)
        items = [api_to_task(t, tl_id) for t in api_tasks]

        if not items:
            return 0, 0

        # Write .gax.yaml metadata
        metadata = {
            "type": "gax/task-checkout",
            "tasklist_id": tl_id,
            "title": tl_title,
            "checked_out": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        metadata_path = folder / ".gax.yaml"
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Get existing task IDs in folder
        existing_ids = set()
        for f_path in folder.glob("*.task.gax.yaml"):
            try:
                task_content = f_path.read_text()
                parsed = yaml_to_task(task_content)
                if parsed.id:
                    existing_ids.add(parsed.id)
            except Exception:
                pass

        cloned = 0
        skipped = 0

        for task in items:
            if task.id in existing_ids:
                skipped += 1
                continue

            filename = f"{_safe_filename(task.title)}.task.gax.yaml"
            file_path = folder / filename
            if file_path.exists():
                filename = f"{_safe_filename(task.title)}_{task.id[:8]}.task.gax.yaml"
                file_path = folder / filename

            content = task_to_yaml(task)
            file_path.write_text(content)
            cloned += 1
            logger.info(f"Writing {filename}")

        return cloned, skipped

    def _write_list_file(
        self,
        path: Path,
        tl_id: str,
        tl_title: str,
        items: list[TaskItem],
        fmt: str,
        show_all: bool,
    ) -> None:
        """Write task list file with header and formatted body."""
        header: dict[str, Any] = {
            "type": "gax/task-list",
            "id": tl_id,
            "title": tl_title,
            "format": fmt,
            "synced": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if show_all:
            header["show_all"] = True

        if fmt == "yaml":
            body = format_tasks_yaml(items)
        else:
            body = format_tasks_md(items)

        with open(path, "w") as f:
            f.write("---\n")
            yaml.dump(
                header,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            f.write("---\n")
            f.write(body)


# =============================================================================
# Task(Resource) -- single task (clone/new/pull/diff/push/done/delete)
# =============================================================================


class Task(Resource):
    """Google Tasks single-task resource."""

    name = "task"

    def clone(
        self,
        task_id: str,
        *,
        tasklist: str | None = None,
        output: Path | None = None,
        **kw,
    ) -> Path:
        """Clone a single task to a .task.gax.yaml file."""
        tl_id, _tl_title = resolve_tasklist_id(tasklist)
        api_task = get_task(tl_id, task_id)
        task = api_to_task(api_task, tl_id)

        if not output:
            output = Path(f"{_safe_filename(task.title)}.task.gax.yaml")

        if output.exists():
            raise ValueError(f"File already exists: {output}")

        content = task_to_yaml(task)
        output.write_text(content)
        return output

    def new(
        self,
        title: str,
        *,
        tasklist: str | None = None,
        output: Path | None = None,
    ) -> Path:
        """Create a new task on Google and write local .task.gax.yaml file."""
        tl_id, _tl_title = resolve_tasklist_id(tasklist)

        body = {"title": title, "status": "needsAction"}
        result = create_task(tl_id, body)

        task = api_to_task(result, tl_id)

        file_path = output or Path(f"{_safe_filename(title)}.task.gax.yaml")
        content = task_to_yaml(task)
        file_path.write_text(content)
        return file_path

    def pull(self, path: Path, **kw) -> None:
        """Pull latest task data from API."""
        content = path.read_text()
        local = yaml_to_task(content)

        if not local.id:
            raise ValueError("Task has no ID (not yet pushed upstream)")

        api_task = get_task(local.tasklist, local.id)
        updated = api_to_task(api_task, local.tasklist)

        new_content = task_to_yaml(updated)
        path.write_text(new_content)

    def diff(self, path: Path, **kw) -> str | None:
        """Preview changes between local task file and remote.

        Returns a human-readable diff string, or None if no changes.
        For new tasks (no id), returns a summary of what will be created.
        """
        content = path.read_text()
        local = yaml_to_task(content)

        if not local.id:
            parts = [f"New task: {local.title}"]
            if local.due:
                parts.append(f"due: {local.due}")
            return "\n".join(parts)

        api_task = get_task(local.tasklist, local.id)
        remote = api_to_task(api_task, local.tasklist)

        fields = [
            ("title", local.title, remote.title),
            ("status", local.status, remote.status),
            ("due", local.due, remote.due),
            ("notes", local.notes, remote.notes),
        ]

        lines = []
        for name, local_val, remote_val in fields:
            if local_val != remote_val:
                lines.append(f"{name}: {remote_val} -> {local_val}")

        return "\n".join(lines) if lines else None

    def push(self, path: Path, **kw) -> str:
        """Push local task changes to API. Returns task title."""
        content = path.read_text()
        local = yaml_to_task(content)

        body = task_to_api_body(local)

        if local.id:
            update_task(local.tasklist, local.id, body)
            return local.title
        else:
            tl_id = local.tasklist
            if not tl_id:
                tl_id, _ = resolve_tasklist_id(None)

            result = create_task(tl_id, body, parent=local.parent)

            # Update local file with new ID
            local.id = result["id"]
            local.tasklist = tl_id
            local.source = result.get("selfLink", "")
            local.synced = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            new_content = task_to_yaml(local)
            path.write_text(new_content)

            return local.title

    def done(self, path: Path) -> str:
        """Mark task as completed and push. Returns task title."""
        content = path.read_text()
        local = yaml_to_task(content)

        local.status = "completed"
        local.completed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Write updated status locally
        path.write_text(task_to_yaml(local))

        # Push to API
        if local.id:
            body = task_to_api_body(local)
            update_task(local.tasklist, local.id, body)

        return local.title

    def delete(self, path: Path) -> str:
        """Delete task from Google and local file. Returns task title."""
        content = path.read_text()
        local = yaml_to_task(content)

        if not local.id:
            raise ValueError("Task has no ID (not on Google)")

        delete_task(local.tasklist, local.id)
        path.unlink()
        return local.title
