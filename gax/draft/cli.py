"""CLI commands for Gmail draft operations."""

import sys
import click
from pathlib import Path

from ..cli_lib import handle_errors, _confirm_and_push, success
from .. import docs
from . import Draft


@docs.section("resource")
@click.group()
def draft():
    """Draft operations"""
    pass


@draft.command("new")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <subject>.draft.gax.md)",
)
@click.option("--to", "to_addr", default="", help="Recipient email address")
@click.option("--subject", default="", help="Email subject")
@handle_errors
def draft_new(output, to_addr, subject):
    """Create a new local draft file.

    Creates a .draft.gax.md file that can be edited and pushed to Gmail.

    Examples:

        gax draft new
        gax draft new --to alice@example.com --subject "Hello"
        gax draft new -o my_draft.draft.gax.md
    """
    if not to_addr:
        to_addr = click.prompt("To")
    if not subject:
        subject = click.prompt("Subject")

    file_path = Draft().new(to=to_addr, subject=subject, output=output)
    success(f"Created: {file_path}")
    click.echo(f"Edit the file, then run: gax draft push {file_path}")


@draft.command("clone")
@click.argument("draft_id_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <subject>.draft.gax.md)",
)
@handle_errors
def draft_clone(draft_id_or_url, output):
    """Clone an existing draft from Gmail.

    Examples:

        gax draft clone r-1234567890123456789
        gax draft clone "https://mail.google.com/mail/u/0/#drafts/..."
        gax draft clone r-1234567890 -o my_draft.draft.gax.md
    """
    path = Draft.from_url_or_id(draft_id_or_url).clone(output=output)
    success(f"Created: {path}")


@draft.command("list")
@click.option("--limit", default=100, help="Maximum results (default: 100)")
@handle_errors
def draft_list(limit):
    """List Gmail drafts (TSV output).

    Output columns: draft_id, thread_id, date, to, subject
    """
    Draft().list(sys.stdout, limit=limit)


@draft.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def draft_push(file, yes):
    """Push local draft to Gmail.

    If the draft doesn't exist in Gmail yet, creates it.
    If it exists, shows diff and updates it (with confirmation).

    Examples:

        gax draft push my_draft.draft.gax.md
        gax draft push my_draft.draft.gax.md -y
    """
    _confirm_and_push(Draft.from_file(file), yes=yes)


@draft.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@handle_errors
def draft_pull(file):
    """Pull latest content from Gmail draft.

    Updates the local .draft.gax.md file with the remote draft content.

    Example:

        gax draft pull my_draft.draft.gax.md
    """
    Draft.from_file(file).pull()
    success(f"Updated: {file}")
