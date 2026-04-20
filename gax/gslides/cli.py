"""CLI commands for Google Slides operations."""

import click
from pathlib import Path

from ..ui import handle_errors, success
from .. import docs
from . import Slide, Presentation


@docs.section("resource")
@click.group()
def slides():
    """Google Slides operations"""
    pass


@slides.command("checkout")
@click.argument("url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output directory path",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    default="md",
    type=click.Choice(["md", "json"]),
    show_default=True,
    help="Output format: md (read-only) or json (read-write)",
)
@handle_errors
def slides_checkout(url: str, output: Path | None, fmt: str):
    """Checkout a Google Slides presentation to a local directory.

    Creates a .slides.gax.md.d/ folder with one file per slide.

    \b
    Formats:
        md   — human-readable markdown (pull only, no push)
        json — full-fidelity JSON (supports push)

    \b
    Examples:
        gax slides checkout https://docs.google.com/presentation/d/abc123/edit
        gax slides checkout abc123 -o my_slides
        gax slides checkout abc123 --format json
    """
    folder_path = Presentation.from_url(url).checkout(output=output, fmt=fmt)
    success(f"Checked out: {folder_path}")


@slides.command("pull")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@handle_errors
def slides_pull(path: Path):
    """Pull latest slides from Google.

    Works with both single .slides.gax.md files and .slides.gax.md.d/ folders.

    \b
    Examples:
        gax slides pull my_deck.slides.gax.md.d/
        gax slides pull 00_Welcome.slides.gax.md
    """
    if path.is_dir():
        Presentation.from_file(path).pull()
    else:
        Slide.from_file(path).pull()
    success(f"Updated: {path}")


@slides.command("push")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def slides_push(path: Path, yes: bool):
    """Push local slides to Google. JSON format only.

    Markdown checkouts cannot be pushed — re-checkout with --format json.
    Works with both single .slides.gax.md files and .slides.gax.md.d/ folders.

    \b
    Examples:
        gax slides push my_deck.slides.gax.md.d/
        gax slides push my_deck.slides.gax.md.d/ -y
        gax slides push 00_Welcome.slides.gax.md
    """
    if path.is_dir():
        p = Presentation.from_file(path)
        diff_text = p.diff()
        if diff_text is None:
            click.echo("No changes to push.")
            return
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Push these changes?"):
                click.echo("Cancelled.")
                return
        p.push()
    else:
        s = Slide.from_file(path)
        diff_text = s.diff()
        if diff_text is None:
            click.echo("No changes to push.")
            return
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Push these changes?"):
                click.echo("Cancelled.")
                return
        s.push()

    success(f"Pushed: {path}")
