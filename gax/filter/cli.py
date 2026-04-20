"""CLI commands for Gmail filter management."""

import sys
import click
from pathlib import Path

from ..ui import handle_errors, _confirm_and_push, success
from .. import docs
from . import Filter


@docs.section("resource")
@click.group("mail-filter")
def mail_filter():
    """Gmail filter management (declarative).

    Note: Gmail applies ALL matching filters simultaneously, not sequentially.
    Filter order has no significance - there is no "stop processing" feature.
    """
    pass


@mail_filter.command("list")
@handle_errors
def filter_list():
    """List Gmail filters (TSV output)."""
    Filter().list(sys.stdout)


@mail_filter.command("clone")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: filters.mail.gax.md)",
)
@handle_errors
def filter_clone(output):
    """Clone Gmail filters to a .gax.md file."""
    file_path = Filter().clone(output=output)
    success(f"Created: {file_path}")


@mail_filter.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@handle_errors
def filter_pull(file):
    """Pull latest filters to existing file."""
    Filter.from_file(file).pull()
    success(f"Updated: {file}")


@mail_filter.command("plan")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@handle_errors
def filter_plan(file):
    """Preview filter changes (diff)."""
    diff_text = Filter.from_file(file).diff()
    if diff_text is None:
        click.echo("No changes to apply.")
        return
    click.echo(diff_text)


@mail_filter.command("apply")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def filter_apply(file, yes):
    """Apply filter changes to Gmail."""
    _confirm_and_push(Filter.from_file(file), yes=yes)
