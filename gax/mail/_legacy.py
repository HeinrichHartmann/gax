"""Gmail sync for gax — legacy CLI commands being moved to cli.py."""

import logging
import sys
from pathlib import Path
from typing import Optional

import click

from ..ui import success, error
from .. import docs as doc

from .shared import (  # noqa: F401 — re-exported for backward compat
    Attachment as Attachment,
    Message as Message,
    MailSection as MailSection,
    _mail_section_to_multipart as _mail_section_to_multipart,
    format_section as format_section,
    format_multipart as format_multipart,
    extract_thread_id as extract_thread_id,
    _get_header as _get_header,
    pull_thread as pull_thread,
)
from .thread import (  # noqa: F401 — re-exported for backward compat
    Thread as Thread,
    _is_thread_id as _is_thread_id,
    _pull_single_file as _pull_single_file,
)
from .mailbox import (  # noqa: F401 — re-exported for backward compat
    Mailbox as Mailbox,
    SYS_LABEL_TO_ABBREV as SYS_LABEL_TO_ABBREV,
    ABBREV_TO_SYS_LABEL as ABBREV_TO_SYS_LABEL,
    CAT_LABEL_TO_ABBREV as CAT_LABEL_TO_ABBREV,
    ABBREV_TO_CAT_LABEL as ABBREV_TO_CAT_LABEL,
    TRACKED_SYS_LABELS as TRACKED_SYS_LABELS,
    TRACKED_CAT_LABELS as TRACKED_CAT_LABELS,
    _tsv_quote as _tsv_quote,
    _parse_tsv_line as _parse_tsv_line,
    _write_gax_file as _write_gax_file,
    _parse_gax_header as _parse_gax_header,
    _parse_gax_content as _parse_gax_content,
    _make_filename as _make_filename,
    _get_existing_thread_ids as _get_existing_thread_ids,
    _get_thread_summary as _get_thread_summary,
    _get_thread_for_relabel as _get_thread_for_relabel,
    _relabel_fetch_threads as _relabel_fetch_threads,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Thread CLI commands (will move to cli.py in Step 4)
# =============================================================================


@doc.section("resource")
@click.group()
def thread():
    """Individual email thread operations (clone, pull, reply)"""
    pass


@thread.command()
@click.argument("thread_id_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file",
)
def clone(thread_id_or_url: str, output: Optional[Path]):
    """Clone a single email thread to a local .mail.gax.md file.

    \b
    Examples:
        gax mail clone 19d0bed1cddbab6d
        gax mail clone "https://mail.google.com/..."
        gax mail clone 19d0bed1cddbab6d -o thread.mail.gax.md
    """
    try:
        file_path = Thread().clone(url=thread_id_or_url, output=output)
        success(f"Created: {file_path}")
    except (ValueError, Exception) as e:
        error(str(e))
        sys.exit(1)


@thread.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def pull(path: Path):
    """Pull latest messages for .mail.gax.md file(s).

    Single file:

        gax mail pull thread.mail.gax.md

    Folder (updates all .mail.gax.md files):

        gax mail pull Inbox/
    """
    try:
        Thread().pull(path)
        success(f"Updated: {path}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)


@thread.command()
@click.argument("file_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: Re_<subject>.draft.gax.md)",
)
def reply(file_or_url: str, output: Optional[Path]):
    """Create a reply draft from a thread."""
    try:
        out_path = Thread().reply(file_or_url, output=output)
        success(f"Created: {out_path}")
        click.echo(f"Edit the file, then run: gax draft push {out_path}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)


# =============================================================================
# Mailbox CLI commands (will move to cli.py in Step 4)
# =============================================================================


@doc.section("resource")
@click.group(invoke_without_command=True)
@click.option(
    "-q", "--query", default="in:inbox", help="Search query (default: in:inbox)"
)
@click.option("--limit", default=20, help="Maximum results (default: 20)")
@click.pass_context
def mailbox(ctx, query: str, limit: int):
    """Search/list Gmail threads and bulk label operations.

    Without subcommand, lists threads matching query (TSV output).

    \b
    Examples:
        gax mailbox                        # List inbox
        gax mailbox -q "from:alice"        # Search
        gax mailbox clone                  # Clone for bulk labeling
    """
    if ctx.invoked_subcommand is None:
        try:
            Mailbox().list(sys.stdout, query=query, limit=limit)
        except ValueError as e:
            error(str(e))
            sys.exit(1)


@mailbox.command("fetch")
@click.option(
    "-o",
    "--output",
    default="mailbox.gax.md.d",
    type=click.Path(path_type=Path),
    help="Output folder (default: mailbox.gax.md.d)",
)
@click.option(
    "-q", "--query", default="in:inbox", help="Search query (default: in:inbox)"
)
@click.option("--limit", default=50, help="Maximum threads (default: 50)")
def mailbox_fetch(output: Path, query: str, limit: int):
    """Fetch full threads matching query into a folder."""
    try:
        cloned, skipped = Mailbox().fetch(query=query, limit=limit, output=output)
        success(f"Cloned: {cloned}, Skipped: {skipped} (already present)")
    except ValueError as e:
        error(str(e))
        sys.exit(1)


@mailbox.command("clone")
@click.option(
    "-o",
    "--output",
    default="mailbox.gax.md",
    help="Output file (default: mailbox.gax.md)",
)
@click.option(
    "-q", "--query", default="in:inbox", help="Search query (default: in:inbox)"
)
@click.option("--limit", default=50, help="Maximum threads (default: 50)")
def mailbox_clone(output: str, query: str, limit: int):
    """Clone threads from Gmail for bulk labeling."""
    try:
        file_path = Mailbox().clone(query=query, limit=limit, output=Path(output))
        success(f"Cloned to: {file_path}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)


@mailbox.command("pull")
@click.argument("file", type=click.Path(exists=True))
def relabel_pull(file: str):
    """Update a .gax.md file by re-fetching from Gmail."""
    try:
        Mailbox().pull(Path(file))
        success(f"Updated: {file}")
    except ValueError as e:
        error(str(e))
        sys.exit(1)


@mailbox.command("plan")
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    default="mailbox.plan.yaml",
    help="Output file (default: mailbox.plan.yaml)",
)
def mailbox_plan(file: str, output: str):
    """Generate plan from edited list file."""
    import yaml

    try:
        plan = Mailbox().compute_plan(Path(file))

        if not plan["changes"]:
            click.echo("No changes to apply.")
            return

        path = Path(output)
        with open(path, "w") as f:
            yaml.dump(
                plan, f, default_flow_style=False, allow_unicode=True, sort_keys=False
            )

        changes = plan["changes"]
        click.echo(f"Wrote {len(changes)} changes to {output}")

        sys_add_count = sum(1 for c in changes if c.get("add_sys"))
        sys_remove_count = sum(1 for c in changes if c.get("remove_sys"))
        cat_change_count = sum(
            1 for c in changes if c.get("add_cat") or c.get("remove_cat")
        )
        add_count = sum(1 for c in changes if c.get("add"))
        remove_count = sum(1 for c in changes if c.get("remove"))

        if sys_add_count or sys_remove_count:
            click.echo(f"  System label changes: {sys_add_count + sys_remove_count}")
        if cat_change_count:
            click.echo(f"  Category changes: {cat_change_count}")
        if add_count:
            click.echo(f"  Add user labels: {add_count}")
        if remove_count:
            click.echo(f"  Remove user labels: {remove_count}")

    except ValueError as e:
        error(str(e))
        sys.exit(1)


@mailbox.command("apply")
@click.argument("plan_file", type=click.Path(exists=True))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def relabel_apply(plan_file: str, yes: bool):
    """Apply label changes from plan."""
    import yaml

    try:
        with open(plan_file) as f:
            plan = yaml.safe_load(f)

        changes = plan.get("changes", [])
        if not changes:
            click.echo("No changes in plan.")
            return

        # Show summary
        click.echo(f"Plan: {plan_file}")
        click.echo(f"Changes: {len(changes)}")
        click.echo()

        for change in changes[:10]:
            thread_id = change["id"][:12] + "..."
            actions = []
            if change.get("add_sys"):
                actions.append("+sys:" + ",".join(change["add_sys"]))
            if change.get("remove_sys"):
                actions.append("-sys:" + ",".join(change["remove_sys"]))
            if change.get("add_cat"):
                actions.append("+cat:" + change["add_cat"])
            if change.get("remove_cat"):
                actions.append("-cat:" + change["remove_cat"])
            if change.get("add"):
                actions.append("+" + ",".join(change["add"]))
            if change.get("remove"):
                actions.append("-" + ",".join(change["remove"]))
            click.echo(f"  {thread_id}  {' '.join(actions)}")

        if len(changes) > 10:
            click.echo(f"  ... and {len(changes) - 10} more")

        click.echo()

        if not yes and not click.confirm("Apply these changes?"):
            click.echo("Aborted.")
            return

        succeeded, failed = Mailbox().apply_plan(plan)
        success(f"Applied: {succeeded} threads")
        if failed:
            error(f"Failed: {failed} threads")

    except ValueError as e:
        error(str(e))
        sys.exit(1)
