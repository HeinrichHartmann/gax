"""CLI commands for Google Docs operations."""

import sys
import click
from pathlib import Path

from ..ui import gax_command, confirm_and_pull, success
from .. import docs
from . import Tab, Doc


def _push_tab_patch_or_bulk(
    t: "Tab",
    content: str,
    yes: bool,
    bulk: bool,
    label: str = "",
) -> bool:
    """Patch-first push for one tab. Returns True if pushed, False if no diff."""
    from .doc import parse_multipart, extract_doc_id
    from .ir import from_markdown, check_unsupported

    prefix = f"[{label}] " if label else ""

    if not bulk:
        from .diff_push import preview_diff

        section = parse_multipart(t.path.read_text(encoding="utf-8"))[0]
        document_id = extract_doc_id(section.source)
        tab_name = section.section_title

        preview = preview_diff(document_id, tab_name, content)

        if not preview.ops:
            return False  # no differences

        if preview.error:
            if preview.fatal:
                click.echo(f"{prefix}Error: {preview.error}", err=True)
                sys.exit(1)
            # Non-fatal: offer bulk fallback
            click.echo(f"{prefix}Patch cannot be applied: {preview.error}")
            if yes:
                bulk = True  # silent fallback
            else:
                if not click.confirm(f"{prefix}Fall back to bulk push?", default=False):
                    click.echo("Aborted.")
                    sys.exit(1)
                bulk = True

        if not bulk:
            # Clean patch — show summary and confirm
            click.echo(f"{prefix}Patch operations:")
            click.echo("-" * 40)
            for line in preview.summary_lines:
                click.echo(line)
            click.echo("-" * 40)
            if not yes:
                if not click.confirm("Apply patch?"):
                    click.echo("Aborted.")
                    sys.exit(1)
            t.push(patch=True)
            return True

    # BULK path
    diff_text = t.diff()
    if diff_text is None:
        return False  # no differences

    if not (bulk and yes):
        click.echo(f"{prefix}Changes to push:")
        click.echo("-" * 40)
        click.echo(diff_text)
        click.echo("-" * 40)
        section = parse_multipart(t.path.read_text(encoding="utf-8"))[0]
        for w in check_unsupported(from_markdown(section.content)):
            click.echo(f"  Warning: {w.feature}: {w.detail}")
        click.echo(
            "Warning: markdown cannot faithfully represent a Google Doc. "
            "Non-markdown formatting (colors, fonts, alignment, comments, "
            "suggestions, images) may be lost."
        )
        if not yes:
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                sys.exit(1)

    t.push()
    return True


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
@click.option("--bulk", is_flag=True, help="Full-replace push, skipping patch attempt")
@click.option(
    "--body",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Push content from this external markdown file instead of the tracking file.",
)
@gax_command
def doc_tab_push(file: Path, yes: bool, bulk: bool, body: Path | None):
    """Push local changes to a single tab.

    Patch is the default: applies only changed elements, preserving collaborator
    formatting, comments, and suggestions. Use ``--bulk`` to force a full-replace
    push (faster, but destroys all non-markdown formatting).

    When patch cannot be applied (e.g. structural changes like adding a table),
    gax offers to fall back to bulk. With ``-y`` the fallback happens silently.

    Use ``--body`` to push content from an external markdown file. The tracking
    file is updated in place so subsequent ``pull`` round-trips stay consistent.
    ``--body`` is only supported with the bulk path.
    """
    from .doc import parse_multipart, extract_doc_id
    from . import native_md as _native_md

    t = Tab.from_file(file)

    if body:
        # TODO: --body with patch
        bulk = True

    # Resolve content
    if body:
        raw = body.read_text(encoding="utf-8")
    else:
        raw = parse_multipart(file.read_text(encoding="utf-8"))[0].content
    content = _native_md.inline_images_from_store(raw)

    if bulk:
        diff_text = t.diff(body=body)
        if diff_text is None:
            click.echo("No differences to push.")
            return
        if not (bulk and yes):
            from .ir import from_markdown, check_unsupported
            click.echo("Changes to push:")
            click.echo("-" * 40)
            click.echo(diff_text)
            click.echo("-" * 40)
            raw_for_warn = body.read_text(encoding="utf-8") if body else (
                parse_multipart(file.read_text(encoding="utf-8"))[0].content
            )
            for w in check_unsupported(from_markdown(raw_for_warn)):
                click.echo(f"  Warning: {w.feature}: {w.detail}")
            click.echo(
                "Warning: markdown cannot faithfully represent a Google Doc. "
                "Non-markdown formatting (colors, fonts, alignment, comments, "
                "suggestions, images) may be lost."
            )
            if not yes:
                if not click.confirm("Push these changes?"):
                    click.echo("Aborted.")
                    return
        t.push(body=body)
        success("Pushed successfully.")
        return

    # PATCH path (default)
    from .diff_push import preview_diff

    section = parse_multipart(file.read_text(encoding="utf-8"))[0]
    document_id = extract_doc_id(section.source)
    tab_name = section.section_title

    preview = preview_diff(document_id, tab_name, content)

    if not preview.ops:
        click.echo("No differences to push.")
        return

    if preview.error:
        if preview.fatal:
            click.echo(f"Error: {preview.error}", err=True)
            sys.exit(1)
        click.echo(f"Patch cannot be applied: {preview.error}")
        if not yes:
            if not click.confirm("Fall back to bulk push?", default=False):
                click.echo("Aborted.")
                return
        # Bulk fallback
        if t.diff(body=body) is None:
            click.echo("No differences to push.")
            return
        if not yes:
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                return
        t.push(body=body)
        success("Pushed successfully (bulk fallback).")
        return

    # Clean patch
    click.echo("Patch operations:")
    click.echo("-" * 40)
    for line in preview.summary_lines:
        click.echo(line)
    click.echo("-" * 40)
    if not yes:
        if not click.confirm("Apply patch?"):
            click.echo("Aborted.")
            return
    t.push(patch=True)
    success("Patched successfully.")


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


@doc.command("push")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--bulk", is_flag=True, help="Full-replace push for all tabs")
@gax_command
def doc_push(folder: Path, yes: bool, bulk: bool):
    """Push all changed tabs in a checkout folder to Google Docs.

    Patch is the default: applies only changed elements per tab, preserving
    collaborator formatting. Use ``--bulk`` to force full-replace for all tabs.

    When patch cannot be applied for a tab, gax offers to fall back to bulk
    for that tab individually. With ``-y`` the fallback happens silently.
    """
    from .doc import parse_multipart, _known_tab_files, _read_checkout_metadata
    from . import native_md as _native_md

    metadata = _read_checkout_metadata(folder)
    tab_files = list(_known_tab_files(folder, metadata))

    if not tab_files:
        click.echo("No tab files found.")
        return

    pushed = 0
    for tab_file in tab_files:
        t = Tab.from_file(tab_file)
        label = tab_file.relative_to(folder).as_posix()
        section = parse_multipart(tab_file.read_text(encoding="utf-8"))[0]
        content = _native_md.inline_images_from_store(section.content)
        did_push = _push_tab_patch_or_bulk(t, content=content, yes=yes, bulk=bulk, label=label)
        if did_push:
            pushed += 1

    if pushed == 0:
        click.echo("No differences to push.")
    else:
        success(f"Pushed {pushed} tab(s).")


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
