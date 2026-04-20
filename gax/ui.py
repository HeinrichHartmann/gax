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

    When inside an operation() context, log messages update the spinner.
    When outside, logs are silently ignored (not printed).
    """

    def emit(self, record):
        global _active_task

        if _active_task is None:
            # No active progress - swallow the log (silent by default)
            return

        msg = self.format(record)
        progress, task_id = _active_task

        # Color based on level
        level_colors = {
            logging.DEBUG: "dim",
            logging.INFO: "cyan",
            logging.WARNING: "yellow",
            logging.ERROR: "red",
        }
        color = level_colors.get(record.levelno, "white")
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


def handle_errors(fn):
    """Decorator: catch exceptions, print error, exit 1."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            error(str(e))
            sys.exit(1)

    return wrapper


def _confirm_and_push(resource, *, yes=False, **kw):
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
