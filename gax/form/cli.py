"""CLI commands for Google Forms operations."""

import click
from pathlib import Path

from ..ui import gax_command, confirm_and_push, success
from .. import docs
from . import Form


@docs.section("resource")
@docs.maturity("unstable")
@click.group()
def form():
    """Google Forms operations"""
    pass


@form.command("clone")
@click.argument("url")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: <title>.form.gax.md)",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "yaml"]),
    default="md",
    help="Content format: md (readable, default) or yaml (round-trip safe)",
)
@gax_command
def form_clone(url, output, fmt):
    """Clone a Google Form to a local .form.gax.md file.

    By default, creates a human-readable markdown representation.
    Use --format yaml for faithful round-trip representation (required for push).
    """
    file_path = Form.from_url(url).clone(output=output, format=fmt)
    success(f"Created: {file_path}")
    if fmt == "md":
        click.echo("Note: Use --format yaml for round-trip safe format")


@form.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@gax_command
def form_pull(file):
    """Pull latest form definition from Google Forms."""
    Form.from_file(file).pull()
    success(f"Updated: {file}")


@form.command("plan")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default="form.plan.yaml",
    help="Output plan file",
)
@gax_command
def form_plan(file, output):
    """Preview form changes (diff)."""
    diff_text = Form.from_file(file).diff()
    if diff_text is None:
        click.echo("No changes to apply.")
        return
    click.echo(diff_text)


@form.command("apply")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@gax_command
def form_apply(file, yes):
    """Apply form changes to Google Forms."""
    confirm_and_push(Form.from_file(file), yes=yes)
