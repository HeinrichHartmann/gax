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
from .ui import gax_command, confirm_and_push  # noqa: F401
from .resource import Resource

# Import resource CLI groups — triggers Resource.__init_subclass__ registration
from .gsheet.cli import sheet
from .contacts.cli import contacts
from .gdrive.cli import drive_group
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


def _split_flags_and_files(
    args: tuple[str, ...],
) -> tuple[dict[str, bool], list[str]]:
    """Separate ``--flag`` items from file paths in a combined argument tuple.

    With ``nargs=-1`` and ``ignore_unknown_options``, Click puts unknown
    flags into the argument tuple alongside file paths.  This splits them
    back out, mapping ``--flag-name`` → ``flag_name=True``.

    Returns (extra_kwargs, file_args).
    """
    kw: dict[str, bool] = {}
    files: list[str] = []
    for arg in args:
        if arg.startswith("--"):
            kw[arg.lstrip("-").replace("-", "_")] = True
        else:
            files.append(arg)
    return kw, files


@docs.section("main")
@main.command("get")
@click.argument("target")
@click.option("--tab", help="Specific tab (for multi-tab resources)")
def unified_get(target: str, tab: str | None):
    """Fetch remote content to stdout. Read-only, no local changes.

    Reads the source URL from the file's metadata, fetches current remote
    content, and prints to stdout. Does not modify local files.

    \b
    Examples:
        gax get report.doc.gax.md           # Print remote doc content
        gax get Budget.sheet.gax.md.d/      # Print all remote tabs
        gax get Budget.sheet.gax.md.d/ --tab Revenue  # Single tab
        gax get tab.sheet.gax.md            # Print remote tab data
    """
    from .ui import error as ui_error

    path = Path(target)
    if not path.exists():
        # Try as URL
        try:
            resource = Resource.from_url(target)
        except ValueError:
            ui_error(f"Not found: {target}")
            sys.exit(1)
    else:
        try:
            resource = Resource.from_file(path)
        except ValueError:
            ui_error(f"Unsupported file: {target}")
            sys.exit(1)

    try:
        kw = {}
        if tab:
            kw["tab"] = tab
        content = resource.get(**kw)
        click.echo(content, nl=False)
    except NotImplementedError:
        ui_error(f"get not supported for: {target}")
        sys.exit(1)
    except Exception as e:
        ui_error(str(e))
        sys.exit(1)


@docs.section("main")
@main.command(
    "pull",
    context_settings=dict(ignore_unknown_options=True),
)
@click.argument("files", nargs=-1, required=True)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation, overwrite local state")
def unified_pull(files: tuple[str, ...], yes: bool):
    """Pull/update .gax.md file(s) or .gax.md.d folder(s) from their sources.

    Shows a diff and asks for confirmation before overwriting local files.
    Use -y to skip confirmation and overwrite directly.
    Extra flags (e.g. --patch, --with-comments) are forwarded to the
    resource-specific pull method.

    \b
    Examples:
        gax pull file.doc.gax.md           # Pull a single doc
        gax pull *.gax.md                   # Pull all .gax.md files
        gax pull inbox.gax.md notes.doc.gax.md # Pull multiple files
        gax pull folder.doc.gax.md.d/       # Pull a checkout folder
        gax pull -y .                        # Force-pull everything
        gax pull --patch folder.doc.gax.md.d/ # Broadcast pull to individual files
    """
    from .ui import confirm_and_pull

    # Separate flags from file paths — nargs=-1 consumes everything,
    # so unknown flags like --patch end up in the files tuple.
    extra_kw, file_args = _split_flags_and_files(files)

    # Expand globs and '.'
    all_paths: list[Path] = []
    for pattern in file_args:
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

    from .ui import success as ui_success, error as ui_error

    results = []  # (path, ok, message)

    for path in all_paths:
        if not path.exists():
            results.append((path, False, "not found"))
            continue

        # Check if it's a folder
        if path.is_dir():
            if not path.name.endswith(".gax.md.d"):
                results.append((path, False, "not a .gax.md.d folder"))
                continue

        try:
            resource = Resource.from_file(path)
            confirm_and_pull(resource, yes=yes, **extra_kw)
            results.append((path, True, "updated"))
        except Exception as e:
            results.append((path, False, str(e)))

    # Print results after spinner is done
    success_count = 0
    fail_count = 0
    for path, ok, message in results:
        if not ok:
            ui_error(f"{path}: {message}")
            fail_count += 1
        else:
            success_count += 1

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
@click.option("--values", is_flag=True, help="Write as literal strings, no formula interpretation (sheets only)")
def unified_push(files: tuple[str, ...], yes: bool, values: bool):
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
            r.push(values=values)
            click.echo("  pushed")
            success_count += 1
        except Exception as e:
            click.echo(f"Error: {path}: {e}", err=True)

    if len(all_paths) > 1:
        click.echo(f"Done: {success_count}/{len(all_paths)} pushed")


@docs.section("main")
@main.command("get")
@click.argument("source")
@gax_command
def get_cmd(source: str):
    """Fetch remote resource and print content to stdout.

    Accepts a URL or a local .gax.md tracking file (reads the source URL
    from its header). Content goes to stdout; progress goes to stderr.
    No files are created or modified.

    \b
    Examples:
        gax get https://docs.google.com/document/d/abc123 | less
        gax get report.doc.gax.md | grep TODO
        diff <(gax get https://docs.google.com/...) local.md
    """
    import tempfile

    def _print_file(file_path: Path, first_ref: list) -> None:
        sections = _parse(file_path.read_text(encoding="utf-8"))
        for section in sections:
            if not first_ref[0]:
                sys.stdout.write("\n---\n\n")
            sys.stdout.write(section.content)
            if section.content and not section.content.endswith("\n"):
                sys.stdout.write("\n")
            first_ref[0] = False

    def _print_folder(folder: Path, first_ref: list) -> None:
        tab_files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix == ".md")
        for tab_file in tab_files:
            _print_file(tab_file, first_ref)

    from .gaxfile import parse_multipart as _parse

    path = Path(source)
    if path.exists():
        # Read source URL from file header, fetch remote
        sections = _parse(path.read_text(encoding="utf-8"))
        src_url = sections[0].headers.get("source", "") if sections else ""
        if not src_url:
            click.echo(f"Error: no source URL in {path}", err=True)
            sys.exit(1)
        resource = Resource.from_url(src_url)
    else:
        resource = Resource.from_url(source)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = resource.clone(output=Path(tmpdir) / "_get_tmp")
        first_ref = [True]
        if tmp_path.is_dir():
            _print_folder(tmp_path, first_ref)
        else:
            _print_file(tmp_path, first_ref)


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
@gax_command
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
@gax_command
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
@click.option(
    "--scopes",
    default=None,
    help="Comma-separated scopes to request (e.g. gmail.readonly,calendar). "
    "Omit for all scopes. Use 'gax auth scopes' to list available scopes.",
)
@gax_command
def login(scopes):
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

    scope_list = [s.strip() for s in scopes.split(",")] if scopes else None

    if scope_list:
        click.echo(f"Requesting scopes: {', '.join(scope_list)}")
    else:
        click.echo("Requesting all scopes.")

    click.echo("Opening browser for authentication...")
    auth.login(scopes=scope_list)
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
def scopes():
    """List available OAuth scopes grouped by resource."""
    from .resource import Resource

    seen: set[str] = set()
    for sub in Resource._subclasses:
        if not sub.SCOPES:
            continue
        scope_str = ", ".join(sub.SCOPES)
        key = f"{sub.name}:{scope_str}"
        if key in seen:
            continue
        seen.add(key)
        click.echo(f"{sub.name:15s} {scope_str}")


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
main.add_command(drive_group, name="drive")
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


@docs.section("utility")
@main.command()
@click.option("-v", "--verbose", is_flag=True, help="Show full commit descriptions")
@click.option("-n", "--count", default=20, help="Number of commits (default: 20)")
def changelog(verbose: bool, count: int):
    """Show recent commits on main (requires gh CLI)."""
    import json
    import shutil
    import subprocess

    if not shutil.which("gh"):
        click.echo("Error: 'gh' (GitHub CLI) is required.", err=True)
        sys.exit(1)

    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{REPO}/commits?sha=main&per_page={count}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            click.echo("Failed to fetch commits from GitHub.", err=True)
            sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    commits = json.loads(result.stdout)

    # Group commits by date
    from collections import OrderedDict

    by_date: OrderedDict[str, list] = OrderedDict()
    for commit in commits:
        date_str = commit["commit"]["author"]["date"][:10]  # YYYY-MM-DD
        by_date.setdefault(date_str, []).append(commit)

    for date, day_commits in by_date.items():
        click.echo(f"\n{date}")
        for commit in day_commits:
            message = commit["commit"]["message"]
            title = message.split("\n")[0]
            click.echo(f"  {title}")
            if verbose:
                body = "\n".join(message.split("\n")[1:]).strip()
                # Skip co-authored-by lines and trailing separators
                body_lines = [
                    line
                    for line in body.splitlines()
                    if not line.startswith("Co-authored-by:")
                    and not line.startswith("Co-Authored-By:")
                    and line.strip() != "---------"
                ]
                # Collapse runs of blank lines into single blanks
                cleaned = []
                for line in body_lines:
                    if not line.strip() and cleaned and not cleaned[-1].strip():
                        continue
                    cleaned.append(line)
                body = "\n".join(cleaned).strip()
                if body:
                    click.echo()
                    for line in body.splitlines():
                        click.echo(f"    {line}")
                    click.echo()


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
