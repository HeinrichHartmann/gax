"""CLI interface for gax.

Top-level commands (pull, push, clone, checkout) and infrastructure.
Resource-specific commands live in each resource's cli.py module.
"""

import glob
import sys
import click
from pathlib import Path

from . import auth
from . import docs
from .ui import handle_errors, confirm_and_push  # noqa: F401
from .resource import Resource

# Import resource CLI groups — triggers Resource.__init_subclass__ registration
from .gsheet.cli import sheet
from .contacts.cli import contacts
from .gdrive.cli import file_group
from .mail.cli import mail_group, mailbox_group, draft
from .label.cli import mail_label
from .filter.cli import mail_filter
from .gcal.cli import cal_group
from .gtask.cli import task_group
from .form.cli import form
from .gdoc.cli import doc
from .gslides.cli import slides


@click.group()
@click.version_option()
def main():
    """gax - Google Access CLI"""
    from . import ui

    ui.setup_logging()


# =============================================================================
# Top-level commands (dispatch via Resource registry)
# =============================================================================


@docs.section("main")
@main.command("pull")
@click.argument("files", nargs=-1, required=True)
def unified_pull(files: tuple[str, ...]):
    """Pull/update .gax.md file(s) or .gax.md.d folder(s) from their sources.

    Automatically detects file type and calls the appropriate pull command.

    \b
    Examples:
        gax pull file.doc.gax.md           # Pull a single doc
        gax pull *.gax.md                   # Pull all .gax.md files
        gax pull inbox.gax.md notes.doc.gax.md # Pull multiple files
        gax pull folder.doc.gax.md.d/       # Pull a checkout folder
    """
    # Expand globs and '.'
    all_paths: list[Path] = []
    for pattern in files:
        if pattern == ".":
            # Current directory - find all .gax.md files and .gax.md.d folders
            all_paths.extend(Path(".").glob("*.gax.md"))
            all_paths.extend(Path(".").glob("*.gax.md.d"))
        elif "*" in pattern or "?" in pattern:
            # Glob pattern
            all_paths.extend(Path(p) for p in glob.glob(pattern))
        else:
            all_paths.append(Path(pattern))

    if not all_paths:
        click.echo("No .gax.md files or .gax.md.d folders found.", err=True)
        sys.exit(1)

    import logging
    from .ui import operation, success as ui_success, error as ui_error

    logger = logging.getLogger(__name__)

    results = []  # (path, ok, message)

    with operation("Pulling", total=len(all_paths)) as op:
        for path in all_paths:
            if not path.exists():
                results.append((path, False, "not found"))
                op.advance()
                continue

            # Check if it's a folder
            if path.is_dir():
                if not path.name.endswith(".gax.md.d"):
                    results.append((path, False, "not a .gax.md.d folder"))
                    op.advance()
                    continue

                logger.info(f"Pulling {path}/")
            else:
                logger.info(f"Pulling {path}")

            try:
                Resource.from_file(path).pull()
                results.append((path, True, "updated"))
            except Exception as e:
                results.append((path, False, str(e)))

            op.advance()

    # Print results after spinner is done
    success_count = 0
    fail_count = 0
    for path, ok, message in results:
        if ok:
            if message != "cancelled":
                ui_success(f"{path}: {message}")
            success_count += 1
        else:
            if message != "cancelled":
                ui_error(f"{path}: {message}")
            fail_count += 1

    if len(all_paths) > 1:
        summary = f"Done: {success_count}/{len(all_paths)} updated"
        if fail_count:
            ui_error(summary)
        else:
            ui_success(summary)

    if fail_count:
        sys.exit(1)


@docs.section("main")
@main.command("push")
@click.argument("files", nargs=-1, required=True)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--with-formulas", is_flag=True, help="Interpret formulas (sheets only)")
def unified_push(files: tuple[str, ...], yes: bool, with_formulas: bool):
    """Push local .gax.md file(s) or .gax.md.d folder(s) to their sources.

    Automatically detects file type from YAML header and calls
    the appropriate push command. Shows diff/confirmation unless -y is passed.

    \b
    Supported types:
        .sheet.gax.md       Single sheet tab
        .sheet.gax.md.d/    Sheet checkout folder
        .tab.gax.md         Single doc tab
        .draft.gax.md       Gmail draft
        .cal.gax.md         Calendar event
        <file>.gax.md       Drive file tracking

    \b
    Examples:
        gax push file.sheet.gax.md          # Push a single sheet tab
        gax push *.draft.gax.md             # Push all drafts
        gax push Budget.sheet.gax.md.d/     # Push a checkout folder
        gax push event.cal.gax.md -y        # Push without confirmation
    """
    # Expand globs
    all_paths: list[Path] = []
    for pattern in files:
        if "*" in pattern or "?" in pattern:
            all_paths.extend(Path(p) for p in glob.glob(pattern))
        else:
            all_paths.append(Path(pattern))

    if not all_paths:
        click.echo("No .gax.md files or .gax.md.d folders found.", err=True)
        sys.exit(1)

    success_count = 0
    for path in all_paths:
        if not path.exists():
            click.echo(f"Error: {path} not found", err=True)
            continue

        # Check if it's a folder
        if path.is_dir():
            if not path.name.endswith(".gax.md.d"):
                click.echo(
                    f"Skipping directory: {path} (not a .gax.md.d folder)", err=True
                )
                continue

            click.echo(f"Pushing {path}/...")
        else:
            click.echo(f"Pushing {path}...")

        try:
            r = Resource.from_file(path)
        except ValueError:
            click.echo(f"Error: unsupported file: {path}", err=True)
            continue

        try:
            diff_text = r.diff()
        except NotImplementedError:
            click.echo(f"Error: push not supported for: {path}", err=True)
            continue

        if diff_text is None:
            click.echo("  no changes")
            success_count += 1
            continue

        if not yes:
            click.echo(diff_text)
            if not click.confirm("Push these changes?"):
                click.echo("Cancelled.")
                continue

        try:
            r.push(with_formulas=with_formulas)
            click.echo("  pushed")
            success_count += 1
        except Exception as e:
            click.echo(f"Error: {path}: {e}", err=True)

    if len(all_paths) > 1:
        click.echo(f"Done: {success_count}/{len(all_paths)} pushed")


@docs.section("main")
@main.command()
@click.argument("url")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output file")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "yaml"]),
    default="md",
    help="Output format (for forms)",
)
@handle_errors
def clone(url: str, output: Path | None, fmt: str):
    """Clone a Google resource from URL.

    Supports Google Docs, Sheets, Forms, Gmail, and Calendar.
    """
    from .ui import success

    path = Resource.from_url(url).clone(output=output, fmt=fmt)
    success(f"Created: {path}")


@docs.section("main")
@main.command()
@click.argument("url")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output folder")
@click.option("-f", "--format", "fmt", default="md", help="Output format (for sheets)")
@handle_errors
def checkout(url: str, output: Path | None, fmt: str):
    """Checkout a Google resource from URL into a folder of individual files.

    Supports Google Docs, Sheets, Slides, and Calendar.

    \b
    Examples:
        gax checkout <docs-url>
        gax checkout <sheets-url> -f csv
        gax checkout <calendar-url> -o Week/
    """
    from .ui import success

    path = Resource.from_url(url).checkout(output=output, fmt=fmt)
    success(f"Checked out: {path}")


@main.command()
@click.option("--md", is_flag=True, help="Output as Markdown (for pandoc)")
@click.pass_context
def man(ctx, md: bool):
    """Print the complete manual (auto-generated from commands)."""
    from .docs import _collect_commands, format_man_plain, format_man_md

    root = ctx.find_root().command

    # Collect commands and group by doc_section attribute
    _section_order = {"main": 0, "resource": 1, "utility": 2}
    _section_titles = {"main": "Main", "resource": "Resources", "utility": "Utility"}

    buckets: dict[str, dict[str, tuple[str | None, list]]] = {}
    for cmd_name in root.list_commands(ctx):
        if cmd_name == "man":
            continue
        cmd = root.get_command(ctx, cmd_name)
        if not cmd:
            continue
        commands = _collect_commands(cmd, override_name=cmd_name)
        if not commands:
            continue

        section_key = getattr(cmd, "doc_section", "resource")
        maturity = getattr(cmd, "doc_maturity", None)
        buckets.setdefault(section_key, {})[cmd_name] = (maturity, commands)

    sections: list[tuple[str, dict[str, tuple[str | None, list]]]] = []
    for key in sorted(buckets, key=lambda k: _section_order.get(k, 99)):
        title = _section_titles.get(key, key.title())
        sections.append((title, buckets[key]))

    if md:
        click.echo(format_man_md(sections))
    else:
        click.echo(format_man_plain(sections))


# =============================================================================
# Auth commands
# =============================================================================


@docs.section("utility")
@main.group()
def auth_cmd():
    """Authentication management"""
    pass


# Rename to 'auth' for CLI
main.add_command(auth_cmd, name="auth")


@auth_cmd.command()
@handle_errors
def login():
    """Authenticate with Google (opens browser)."""
    if not auth.credentials_exist():
        click.echo(f"OAuth credentials not found at {auth.CREDENTIALS_FILE}")
        click.echo("")
        click.echo(
            "Please download OAuth client credentials from Google Cloud Console:"
        )
        click.echo("  1. Go to https://console.cloud.google.com/apis/credentials")
        click.echo("  2. Create OAuth 2.0 Client ID (Desktop app)")
        click.echo(f"  3. Download JSON and save to: {auth.CREDENTIALS_FILE}")
        sys.exit(1)

    click.echo("Opening browser for authentication...")
    auth.login()
    click.echo("Authenticated successfully!")
    click.echo(f"Token saved to: {auth.TOKEN_FILE}")


@auth_cmd.command()
def status():
    """Show authentication status."""
    status = auth.get_status()

    click.echo(f"config_dir\t{status['config_dir']}")
    click.echo(f"credentials_path\t{status['credentials_path']}")
    click.echo(f"credentials_exists\t{status['credentials_exists']}")
    click.echo(f"token_path\t{status['token_path']}")
    click.echo(f"token_exists\t{status['token_exists']}")
    click.echo(f"authenticated\t{status['authenticated']}")


@auth_cmd.command()
def logout():
    """Remove stored authentication token."""
    if auth.logout():
        click.echo("Logged out successfully.")
    else:
        click.echo("No token to remove.")


# =============================================================================
# Register resource command groups
# =============================================================================

main.add_command(sheet)
main.add_command(doc)
main.add_command(mail_group, name="mail")
main.add_command(mailbox_group, name="mailbox")
main.add_command(mail_label)  # Flattened from mail.label (ADR 020)
main.add_command(mail_filter)  # Flattened from mail.filter (ADR 020)
main.add_command(cal_group)
main.add_command(task_group)
main.add_command(form)
main.add_command(draft)  # Flattened from mail.draft (ADR 020)
main.add_command(contacts)
main.add_command(file_group, name="file")
main.add_command(slides)


# =============================================================================
# Utility commands
# =============================================================================

REPO = "HeinrichHartmann/gax"
ISSUES_URL = f"https://github.com/{REPO}/issues"


@docs.section("utility")
@main.command()
@click.argument("title", required=False)
@click.option("--body", "-b", help="Issue description")
@click.option(
    "--type",
    "issue_type",
    type=click.Choice(["bug", "feature"]),
    default="bug",
    show_default=True,
    help="Issue type (sets the GitHub label)",
)
def issue(title: str | None, body: str | None, issue_type: str):
    """File a GitHub issue for gax (opens via gh CLI).

    \b
    Examples:
        gax issue
        gax issue "Push swallows newlines"
        gax issue "Attachment support" --type feature
    """
    import shutil
    import subprocess

    if not shutil.which("gh"):
        click.echo("Error: 'gh' (GitHub CLI) is not installed.", err=True)
        click.echo(f"\nPlease file issues at: {ISSUES_URL}/new", err=True)
        click.echo("\nOr install gh: https://cli.github.com/", err=True)
        sys.exit(1)

    cmd = ["gh", "issue", "create", "--repo", REPO, "--label", issue_type]
    if title:
        cmd += ["--title", title]
    if body:
        cmd += ["--body", body]

    sys.exit(subprocess.call(cmd))


def _get_installed_sha() -> str | None:
    """Return the git commit SHA of the currently installed gax uv tool, or None."""
    import glob
    import json

    pattern = (
        f"{Path.home()}/.local/share/uv/tools/gax"
        "/lib/python*/site-packages/gax-*.dist-info/direct_url.json"
    )
    matches = glob.glob(pattern)
    if not matches:
        return None
    try:
        data = json.loads(Path(matches[0]).read_text())
        return data.get("vcs_info", {}).get("commit_id")
    except Exception:
        return None


def _fetch_commits_since(sha: str, verbose: bool) -> list[str] | None:
    """Use gh CLI to fetch commits on main since sha. Returns formatted lines, or None."""
    import shutil
    import subprocess

    if not shutil.which("gh"):
        return None

    try:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{REPO}/commits?sha=main&per_page=100",
                "--jq",
                '.[] | .sha + " " + (.commit.message | split("\\n")[0])',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return None

        lines = []
        for line in result.stdout.strip().splitlines():
            commit_sha, _, message = line.partition(" ")
            if commit_sha.startswith(sha[:7]) or sha.startswith(commit_sha[:7]):
                break
            if verbose:
                lines.append(f"  {commit_sha[:7]}  {message}")
            else:
                lines.append(f"  {commit_sha[:7]}  {message}")
        return lines if lines else []
    except Exception:
        return None


@docs.section("utility")
@main.command()
@click.option("-v", "--verbose", is_flag=True, help="Show full commit messages")
@click.option("-q", "--quiet", is_flag=True, help="Skip changelog after upgrade")
def upgrade(verbose: bool, quiet: bool):
    """Upgrade gax to the latest version from GitHub (uv tool install path).

    After upgrading, shows commits merged since your previous install.
    Requires ``gh`` CLI for the changelog (skipped silently if absent).
    Press Ctrl+C during changelog fetch to skip it.
    """
    import shutil
    import subprocess
    from .ui import operation

    if not shutil.which("uv"):
        click.echo("Error: 'uv' is not installed.", err=True)
        click.echo(
            "Install it: https://docs.astral.sh/uv/getting-started/installation/",
            err=True,
        )
        sys.exit(1)

    old_sha = _get_installed_sha()

    git_url = f"git+https://github.com/{REPO}.git"
    cmd = ["uv", "tool", "install", "--reinstall", git_url]
    click.echo(f"Running: {' '.join(cmd)}")
    rc = subprocess.call(cmd)
    if rc != 0:
        sys.exit(rc)

    if quiet or not shutil.which("gh"):
        return

    if not old_sha:
        click.echo("\nCould not determine previous version; skipping changelog.")
        return

    click.echo("\nFetching changelog... (Ctrl+C to skip)")
    try:
        with operation("Fetching commits from GitHub"):
            commits = _fetch_commits_since(old_sha, verbose)
    except KeyboardInterrupt:
        click.echo("\nChangelog skipped.")
        return

    if commits is None:
        click.echo("(gh CLI unavailable or request failed — skipping changelog)")
    elif not commits:
        click.echo("Already up to date.")
    else:
        click.echo(f"\nChanges since last upgrade ({old_sha[:7]}):")
        for line in commits:
            click.echo(line)


if __name__ == "__main__":
    main()
