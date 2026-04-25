"""Unified UI layer with progress/logging integration.

Logs are silent by default. During `operation()` contexts, log messages
appear as ephemeral status in the progress spinner.

Usage:
    import logging
    from .ui import operation, echo, error, success, confirm

    logger = logging.getLogger(__name__)

    with operation("Pushing changes", total=5) as op:
        for item in items:
            logger.info(f"Processing {item}")  # Shows in spinner
            do_work()
            op.advance()

    success("Done!")  # Explicit output still works
"""

import functools
import logging
import sys
from contextlib import contextmanager
from typing import Optional

import click
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
)
from rich.prompt import Confirm

console = Console()
err_console = Console(stderr=True)

# Track active progress context
_active_task: Optional[tuple] = None


class ProgressHandler(logging.Handler):
    """Logging handler that feeds into active progress display.

    When inside an operation() context:
      - INFO/DEBUG: update the spinner status line (ephemeral)
      - WARNING/ERROR: print permanently above the spinner

    When outside, logs are silently ignored (not printed).
    """

    def emit(self, record):
        global _active_task

        if _active_task is None:
            return

        msg = self.format(record)
        progress, task_id = _active_task

        if record.levelno >= logging.WARNING:
            # Warnings and errors print permanently above the spinner
            color = "red" if record.levelno >= logging.ERROR else "yellow"
            progress.console.print(f"[{color}]{msg}[/{color}]")
        else:
            # Info and debug update the ephemeral spinner line
            color = "dim" if record.levelno <= logging.DEBUG else "cyan"
            progress.update(task_id, description=f"[{color}]{msg}[/{color}]")


class Operation:
    """Handle returned by operation() context manager."""

    def __init__(self, progress: Progress, task_id):
        self._progress = progress
        self._task_id = task_id

    def advance(self, n: int = 1):
        """Advance progress by n steps."""
        self._progress.advance(self._task_id, n)

    def update(self, description: str):
        """Update the status description."""
        self._progress.update(self._task_id, description=description)


@contextmanager
def operation(description: str, total: Optional[int] = None):
    """Context manager for operations with integrated progress/logging.

    Supports nesting: inner operations reuse the existing Progress display
    by adding a new task to it, rather than creating a new spinner.

    Args:
        description: Initial status message
        total: Total number of steps (for progress bar)

    Yields:
        Operation object with advance() and update() methods

    Example:
        with operation("Processing files", total=10) as op:
            for f in files:
                logger.info(f"Working on {f}")
                process(f)
                op.advance()
    """
    global _active_task

    if _active_task is not None:
        # Nested: reuse existing Progress, add a new task
        progress, outer_task_id = _active_task
        task_id = progress.add_task(description, total=total)
        _active_task = (progress, task_id)
        try:
            yield Operation(progress, task_id)
        finally:
            progress.remove_task(task_id)
            _active_task = (progress, outer_task_id)
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,  # Disappears when done
    ) as progress:
        task_id = progress.add_task(description, total=total)
        _active_task = (progress, task_id)

        try:
            yield Operation(progress, task_id)
        finally:
            _active_task = None


def setup_logging():
    """Wire up the progress-aware logging handler.

    Call once at CLI startup.
    """
    handler = ProgressHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))

    # Add to root logger
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.DEBUG)  # Handler decides what to show


# Explicit output functions (always print)


def echo(msg: str) -> None:
    """Standard output."""
    console.print(msg)


def info(msg: str) -> None:
    """Info message (blue)."""
    console.print(f"[blue]{msg}[/blue]")


def success(msg: str) -> None:
    """Success message (green)."""
    console.print(f"[green]{msg}[/green]")


def error(msg: str) -> None:
    """Error message (red) to stderr."""
    err_console.print(f"[red]{msg}[/red]")


def warning(msg: str) -> None:
    """Warning message (yellow)."""
    console.print(f"[yellow]{msg}[/yellow]")


def confirm(question: str, default: bool = False) -> bool:
    """Ask yes/no question."""
    return Confirm.ask(question, default=default)


def gax_command(fn):
    """Standard decorator for all gax CLI commands.

    Wraps the command in an operation() context so that:
      - logger.info() messages show as ephemeral spinner status
      - logger.warning() messages print permanently above the spinner
      - Exceptions are caught, printed as errors, and cause exit(1)

    The spinner description is derived from the function name.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        desc = fn.__name__.replace("_", " ")
        try:
            with operation(desc):
                return fn(*args, **kwargs)
        except Exception as e:
            error(str(e))
            sys.exit(1)

    return wrapper


# Backwards compatibility
handle_errors = gax_command


def confirm_and_push(resource, *, yes=False, **kw):
    """Standard diff -> confirm -> push flow."""
    diff_text = resource.diff(**kw)
    if diff_text is None:
        click.echo("No changes to push.")
        return
    if not yes:
        click.echo(diff_text)
        if not click.confirm("Push these changes?"):
            click.echo("Cancelled.")
            return
    resource.push(**kw)
    success("Pushed successfully.")


def confirm_and_pull(resource, *, yes=False, **kw):
    """Standard diff -> confirm -> pull flow."""
    if not yes:
        diff_text = resource.diff(**kw)
        if diff_text is None:
            click.echo("No changes to pull.")
            return
        click.echo(diff_text)
        if not click.confirm("Pull these changes?"):
            click.echo("Cancelled.")
            return
    resource.pull(**kw)
    success("Pulled successfully.")
