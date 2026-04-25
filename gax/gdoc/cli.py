"""CLI commands for Google Docs operations."""

import sys
import click
from pathlib import Path

from ..ui import gax_command, confirm_and_pull, success
from .. import docs
from . import Tab, Doc


@docs.section("resource")
@click.group()
def doc():
    """Google Docs operations"""
    pass


@doc.group("tab")
def doc_tab():
    """Single tab operations"""
    pass


@doc_tab.command("list")
@click.argument("url")
@gax_command
def doc_tab_list(url: str):
    """List tabs in a document (TSV output)."""
    Doc.from_url(url).tab_list(sys.stdout)


@doc_tab.command("import")
@click.argument("url")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output tracking file (default: <filename>.tab.gax.md)",
)
@gax_command
def doc_tab_import(url: str, file: Path, output: Path | None):
    """Import a markdown file as a new tab in a document."""
    tracking_path = Doc.from_url(url).tab_import(file, output=output)
    success(f"Created: {tracking_path}")


@doc_tab.command("clone")
@click.argument("url")
@click.argument("tab_name")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <tab>.tab.gax.md)",
)
@gax_command
def doc_tab_clone(url: str, tab_name: str, output: Path | None):
    """Clone a single tab to a .tab.gax.md file."""
    file_path = Tab.from_url(url).clone(output=output, tab_name=tab_name)
    success(f"Created: {file_path}")


@doc_tab.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation, overwrite local state")
@gax_command
def doc_tab_pull(file: Path, yes: bool):
    """Pull latest content for a single tab."""
    confirm_and_pull(Tab.from_file(file), yes=yes)


@doc_tab.command("diff")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@gax_command
def doc_tab_diff(file: Path):
    """Show diff between local file and remote tab."""
    diff_text = Tab.from_file(file).diff()
    if diff_text is None:
        click.echo("No differences.")
    else:
        click.echo(diff_text)


@doc_tab.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--patch",
    "use_patch",
    is_flag=True,
    help="Incremental push: apply only changed elements (experimental)",
)
@gax_command
def doc_tab_push(file: Path, yes: bool, use_patch: bool):
    """Push local changes to a single tab (with confirmation).

    The default push path is full-replace (see ADR 023). The ``--patch`` flag
    selects an **experimental** incremental push path (ADR 027) that diffs the
    local markdown against the live document and applies only the changed
    elements. The ``--patch`` path is under evaluation and may fail on
    structural changes; when in doubt, omit the flag.
    """
    from .doc import parse_multipart, extract_doc_id
    from ..ui import error

    t = Tab.from_file(file)

    if use_patch:
        from .diff_push import preview_diff
        from . import native_md as _native_md

        section = parse_multipart(file.read_text(encoding="utf-8"))[0]
        source_url = section.source
        tab_name = section.section_title
        document_id = extract_doc_id(source_url)

        content_to_push = _native_md.inline_images_from_store(section.content)

        preview = preview_diff(document_id, tab_name, content_to_push)

        if not preview.ops:
            click.echo("No differences to push.")
            return

        click.echo("Patch operations:")
        click.echo("-" * 40)
        for line in preview.summary_lines:
            click.echo(line)
        click.echo("-" * 40)

        if preview.warnings:
            for w in preview.warnings:
                error(w)
            click.echo("Use regular push (without --patch) for structural changes.")
            sys.exit(1)

        if not yes:
            if not click.confirm("Apply patch?"):
                click.echo("Aborted.")
                return

        t.push(use_patch=True)
        success("Patched successfully.")
    else:
        diff_text = t.diff()
        if diff_text is None:
            click.echo("No differences to push.")
            return

        click.echo("Changes to push:")
        click.echo("-" * 40)
        click.echo(diff_text)
        click.echo("-" * 40)

        from .ir import from_markdown, check_unsupported

        section = parse_multipart(file.read_text(encoding="utf-8"))[0]
        push_warnings = check_unsupported(from_markdown(section.content))
        for w in push_warnings:
            click.echo(f"  Warning: {w.feature}: {w.detail}")

        click.echo(
            "Warning: markdown cannot faithfully represent a Google Doc. "
            "Non-markdown formatting (colors, fonts, alignment, comments, "
            "suggestions, images) may be lost. Use --patch for incremental "
            "updates that preserve formatting (experimental)."
        )

        if not yes:
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                return

        t.push()
        success("Pushed successfully.")


@doc.command("clone")
@click.argument("url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <title>.doc.gax.md)",
)
@click.option(
    "--with-comments",
    is_flag=True,
    help="Include document comments as separate sections",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Suppress multi-tab status message",
)
@gax_command
def doc_clone(url: str, output: Path | None, with_comments: bool, quiet: bool):
    """Clone a Google Doc to a local .doc.gax.md file.

    Clones a single tab. For multi-tab documents, use 'gax doc checkout'.
    """
    from .doc import extract_doc_id, get_tabs_list

    file_path = Tab.from_url(url).clone(output=output, with_comments=with_comments)
    success(f"Created: {file_path}")

    if not quiet:
        document_id = extract_doc_id(url)
        tabs = get_tabs_list(document_id)
        if len(tabs["tabs"]) > 1:
            first_tab = tabs["tabs"][0].title
            click.echo(
                f'  Tab "{first_tab}" cloned (1 of {len(tabs["tabs"])} tabs).\n'
                f"  For all tabs: gax doc checkout {url}"
            )


@doc.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-comments",
    is_flag=True,
    help="Include document comments as separate sections",
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation, overwrite local state")
@gax_command
def doc_pull(file: Path, with_comments: bool, yes: bool):
    """Pull latest content from Google Docs to local file."""
    confirm_and_pull(Tab.from_file(file), yes=yes, with_comments=with_comments)


@doc.command("checkout")
@click.argument("url")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output folder (default: <title>.doc.gax.md.d)",
)
@click.option(
    "--with-comments",
    is_flag=True,
    help="Include document comments as a separate file",
)
@gax_command
def doc_checkout(url: str, output: Path | None, with_comments: bool):
    """Checkout all tabs to individual files in a folder.

    Creates a folder with individual .doc.gax.md files for each tab.
    """
    folder = Doc.from_url(url).checkout(output=output, with_comments=with_comments)
    success(f"Checked out to: {folder}")
