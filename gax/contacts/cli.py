"""CLI commands for Google Contacts operations."""

import click
from pathlib import Path

from ..ui import handle_errors, confirm_and_push, success
from .. import docs
from . import Contact, Contacts  # noqa: F401 — both imported to register with Resource


@docs.section("resource")
@click.group()
def contacts():
    """Google Contacts operations."""
    pass


@contacts.command("clone")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "jsonl"]),
    default="md",
    help="Output format: md (view-only) or jsonl (editable)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: contacts.<format>)",
)
@handle_errors
def contacts_clone(fmt, output):
    """Clone all contacts to a local file.

    \b
    Formats:
      md     Human-readable markdown (default, view-only)
      jsonl  JSON Lines format (editable, scriptable)
    """
    path = Contacts().clone(fmt=fmt, output=output)
    success(f"Created: {path}")


@contacts.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@handle_errors
def contacts_pull(file):
    """Pull latest contacts from Google.

    Updates the file with current contact data, preserving format.
    """
    Contacts(path=file).pull()
    success(f"Updated: {file}")


@contacts.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def contacts_push(file, yes):
    """Push local JSONL contacts to Google.

    Compares local contacts with remote, shows diff, and applies changes.
    Only works with JSONL format files.
    """
    confirm_and_push(Contacts(path=file), yes=yes)


@contacts.command("checkout")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output folder (default: contacts.contacts.gax.md.d)",
)
@handle_errors
def contacts_checkout(output):
    """Checkout contacts as individual files into a folder.

    Creates one .contact.gax.yaml file per contact for easy per-contact
    editing and diffing.
    """
    cloned, skipped = Contacts().checkout(output=output)
    parts = [f"{cloned} contacts"]
    if skipped:
        parts.append(f"({skipped} skipped)")
    success(f"Checked out {' '.join(parts)}")
