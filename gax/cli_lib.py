"""Shared CLI infrastructure for resource command files.

Each resource's cli.py imports from here:

    from ..cli_lib import handle_errors, _confirm_and_push, success, error
"""

import functools
import sys

import click

from .ui import success, error, echo, info, warning, confirm, operation, setup_logging  # noqa: F401


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
    """Standard diff → confirm → push flow."""
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
