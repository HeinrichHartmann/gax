"""Google Docs sync for gax.

Re-exports from doc.py (business logic) and defines CLI commands.
CLI commands will move to cli.py in a subsequent commit.
"""

import sys
from pathlib import Path
from typing import Optional

import click

from .. import docs
from ..ui import success, error

# Re-export business logic from doc.py
from .doc import (  # noqa: F401
    DocSection,
    Comment,
    CommentReply,
    format_section,
    format_multipart,
    parse_multipart,
    extract_doc_id,
    pull_doc,
    pull_single_tab,
    get_tabs_list,
    create_tab_with_content,
    update_tab_content,
    _add_comments_to_sections,
    Tab,
    Doc,
)

# Also re-export native_md for cli_helper.py imports like `from .gdoc import native_md`
from . import native_md  # noqa: F401


# =============================================================================
# CLI commands (to be moved to cli.py)
# =============================================================================


@docs.section("resource")
@click.group()
def doc():
    """Google Docs operations"""
    pass


# --- Tab subcommand group ---


@doc.group()
def tab():
    """Single tab operations"""
    pass


@tab.command("list")
@click.argument("url")
def tab_list(url: str):
    """List tabs in a document (TSV output)."""
    try:
        Doc().tab_list(url, sys.stdout)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("import")
@click.argument("url")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output tracking file (default: <filename>.tab.gax.md)",
)
def tab_import(url: str, file: Path, output: Optional[Path]):
    """Import a markdown file as a new tab in a document."""
    try:
        tracking_path = Doc().tab_import(url, file, output=output)
        success(f"Created: {tracking_path}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)
    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@tab.command("clone")
@click.argument("url")
@click.argument("tab_name")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <tab>.tab.gax.md)",
)
def tab_clone(url: str, tab_name: str, output: Optional[Path]):
    """Clone a single tab to a .tab.gax.md file."""
    try:
        file_path = Tab().clone(url, output=output, tab_name=tab_name)
        success(f"Created: {file_path}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)
    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@tab.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def tab_pull(file: Path):
    """Pull latest content for a single tab."""
    try:
        Tab().pull(file)
        success(f"Updated: {file}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)
    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@tab.command("diff")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def tab_diff(file: Path):
    """Show diff between local file and remote tab."""
    try:
        diff_text = Tab().diff(file)
        if diff_text is None:
            click.echo("No differences.")
        else:
            click.echo(diff_text)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--patch",
    "use_patch",
    is_flag=True,
    help="Incremental push: apply only changed elements (experimental)",
)
def tab_push(file: Path, yes: bool, use_patch: bool):
    """Push local changes to a single tab (with confirmation).

    The default push path is full-replace (see ADR 023). The ``--patch`` flag
    selects an **experimental** incremental push path (ADR 027) that diffs the
    local markdown against the live document and applies only the changed
    elements. The ``--patch`` path is under evaluation and may fail on
    structural changes; when in doubt, omit the flag.
    """
    try:
        t = Tab()

        if use_patch:
            from .diff_push import preview_diff

            section = parse_multipart(file.read_text(encoding="utf-8"))[0]
            source_url = section.source
            tab_name = section.section_title
            document_id = extract_doc_id(source_url)

            from . import native_md as _native_md

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

            t.push(file, use_patch=True)
            success("Patched successfully.")
        else:
            diff_text = t.diff(file)
            if diff_text is None:
                click.echo("No differences to push.")
                return

            click.echo("Changes to push:")
            click.echo("-" * 40)
            click.echo(diff_text)
            click.echo("-" * 40)

            from .md2docs import parse_markdown, check_unsupported

            section = parse_multipart(file.read_text(encoding="utf-8"))[0]
            push_warnings = check_unsupported(parse_markdown(section.content))
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

            t.push(file)
            success("Pushed successfully.")

    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


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
def clone(url: str, output: Optional[Path], with_comments: bool, quiet: bool):
    """Clone a Google Doc to a local .doc.gax.md file.

    Clones a single tab. For multi-tab documents, use 'gax doc checkout'.
    """
    try:
        file_path = Tab().clone(
            url, output=output, with_comments=with_comments
        )
        success(f"Created: {file_path}")

        if not quiet:
            # Check if multi-tab to show hint
            document_id = extract_doc_id(url)
            tabs = get_tabs_list(document_id)
            if len(tabs["tabs"]) > 1:
                first_tab = tabs["tabs"][0]["title"]
                click.echo(
                    f'  Tab "{first_tab}" cloned (1 of {len(tabs["tabs"])} tabs).\n'
                    f"  For all tabs: gax doc checkout {url}"
                )

    except ValueError as e:
        error(str(e))
        sys.exit(1)
    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@doc.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-comments",
    is_flag=True,
    help="Include document comments as separate sections",
)
def pull(file: Path, with_comments: bool):
    """Pull latest content from Google Docs to local file."""
    try:
        Tab().pull(file, with_comments=with_comments)
        success(f"Updated: {file}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)
    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)


@doc.command("checkout")
@click.argument("url")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output folder (default: <title>.doc.gax.md.d)",
)
def checkout(url: str, output: Optional[Path]):
    """Checkout all tabs to individual files in a folder.

    Creates a folder with individual .doc.gax.md files for each tab.
    """
    try:
        folder = Doc().clone(url, output=output)
        success(f"Checked out to: {folder}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)
    except Exception as e:
        error(f"Error: {e}")
        sys.exit(1)
