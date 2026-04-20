"""CLI commands for Google Drive file operations."""

import click
from pathlib import Path

from ..ui import handle_errors, success
from .. import docs
from . import File


@docs.section("resource")
@docs.maturity("unstable")
@click.group("file")
def file_group():
    """Google Drive file operations."""
    pass


@file_group.command("clone")
@click.argument("url_or_id")
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), help="Output file path"
)
@handle_errors
def file_clone(url_or_id, output):
    """Clone a file from Google Drive.

    Downloads the file and creates a tracking .gax.md file.

    Examples:

        gax file clone https://drive.google.com/file/d/abc123/view
        gax file clone abc123 -o report.pdf
    """
    path = File.from_url_or_id(url_or_id).clone(output=output)
    success(f"Created: {path}")


@file_group.command("checkout")
@click.argument("url_or_id")
@click.option(
    "-o", "--output", type=click.Path(path_type=Path), help="Output folder path"
)
@click.option("-R", "--recursive", is_flag=True, help="Recurse into subfolders")
@handle_errors
def file_checkout(url_or_id, output, recursive):
    """Checkout a Google Drive folder to a local directory.

    Downloads all files. Google Workspace files (Docs, Sheets, Forms)
    are cloned via their native gax resource.

    \b
    Examples:
        gax file checkout https://drive.google.com/drive/folders/abc123
        gax file checkout abc123 -o my_folder
        gax file checkout abc123 -R
    """
    from .gdrive import Folder

    path = Folder.from_url_or_id(url_or_id).checkout(output=output, recursive=recursive)
    success(f"Checked out: {path}")


@file_group.command("pull")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@handle_errors
def file_pull(file_path):
    """Pull latest version of a file from Google Drive.

    Requires a .gax.md tracking file (created by 'gax file clone').

    Example:

        gax file pull report.pdf
    """
    File(path=file_path).pull()
    success(f"Updated: {file_path}")


@file_group.command("push")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("--public", is_flag=True, help="Make file publicly accessible")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def file_push(file_path, public, yes):
    """Push local file to Google Drive.

    If file has a .gax.md tracking file, updates existing file.
    Otherwise, uploads as a new file.

    Examples:

        gax file push report.pdf
        gax file push report.pdf --public
        gax file push report.pdf -y
    """
    tracking_path = file_path.with_suffix(file_path.suffix + ".gax.md")

    if tracking_path.exists():
        from .gdrive import read_tracking_file

        tracking_data = read_tracking_file(tracking_path)
        if not yes:
            click.echo(f"Will update Drive file: {tracking_data.get('name')}")
            click.echo(f"Local file: {file_path}")
            if public:
                click.echo("Will make publicly accessible")
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                return
    else:
        if not yes:
            click.echo(f"Will upload new file: {file_path.name}")
            if public:
                click.echo("Will make publicly accessible")
            if not click.confirm("Upload this file?"):
                click.echo("Aborted.")
                return

    File(path=file_path).push(public=public)
    success("Pushed successfully.")
