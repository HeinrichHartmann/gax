"""CLI commands for Google Docs operations."""

import sys
import click
from pathlib import Path

from ..ui import gax_command, confirm_and_pull, success
from .. import docs
from . import Tab, Doc


def _compute_plan(file: Path, content: str):
    """Compute push plan for a tab file.

    Returns (plan, section) tuple.

    Revision guard (ADR 037): if the remote revisionId differs from the
    stored one, refuse immediately — the user must pull first.
    """
    from .doc import parse_multipart, extract_doc_id, _fetch_doc, _flatten_tabs
    from .diff_push import compute_three_way_plan, ThreeWayPlan

    section = parse_multipart(file.read_text(encoding="utf-8"))[0]
    document_id = extract_doc_id(section.source)
    tab_name = section.section_title

    doc = _fetch_doc(document_id)
    flat = _flatten_tabs(doc.get("tabs", []))

    matched_tab = None
    for tab, info in flat:
        if info.title == tab_name:
            matched_tab = tab
            break

    if matched_tab is None:
        return ThreeWayPlan(
            ops=[],
            mutations=[],
            summary_lines=[],
            error=f"Tab '{tab_name}' not found in document",
        ), section

    doc_tab = matched_tab.get("documentTab", {})
    body = doc_tab.get("body", {}).get("content", [])
    lists = doc_tab.get("lists") or doc.get("lists")
    remote_revision = doc.get("revisionId", "")

    stored_revision = section.revision
    tab_id = doc_tab.get("tabProperties", {}).get("tabId", "")

    # --- Revision guard (ADR 037) ---
    # Revision mismatch = immediate refusal; no drift/conflict analysis.
    if stored_revision and remote_revision and stored_revision != remote_revision:
        click.echo(
            f"Remote changed (rev {stored_revision} → {remote_revision}). "
            f"Pull first.",
            err=True,
        )
        sys.exit(1)

    plan = compute_three_way_plan(
        local_markdown=content,
        remote_body=body,
        remote_revision=remote_revision,
        stored_revision=stored_revision,
        tab_id=tab_id,
        lists=lists,
    )

    return plan, section


def _do_force_replace_push(
    t: "Tab",
    file: Path,
    body: "Path | None",
    yes: bool,
) -> None:
    """Full-replace push for a single tab."""
    from .doc import parse_multipart
    from .ir import from_markdown, check_unsupported

    diff_text = t.diff(body=body)
    if diff_text is None:
        click.echo("No differences to push.")
        return
    if not yes:
        click.echo("Changes to push (full-replace):")
        click.echo("-" * 40)
        click.echo(diff_text)
        click.echo("-" * 40)
        raw_for_warn = body.read_text(encoding="utf-8") if body else (
            parse_multipart(file.read_text(encoding="utf-8"))[0].content
        )
        for w in check_unsupported(from_markdown(raw_for_warn)):
            click.echo(f"  Warning: {w.feature}: {w.detail}")
        click.echo(
            "Warning: full-replace destroys all non-markdown formatting "
            "(colors, fonts, alignment, comments, suggestions, images)."
        )
        if not click.confirm("Push these changes?"):
            click.echo("Aborted.")
            return
    t.push(body=body)
    success("Pushed successfully (full-replace).")


def _do_plan_push(
    t: "Tab",
    file: Path,
    content: str,
    yes: bool,
) -> None:
    """Plan-driven push for a single tab."""
    plan, _section = _compute_plan(file, content)

    if plan.is_empty:
        click.echo("No differences to push.")
        return

    if plan.error:
        # Offer full-replace fallback with explicit destructiveness warning
        click.echo(f"Patch cannot be applied: {plan.error}")
        click.echo(
            "Warning: falling back to full-replace will destroy all "
            "non-markdown formatting (colors, fonts, alignment, comments, "
            "suggestions, images)."
        )
        if not yes:
            if not click.confirm("Fall back to full-replace?", default=False):
                click.echo("Aborted.")
                return
        # Full-replace fallback
        if t.diff() is None:
            click.echo("No differences to push.")
            return
        if not yes:
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                return
        t.push()
        success("Pushed successfully (full-replace fallback).")
        return

    # Clean patch — show plan summary and confirm
    click.echo("Patch operations:")
    click.echo("-" * 40)
    for line in plan.summary_lines:
        click.echo(line)
    click.echo("-" * 40)
    if not yes:
        if not click.confirm("Apply patch?"):
            click.echo("Aborted.")
            return
    t.push(patch=True)
    success("Patched successfully.")


def _push_tab_plan_or_force(
    t: "Tab",
    content: str,
    yes: bool,
    force_replace: bool,
    label: str = "",
) -> bool:
    """Plan-driven push for one tab in a folder. Returns True if pushed."""
    from .doc import parse_multipart
    from .ir import from_markdown, check_unsupported

    prefix = f"[{label}] " if label else ""

    if not force_replace:
        plan, section = _compute_plan(t.path, content)

        if plan.is_empty:
            return False  # no differences

        if plan.error:
            # Non-fatal: offer full-replace fallback
            click.echo(f"{prefix}Patch cannot be applied: {plan.error}")
            click.echo(
                f"{prefix}Warning: full-replace will destroy all non-markdown "
                "formatting (colors, fonts, alignment, comments, suggestions, images)."
            )
            if yes:
                force_replace = True  # silent fallback
            else:
                if not click.confirm(
                    f"{prefix}Fall back to full-replace?", default=False
                ):
                    click.echo("Aborted.")
                    sys.exit(1)
                force_replace = True

        if not force_replace:
            # Clean patch — show summary and confirm
            click.echo(f"{prefix}Patch operations:")
            click.echo("-" * 40)
            for line in plan.summary_lines:
                click.echo(line)
            click.echo("-" * 40)
            if not yes:
                if not click.confirm("Apply patch?"):
                    click.echo("Aborted.")
                    sys.exit(1)
            t.push(patch=True)
            return True

    # FULL-REPLACE path
    diff_text = t.diff()
    if diff_text is None:
        return False  # no differences

    if not (force_replace and yes):
        click.echo(f"{prefix}Changes to push (full-replace):")
        click.echo("-" * 40)
        click.echo(diff_text)
        click.echo("-" * 40)
        section = parse_multipart(t.path.read_text(encoding="utf-8"))[0]
        for w in check_unsupported(from_markdown(section.content)):
            click.echo(f"  Warning: {w.feature}: {w.detail}")
        click.echo(
            "Warning: full-replace destroys all non-markdown formatting "
            "(colors, fonts, alignment, comments, suggestions, images)."
        )
        if not yes:
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                sys.exit(1)

    t.push()
    return True


def _has_unpushed_edits(file: Path) -> str | None:
    """Check if a doc tab file has unpushed local edits.

    Compares local content against render_baseline(baseline_hash).
    Returns a description of the situation, or None if clean / no baseline.
    """
    from .doc import parse_multipart, render_baseline

    section = parse_multipart(file.read_text(encoding="utf-8"))[0]
    baseline_hash = section.baseline

    if not baseline_hash:
        return None  # no baseline — can't check, caller warns separately

    base_md = render_baseline(baseline_hash)
    if base_md is None:
        return None  # baseline blob missing from store — degrade gracefully

    local_content = section.content
    if local_content.rstrip() != base_md.rstrip():
        return (
            "You have unpushed local edits. "
            "Push first, or pull --force to discard."
        )

    return None  # clean — local matches baseline


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
@click.option("--force", is_flag=True, help="Discard unpushed local edits and pull anyway")
@gax_command
def doc_tab_pull(file: Path, yes: bool, force: bool):
    """Pull latest content for a single tab.

    Refuses if you have unpushed local edits (local content differs from
    the stored baseline). Use ``--force`` to discard local edits and pull
    anyway.
    """
    edit_msg = _has_unpushed_edits(file)
    if edit_msg and not force:
        click.echo(edit_msg, err=True)
        sys.exit(1)
    if edit_msg and force:
        click.echo("Warning: discarding unpushed local edits.", err=True)
    confirm_and_pull(Tab.from_file(file), yes=yes)


@doc_tab.command("diff")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--text",
    "show_text",
    is_flag=True,
    help="Show unified text diff instead of plan summary.",
)
@gax_command
def doc_tab_diff(file: Path, show_text: bool):
    """Show diff between local file and remote tab.

    By default shows the same plan that ``push`` would apply (remote vs
    local diff). Use ``--text`` for a traditional unified diff.
    """
    if show_text:
        diff_text = Tab.from_file(file).diff()
        if diff_text is None:
            click.echo("No differences.")
        else:
            click.echo(diff_text)
        return

    from .doc import parse_multipart
    from . import native_md as _native_md

    section = parse_multipart(file.read_text(encoding="utf-8"))[0]
    content = _native_md.inline_images_from_store(section.content)

    plan_result, _ = _compute_plan(file, content)

    if plan_result.is_empty:
        click.echo("No differences.")
        return

    if plan_result.error:
        click.echo(f"Plan error: {plan_result.error}")

    for line in plan_result.summary_lines:
        click.echo(line)


@doc_tab.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--force-replace",
    is_flag=True,
    help="Full-replace push (destroys all non-markdown formatting). Skips patch attempt.",
)
@click.option(
    "--bulk",
    is_flag=True,
    hidden=True,
    help="Deprecated: use --force-replace instead.",
)
@click.option(
    "--body",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Push content from this external markdown file instead of the tracking file.",
)
@gax_command
def doc_tab_push(file: Path, yes: bool, force_replace: bool, bulk: bool, body: Path | None):
    """Push local changes to a single tab.

    Computes a minimal diff (remote vs local) and applies run-level
    patches that preserve non-markdown formatting (colors, fonts,
    alignment, comments, suggestions, images).

    A revision guard refuses the push when the remote has changed since
    your last pull — run ``gax pull`` first in that case.

    Use ``--force-replace`` to force a full-replace push (faster, but destroys
    all non-markdown formatting).

    When patch cannot be applied (e.g. structural changes), gax offers to
    fall back to full-replace. With ``-y`` the fallback happens silently.

    Use ``--body`` to push content from an external markdown file. The tracking
    file is updated in place so subsequent ``pull`` round-trips stay consistent.
    ``--body`` is only supported with the full-replace path.
    """
    from .doc import parse_multipart
    from . import native_md as _native_md

    if bulk:
        click.echo(
            "Warning: --bulk is deprecated, use --force-replace instead.",
            err=True,
        )
        force_replace = True

    t = Tab.from_file(file)

    if body:
        # --body only supported with force-replace
        force_replace = True

    # Resolve content
    if body:
        raw = body.read_text(encoding="utf-8")
    else:
        raw = parse_multipart(file.read_text(encoding="utf-8"))[0].content
    content = _native_md.inline_images_from_store(raw)

    if force_replace:
        _do_force_replace_push(t, file, body, yes)
        return

    # PATCH path (default) — plan-driven diff
    _do_plan_push(t, file, content, yes)


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
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "tree"]),
    default="md",
    help="Output format: md (default) or tree (doc-tree/v1 YAML)",
)
@gax_command
def doc_clone(url: str, output: Path | None, with_comments: bool, quiet: bool, fmt: str):
    """Clone a Google Doc to a local file.

    \b
    Formats:
        md   (default) — .doc.gax.md with YAML frontmatter + markdown body
        tree           — .doc.gax.yaml with doc-tree/v1 compressed YAML

    Clones a single tab. For multi-tab documents, use 'gax doc checkout'.
    """
    from .doc import extract_doc_id, get_tabs_list

    file_path = Tab.from_url(url).clone(output=output, with_comments=with_comments, fmt=fmt)
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
@click.option("--force", is_flag=True, help="Discard unpushed local edits and pull anyway")
@gax_command
def doc_pull(file: Path, with_comments: bool, yes: bool, force: bool):
    """Pull latest content from Google Docs to local file.

    Refuses if you have unpushed local edits (local content differs from
    the stored baseline). Use ``--force`` to discard local edits and pull
    anyway.
    """
    edit_msg = _has_unpushed_edits(file)
    if edit_msg and not force:
        click.echo(edit_msg, err=True)
        sys.exit(1)
    if edit_msg and force:
        click.echo("Warning: discarding unpushed local edits.", err=True)
    confirm_and_pull(Tab.from_file(file), yes=yes, with_comments=with_comments)


@doc.command("push")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--force-replace",
    is_flag=True,
    help="Full-replace push for all tabs (destroys non-markdown formatting).",
)
@click.option(
    "--bulk",
    is_flag=True,
    hidden=True,
    help="Deprecated: use --force-replace instead.",
)
@gax_command
def doc_push(folder: Path, yes: bool, force_replace: bool, bulk: bool):
    """Push all changed tabs in a checkout folder to Google Docs.

    Computes a minimal diff (remote vs local) per tab and applies
    run-level patches that preserve non-markdown formatting.

    Use ``--force-replace`` to force full-replace for all tabs (destroys
    non-markdown formatting).

    When patch cannot be applied for a tab, gax offers to fall back to
    full-replace for that tab individually. With ``-y`` the fallback
    happens silently.
    """
    from .doc import (
        parse_multipart, _known_tab_files, _read_checkout_metadata,
        _refresh_revision_in_file,
    )
    from . import native_md as _native_md

    if bulk:
        click.echo(
            "Warning: --bulk is deprecated, use --force-replace instead.",
            err=True,
        )
        force_replace = True

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
        did_push = _push_tab_plan_or_force(
            t, content=content, yes=yes, force_replace=force_replace, label=label,
        )
        if did_push:
            pushed += 1
            # After a successful push the document-level revisionId has
            # changed.  Update the revision in all sibling tab files so the
            # revision guard (ADR 037) doesn't trip on our own push (gax-zo1).
            new_rev = parse_multipart(
                tab_file.read_text(encoding="utf-8")
            )[0].revision
            if new_rev:
                for sibling in tab_files:
                    if sibling != tab_file:
                        _refresh_revision_in_file(sibling, new_rev)

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
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "tree"]),
    default="md",
    help="Output format: md (default) or tree (doc-tree/v1 YAML)",
)
@gax_command
def doc_checkout(url: str, output: Path | None, with_comments: bool, fmt: str):
    """Checkout all tabs to individual files in a folder.

    \b
    Formats:
        md   (default) — .doc.gax.md.d/ folder with .doc.gax.md files
        tree           — .doc.gax.yaml.d/ folder with .doc.gax.yaml files

    Creates a folder with individual files for each tab.
    """
    folder = Doc.from_url(url).checkout(output=output, with_comments=with_comments, fmt=fmt)
    success(f"Checked out to: {folder}")
