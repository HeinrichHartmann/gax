"""CLI commands for Google Tasks operations."""

import sys
import click
from pathlib import Path

from ..cli_lib import handle_errors, success
from .. import docs
from . import TaskList, Task as TaskResource


@docs.section("resource")
@click.group(name="task")
def task_group():
    """Google Tasks sync commands."""
    pass


@task_group.command(name="lists")
@handle_errors
def task_lists_cmd():
    """List available task lists."""
    TaskList().lists(sys.stdout)


@task_group.command(name="list")
@click.argument("tasklist", required=False)
@click.option("--all", "show_all", is_flag=True, help="Include completed tasks")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "yaml"]),
    default="md",
    help="Output format (default: md)",
)
@handle_errors
def task_list_cmd(tasklist: str | None, show_all: bool, fmt: str):
    """View tasks from a task list."""
    TaskList().list(sys.stdout, tasklist=tasklist, show_all=show_all, fmt=fmt)


@task_group.command(name="clone")
@click.argument("tasklist", required=False)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file path",
)
@click.option("--all", "show_all", is_flag=True, help="Include completed tasks")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "yaml"]),
    default="md",
    help="Output format (default: md)",
)
@handle_errors
def task_clone_cmd(tasklist: str | None, output: Path | None, show_all: bool, fmt: str):
    """Clone a task list to a single file."""
    path = TaskList().clone(
        tasklist=tasklist, output=output, fmt=fmt, show_all=show_all
    )
    success(f"Created: {path}")


@task_group.command(name="checkout")
@click.argument("tasklist", required=False)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output folder path",
)
@click.option("--all", "show_all", is_flag=True, help="Include completed tasks")
@handle_errors
def task_checkout_cmd(tasklist: str | None, output: Path | None, show_all: bool):
    """Checkout a task list as a folder of individual task files."""
    cloned, skipped = TaskList().checkout(
        tasklist=tasklist, output=output, show_all=show_all
    )
    success(f"Checked out: {cloned}, Skipped: {skipped}")


@task_group.command(name="pull")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@handle_errors
def task_pull_cmd(file_path: Path):
    """Pull latest task data from API."""
    name = file_path.name
    if name.endswith(".tasks.gax.md") or name.endswith(".tasks.gax.yaml"):
        TaskList.from_file(file_path).pull()
    else:
        TaskResource.from_file(file_path).pull()
    success(f"Updated: {file_path}")


@task_group.command(name="new")
@click.argument("title")
@click.option(
    "--tasklist",
    help="Task list name, ID, or index (default: first list)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file path",
)
@handle_errors
def task_new_cmd(title: str, tasklist: str | None, output: Path | None):
    """Create a new task on Google."""
    path = TaskResource().new(title, tasklist=tasklist, output=output)
    success(f"Created: {path}")


@task_group.command(name="diff")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@handle_errors
def task_diff_cmd(file_path: Path):
    """Show differences between local task and remote."""
    diff_text = TaskResource.from_file(file_path).diff()
    if diff_text is None:
        click.echo("No changes.")
    else:
        click.echo(diff_text)


@task_group.command(name="push")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def task_push_cmd(file_path: Path, yes: bool):
    """Push local task changes to API."""
    t = TaskResource.from_file(file_path)
    diff_text = t.diff()
    if diff_text is None:
        click.echo("No changes to push.")
        return
    if not yes:
        click.echo(diff_text)
        if not click.confirm("Push these changes?"):
            click.echo("Cancelled.")
            return
    title = t.push()
    success(f"Pushed: {title}")


@task_group.command(name="done")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def task_done_cmd(file_path: Path, yes: bool):
    """Mark a task as completed and push."""
    if not yes:
        if not click.confirm(f"Mark {file_path.name} as done?"):
            click.echo("Cancelled.")
            return
    title = TaskResource.from_file(file_path).done()
    success(f"Done: {title}")


@task_group.command(name="delete")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def task_delete_cmd(file_path: Path, yes: bool):
    """Delete a task from Google and local file."""
    if not yes:
        if not click.confirm(f"Delete {file_path.name}?"):
            click.echo("Cancelled.")
            return
    title = TaskResource.from_file(file_path).delete()
    success(f"Deleted: {title}")
