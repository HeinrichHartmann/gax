"""CLI commands for Gmail thread, draft, and mailbox operations."""

import sys
import click
from pathlib import Path

from ..ui import handle_errors, _confirm_and_push, success, error
from .. import docs
from . import Thread, Mailbox, Draft


# =============================================================================
# Mail thread commands
# =============================================================================


@docs.section("resource")
@click.group()
def mail_group():
    """Individual email thread operations (clone, pull, reply)"""
    pass


@mail_group.command("clone")
@click.argument("thread_id_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file",
)
@handle_errors
def mail_clone(thread_id_or_url, output):
    """Clone a single email thread to a local .mail.gax.md file.

    \b
    Examples:
        gax mail clone 19d0bed1cddbab6d
        gax mail clone "https://mail.google.com/..."
        gax mail clone 19d0bed1cddbab6d -o thread.mail.gax.md
    """
    path = Thread.from_url_or_id(thread_id_or_url).clone(output=output)
    success(f"Created: {path}")


@mail_group.command("pull")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@handle_errors
def mail_pull(path):
    """Pull latest messages for .mail.gax.md file(s).

    Single file:

        gax mail pull thread.mail.gax.md

    Folder (updates all .mail.gax.md files):

        gax mail pull Inbox/
    """
    Thread(path=path).pull()
    success(f"Updated: {path}")


@mail_group.command("reply")
@click.argument("file_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: Re_<subject>.draft.gax.md)",
)
@handle_errors
def mail_reply(file_or_url, output):
    """Create a reply draft from a thread.

    Examples:

        gax mail reply Project_Update.mail.gax.md
        gax mail reply "https://mail.google.com/mail/u/0/#inbox/abc123"
        gax mail reply thread.mail.gax.md -o my_reply.draft.gax.md
    """
    file_path = Path(file_or_url)
    if file_path.exists():
        thread = Thread(path=file_path)
    else:
        thread = Thread.from_url_or_id(file_or_url)
    out_path = thread.reply(output=output)
    success(f"Created: {out_path}")
    click.echo(f"Edit the file, then run: gax draft push {out_path}")


# =============================================================================
# Mailbox commands
# =============================================================================


@docs.section("resource")
@click.group(invoke_without_command=True)
@click.option(
    "-q", "--query", default="in:inbox", help="Search query (default: in:inbox)"
)
@click.option("--limit", default=20, help="Maximum results (default: 20)")
@click.pass_context
@handle_errors
def mailbox_group(ctx, query, limit):
    """Search/list Gmail threads and bulk label operations.

    Without subcommand, lists threads matching query (TSV output).

    \b
    Examples:
        gax mailbox                        # List inbox
        gax mailbox -q "from:alice"        # Search
        gax mailbox clone                  # Clone for bulk labeling
    """
    if ctx.invoked_subcommand is None:
        Mailbox().list(sys.stdout, query=query, limit=limit)


@mailbox_group.command("fetch")
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
@handle_errors
def mailbox_fetch(output, query, limit):
    """Fetch full threads matching query into a folder."""
    cloned, skipped = Mailbox().fetch(query=query, limit=limit, output=output)
    success(f"Cloned: {cloned}, Skipped: {skipped} (already present)")


@mailbox_group.command("clone")
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
@handle_errors
def mailbox_clone_cmd(output, query, limit):
    """Clone threads from Gmail for bulk labeling."""
    file_path = Mailbox().clone(query=query, limit=limit, output=Path(output))
    success(f"Cloned to: {file_path}")


@mailbox_group.command("pull")
@click.argument("file", type=click.Path(exists=True))
@handle_errors
def mailbox_pull(file):
    """Update a .gax.md file by re-fetching from Gmail."""
    Mailbox(path=Path(file)).pull()
    success(f"Updated: {file}")


@mailbox_group.command("plan")
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    default="mailbox.plan.yaml",
    help="Output file (default: mailbox.plan.yaml)",
)
@handle_errors
def mailbox_plan_cmd(file, output):
    """Generate plan from edited list file."""
    import yaml

    plan = Mailbox(path=Path(file)).compute_plan()

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


@mailbox_group.command("apply")
@click.argument("plan_file", type=click.Path(exists=True))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def mailbox_apply(plan_file, yes):
    """Apply label changes from plan."""
    import yaml

    with open(plan_file) as f:
        plan = yaml.safe_load(f)

    changes = plan.get("changes", [])
    if not changes:
        click.echo("No changes in plan.")
        return

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


# =============================================================================
# Draft commands
# =============================================================================


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
