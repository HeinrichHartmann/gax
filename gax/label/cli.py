"""CLI commands for Gmail label management."""

import sys
import click
from pathlib import Path

from ..ui import gax_command, confirm_and_push, success
from .. import docs
from . import Label


@docs.section("resource")
@click.group("mail-label")
def mail_label():
    """Gmail label management (declarative)."""
    pass


@mail_label.command("list")
@gax_command
def label_list():
    """List Gmail labels (TSV output)."""
    Label().list(sys.stdout)


@mail_label.command("clone")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: labels.mail.gax.md)",
)
@click.option("--all", "include_all", is_flag=True, help="Include system labels")
@gax_command
def label_clone(output, include_all):
    """Clone Gmail labels to a .gax.md file."""
    file_path = Label().clone(output=output, include_all=include_all)
    success(f"Created: {file_path}")


@mail_label.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--all", "include_all", is_flag=True, help="Include system labels")
@gax_command
def label_pull(file, include_all):
    """Pull latest labels to existing file."""
    Label.from_file(file).pull(include_all=include_all)
    success(f"Updated: {file}")


@mail_label.command("plan")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default="labels.plan.yaml",
    help="Output plan file",
)
@click.option("--delete", "allow_delete", is_flag=True, help="Include deletions")
@gax_command
def label_plan(file, output, allow_delete):
    """Preview label changes (diff)."""
    diff_text = Label.from_file(file).diff(allow_delete=allow_delete)
    if diff_text is None:
        click.echo("No changes to apply.")
        return
    click.echo(diff_text)


@mail_label.command("apply")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.option("--delete", "allow_delete", is_flag=True, help="Include deletions")
@gax_command
def label_apply(file, yes, allow_delete):
    """Apply label changes to Gmail."""
    confirm_and_push(Label.from_file(file), yes=yes, allow_delete=allow_delete)
