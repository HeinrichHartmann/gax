"""Tests for gax.gtask -- Google Tasks sync."""

from unittest.mock import patch

import pytest

from gax.gtask import (
    TaskItem,
    api_to_task,
    task_to_api_body,
    task_to_yaml,
    yaml_to_task,
    format_tasks_md,
    parse_tasks_md,
    format_tasks_yaml,
    Task,
)


# =============================================================================
# Sample data
# =============================================================================

SAMPLE_API_TASK = {
    "id": "MTIzNDU2Nzg5",
    "title": "Buy groceries",
    "status": "needsAction",
    "due": "2026-04-21T00:00:00.000Z",
    "notes": "Milk, eggs, bread.",
    "updated": "2026-04-18T14:30:00.000Z",
    "selfLink": "https://www.googleapis.com/tasks/v1/lists/TL1/tasks/MTIzNDU2Nzg5",
    "position": "00000000000000000001",
}

SAMPLE_API_COMPLETED = {
    "id": "MTIzNDU2Nzk5",
    "title": "Write ADR",
    "status": "completed",
    "due": "2026-04-20T00:00:00.000Z",
    "completed": "2026-04-19T15:30:00.000Z",
    "updated": "2026-04-19T15:30:00.000Z",
    "selfLink": "https://www.googleapis.com/tasks/v1/lists/TL1/tasks/MTIzNDU2Nzk5",
    "position": "00000000000000000002",
}

SAMPLE_API_SUBTASK = {
    "id": "SUB1",
    "title": "Milk",
    "status": "needsAction",
    "parent": "MTIzNDU2Nzg5",
    "selfLink": "https://www.googleapis.com/tasks/v1/lists/TL1/tasks/SUB1",
    "position": "00000000000000000001",
}


# =============================================================================
# Inverse pair: API <-> TaskItem
# =============================================================================


class TestTaskApiRoundTrip:
    """Tests for api_to_task / task_to_api_body inverse pair."""

    def test_basic_fields(self):
        """API task dict converts to TaskItem with correct fields."""
        task = api_to_task(SAMPLE_API_TASK, "TL1")

        assert task.id == "MTIzNDU2Nzg5"
        assert task.tasklist == "TL1"
        assert task.title == "Buy groceries"
        assert task.status == "needsAction"
        assert task.due == "2026-04-21"  # date-only, no time
        assert task.notes == "Milk, eggs, bread."
        assert task.position == "00000000000000000001"
        assert task.synced  # should be set

    def test_due_date_normalization(self):
        """API's RFC 3339 due date is stored as date-only."""
        task = api_to_task(SAMPLE_API_TASK, "TL1")
        assert task.due == "2026-04-21"
        assert "T" not in task.due

    def test_completed_task(self):
        """Completed task has status and timestamp."""
        task = api_to_task(SAMPLE_API_COMPLETED, "TL1")
        assert task.status == "completed"
        assert task.completed == "2026-04-19T15:30:00.000Z"

    def test_subtask(self):
        """Subtask has parent field set."""
        task = api_to_task(SAMPLE_API_SUBTASK, "TL1")
        assert task.parent == "MTIzNDU2Nzg5"

    def test_empty_task(self):
        """Minimal API response converts without error."""
        task = api_to_task({"id": "X", "title": "Empty"}, "TL1")
        assert task.id == "X"
        assert task.title == "Empty"
        assert task.due == ""
        assert task.notes == ""

    def test_api_body_includes_pushable_fields(self):
        """task_to_api_body includes title, status, due, notes."""
        task = api_to_task(SAMPLE_API_TASK, "TL1")
        body = task_to_api_body(task)

        assert body["title"] == "Buy groceries"
        assert body["status"] == "needsAction"
        assert body["due"] == "2026-04-21T00:00:00.000Z"
        assert body["notes"] == "Milk, eggs, bread."

    def test_api_body_excludes_readonly(self):
        """task_to_api_body excludes id, completed, updated, position."""
        task = api_to_task(SAMPLE_API_COMPLETED, "TL1")
        body = task_to_api_body(task)

        assert "id" not in body
        assert "completed" not in body
        assert "updated" not in body
        assert "position" not in body

    def test_api_body_omits_empty_optional(self):
        """Empty due and notes are omitted from API body."""
        task = api_to_task({"id": "X", "title": "No extras"}, "TL1")
        body = task_to_api_body(task)

        assert "due" not in body
        assert "notes" not in body
        assert body["title"] == "No extras"


# =============================================================================
# Split YAML format
# =============================================================================


class TestTaskYamlRoundTrip:
    """Tests for task_to_yaml / yaml_to_task."""

    def test_round_trip(self):
        """Full task round-trips through YAML."""
        original = api_to_task(SAMPLE_API_TASK, "TL1")
        yaml_str = task_to_yaml(original)
        parsed = yaml_to_task(yaml_str)

        assert parsed.id == original.id
        assert parsed.tasklist == original.tasklist
        assert parsed.title == original.title
        assert parsed.status == original.status
        assert parsed.due == original.due
        assert parsed.notes == original.notes

    def test_split_structure(self):
        """YAML output has header (gax metadata) and body (task data)."""
        task = api_to_task(SAMPLE_API_TASK, "TL1")
        yaml_str = task_to_yaml(task)

        parts = yaml_str.split("---", 2)
        assert len(parts) == 3

        # Header should have gax fields
        assert "type:" in parts[1]
        assert "gax/task" in parts[1]
        assert "id:" in parts[1]
        assert "tasklist:" in parts[1]

        # Body should have task data
        assert "title:" in parts[2]
        assert "status:" in parts[2]

        # Title should NOT be in header
        assert "Buy groceries" not in parts[1]
        assert "Buy groceries" in parts[2]

    def test_minimal_task(self):
        """Task with only required fields round-trips."""
        task = TaskItem(
            id="X", tasklist="TL1", source="", synced="", title="Simple"
        )
        yaml_str = task_to_yaml(task)
        parsed = yaml_to_task(yaml_str)

        assert parsed.title == "Simple"
        assert parsed.due == ""
        assert parsed.notes == ""

    def test_notes_with_newlines(self):
        """Multi-line notes round-trip correctly."""
        task = TaskItem(
            id="X",
            tasklist="TL1",
            source="",
            synced="",
            title="Multi",
            notes="Line 1\nLine 2\nLine 3\n",
        )
        yaml_str = task_to_yaml(task)
        parsed = yaml_to_task(yaml_str)

        assert parsed.notes == "Line 1\nLine 2\nLine 3\n"

    def test_invalid_no_frontmatter(self):
        """Missing --- raises ValueError."""
        with pytest.raises(ValueError, match="frontmatter"):
            yaml_to_task("title: foo\nstatus: needsAction\n")

    def test_invalid_wrong_type(self):
        """Wrong type field raises ValueError."""
        content = "---\ntype: gax/cal\nid: X\n---\ntitle: foo\n"
        with pytest.raises(ValueError, match="gax/task"):
            yaml_to_task(content)


# =============================================================================
# Markdown checkbox format
# =============================================================================


class TestTaskListFormatMd:
    """Tests for format_tasks_md / parse_tasks_md."""

    def test_format_basic(self):
        """Root tasks formatted as checkboxes."""
        tasks = [
            TaskItem("A", "TL1", "", "", "Task A", "needsAction"),
            TaskItem("B", "TL1", "", "", "Task B", "completed"),
        ]
        result = format_tasks_md(tasks)

        assert "- [ ] Task A `A`" in result
        assert "- [x] Task B `B`" in result

    def test_format_with_due(self):
        """Due date appended to checkbox line."""
        tasks = [
            TaskItem(
                "A", "TL1", "", "", "Task A", "needsAction", due="2026-04-21"
            ),
        ]
        result = format_tasks_md(tasks)
        assert "due:2026-04-21" in result

    def test_format_subtasks(self):
        """Subtasks indented under parent."""
        tasks = [
            TaskItem("P", "TL1", "", "", "Parent", "needsAction"),
            TaskItem("C", "TL1", "", "", "Child", "needsAction", parent="P"),
        ]
        result = format_tasks_md(tasks)

        lines = result.strip().split("\n")
        assert lines[0].startswith("- [ ] Parent")
        assert lines[1].startswith("  - [ ] Child")

    def test_parse_basic(self):
        """Parse checkboxes back to task dicts."""
        content = "- [ ] Task A `A1` due:2026-04-21\n- [x] Task B `B1`\n"
        parsed = parse_tasks_md(content)

        assert len(parsed) == 2
        assert parsed[0]["title"] == "Task A"
        assert parsed[0]["id"] == "A1"
        assert parsed[0]["status"] == "needsAction"
        assert parsed[0]["due"] == "2026-04-21"
        assert parsed[1]["title"] == "Task B"
        assert parsed[1]["status"] == "completed"

    def test_parse_subtask(self):
        """Indented items parsed as subtasks."""
        content = "- [ ] Parent `P`\n  - [ ] Child `C`\n"
        parsed = parse_tasks_md(content)

        assert len(parsed) == 2
        assert parsed[0]["is_subtask"] is False
        assert parsed[1]["is_subtask"] is True

    def test_parse_new_task_no_id(self):
        """Lines without backtick ID are new tasks."""
        content = "- [ ] New task\n"
        parsed = parse_tasks_md(content)

        assert len(parsed) == 1
        assert parsed[0]["title"] == "New task"
        assert parsed[0]["id"] == ""

    def test_round_trip(self):
        """Format -> parse preserves essential data."""
        tasks = [
            TaskItem("A", "TL1", "", "", "Task A", "needsAction", due="2026-04-21"),
            TaskItem("B", "TL1", "", "", "Task B", "completed"),
        ]
        md = format_tasks_md(tasks)
        parsed = parse_tasks_md(md)

        assert len(parsed) == 2
        assert parsed[0]["title"] == "Task A"
        assert parsed[0]["id"] == "A"
        assert parsed[0]["due"] == "2026-04-21"
        assert parsed[1]["title"] == "Task B"
        assert parsed[1]["status"] == "completed"


# =============================================================================
# YAML list format
# =============================================================================


class TestTaskListFormatYaml:
    """Tests for format_tasks_yaml."""

    def test_format_basic(self):
        """Root tasks formatted as YAML list."""
        tasks = [
            TaskItem(
                "A", "TL1", "", "", "Task A", "needsAction", due="2026-04-21"
            ),
        ]
        result = format_tasks_yaml(tasks)

        assert "title: Task A" in result
        assert "id: A" in result
        assert "due: '2026-04-21'" in result or "due: 2026-04-21" in result

    def test_format_subtasks(self):
        """Subtasks nested under parent."""
        tasks = [
            TaskItem("P", "TL1", "", "", "Parent", "needsAction"),
            TaskItem("C", "TL1", "", "", "Child", "needsAction", parent="P"),
        ]
        result = format_tasks_yaml(tasks)

        assert "subtasks:" in result
        assert "Child" in result

    def test_format_omits_empty(self):
        """Empty optional fields omitted."""
        tasks = [TaskItem("A", "TL1", "", "", "Task A", "needsAction")]
        result = format_tasks_yaml(tasks)

        assert "notes:" not in result
        assert "completed:" not in result


# =============================================================================
# Task.diff()
# =============================================================================


class TestTaskDiff:
    """Tests for Task.diff() with mocked API."""

    def test_new_task_returns_summary(self, tmp_path):
        """No ID returns creation summary."""
        task = TaskItem("", "TL1", "", "", "New task", due="2026-04-21")
        path = tmp_path / "new.task.gax.yaml"
        path.write_text(task_to_yaml(task))

        result = Task().diff(path)
        assert "New task" in result
        assert "2026-04-21" in result

    @patch("gax.gtask.get_task")
    @patch("gax.gtask.api_to_task")
    def test_no_changes_returns_none(self, mock_api_to, mock_get, tmp_path):
        """Identical local/remote returns None."""
        task = TaskItem(
            "A", "TL1", "", "", "Same", "needsAction", due="2026-04-21"
        )
        path = tmp_path / "same.task.gax.yaml"
        path.write_text(task_to_yaml(task))

        mock_get.return_value = {}
        mock_api_to.return_value = TaskItem(
            "A", "TL1", "", "", "Same", "needsAction", due="2026-04-21"
        )

        result = Task().diff(path)
        assert result is None

    @patch("gax.gtask.get_task")
    @patch("gax.gtask.api_to_task")
    def test_field_changes(self, mock_api_to, mock_get, tmp_path):
        """Changed fields shown in diff output."""
        local = TaskItem(
            "A", "TL1", "", "", "Updated title", "completed", due="2026-04-22"
        )
        path = tmp_path / "changed.task.gax.yaml"
        path.write_text(task_to_yaml(local))

        mock_get.return_value = {}
        mock_api_to.return_value = TaskItem(
            "A", "TL1", "", "", "Original title", "needsAction", due="2026-04-21"
        )

        result = Task().diff(path)
        assert "title:" in result
        assert "status:" in result
        assert "due:" in result


# =============================================================================
# Task.done()
# =============================================================================


class TestTaskDone:
    """Tests for Task.done() shortcut."""

    @patch("gax.gtask.update_task")
    def test_done_sets_completed_and_pushes(self, mock_update, tmp_path):
        """done() sets status=completed, writes file, calls API."""
        task = TaskItem("A", "TL1", "", "", "My task", "needsAction")
        path = tmp_path / "task.task.gax.yaml"
        path.write_text(task_to_yaml(task))

        title = Task().done(path)

        assert title == "My task"

        # File should be updated
        updated = yaml_to_task(path.read_text())
        assert updated.status == "completed"
        assert updated.completed  # timestamp set

        # API should be called
        mock_update.assert_called_once()
        call_args = mock_update.call_args
        assert call_args[0][0] == "TL1"  # tasklist
        assert call_args[0][1] == "A"  # task_id
        assert call_args[0][2]["status"] == "completed"
