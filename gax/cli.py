"""CLI interface for gax.

Policy: All Click command definitions and CLI UX logic live here.

Resource modules (draft.py, gcal.py, etc.) contain pure business logic
and must not import Click or call sys.exit(). They communicate via:

  - logging.info() / logging.debug()  — status messages (shown in spinner)
  - ValueError                        — user-fixable errors
  - Return values                     — results for cli.py to format

Confirmation prompts (--yes, diff display) are handled here in cli.py
using Resource.diff() to preview changes before calling push/pull.

Output conventions for resource methods:
  - No output (most ops): return None, cli.py prints success()
  - Structured result (path, ID): return it, cli.py formats
  - Tabular/streaming (list, diff): accept a file descriptor, write to it

Imports: cli.py imports only the resource *class* (e.g. Draft, not
parse_draft or create_draft). All interaction goes through class methods.
Non-standard CLI commands can add methods to the class as needed.
Module-to-module imports (e.g. mail.py using draft internals) are fine.
"""

import glob
import re
import sys
import click
from datetime import datetime, timezone
from pathlib import Path

from .gsheet import pull as gsheet_pull, push as gsheet_push, pull_all
from .gsheet.client import GSheetClient
from .multipart import Section, format_section
from .gsheet.frontmatter import SheetConfig, format_content
from .formats import get_format
from . import auth
from . import docs
from .gdoc import doc
from .mail import Thread, Mailbox
from .label import Label
from .filter import Filter
from .gcal import Cal
from .form import Form
from .draft import Draft
from .contacts import Contacts
from .gdrive import File
from .cli_helper import (
    _detect_file_type,
    _pull_folder,
    _push_folder,
    _push_file,
    _pull_file,
)


@click.group()
@click.version_option()
def main():
    """gax - Google Access CLI"""
    from . import ui

    ui.setup_logging()


@docs.section("main")
@main.command("pull")
@click.argument("files", nargs=-1, required=True)
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
def unified_pull(files: tuple[str, ...], verbose: bool, yes: bool):
    """Pull/update .gax.md file(s) or .gax.md.d folder(s) from their sources.

    Automatically detects file type from YAML header and calls
    the appropriate pull command. For .gax.md.d folders, performs
    a checkout to a scratch directory, shows diff, and prompts
    for confirmation.

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
                ok, message = _pull_folder(path, verbose, yes=yes)
                results.append((path, ok, message))
            else:
                # Check if this is a file with a .gax.md tracking file (Drive file)
                if not path.name.endswith(".gax.md"):
                    tracking_path = path.with_suffix(path.suffix + ".gax.md")
                    if tracking_path.exists():
                        try:
                            logger.info(f"Pulling Drive file {path}")
                            File().pull(path)
                            results.append((path, True, "updated"))
                            op.advance()
                            continue
                        except Exception as e:
                            results.append((path, False, str(e)))
                            op.advance()
                            continue

                # Pull regular .gax.md file
                file_type = _detect_file_type(path)
                type_str = f"({file_type})" if file_type else "(unknown)"
                logger.info(f"Pulling {path} {type_str}")

                ok, message = _pull_file(path, verbose)
                results.append((path, ok, message))

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

            # Push folder
            result, message = _push_folder(path, yes=yes, with_formulas=with_formulas)

            if result:
                if message != "cancelled":
                    click.echo(f"Pushed {path}: {message}")
                success_count += 1
            else:
                if message != "cancelled":
                    click.echo(f"Error: {path}: {message}", err=True)
        else:
            # Check if this is a non-.gax.md file with a .gax.md tracking file (Drive file)
            if not path.name.endswith(".gax.md"):
                tracking_path = path.with_suffix(path.suffix + ".gax.md")
                if tracking_path.exists():
                    try:
                        if not yes:
                            from .gdrive import read_tracking_file

                            tracking_data = read_tracking_file(tracking_path)
                            click.echo(
                                f"Update Drive file: {tracking_data.get('name')}"
                            )
                            click.echo(f"From local file: {path}")
                            if not click.confirm("Proceed?"):
                                click.echo("Cancelled.")
                                continue

                        File().push(path)

                        click.echo(f"Pushed {path} to Drive")
                        success_count += 1
                        continue
                    except Exception as e:
                        click.echo(f"Error pushing Drive file {path}: {e}", err=True)
                        continue

            # Push regular .gax.md file
            file_type = _detect_file_type(path)
            type_str = f"({file_type})" if file_type else "(unknown)"

            click.echo(f"Pushing {path} {type_str}...")

            result, message = _push_file(path, yes=yes, with_formulas=with_formulas)

            if result:
                if message != "cancelled":
                    click.echo(f"  {message}")
                success_count += 1
            else:
                if message != "cancelled":
                    click.echo(f"Error: {path}: {message}", err=True)

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
@click.pass_context
def clone(ctx, url: str, output: Path | None, fmt: str):
    """Clone a Google resource from URL.

    Supports Google Docs, Sheets, Forms, Gmail, and Calendar events.
    """
    # Google Docs
    if re.search(r"docs\.google\.com/document/d/", url):
        ctx.invoke(doc.commands["clone"], url=url, output=output)

    # Google Sheets
    elif re.search(r"docs\.google\.com/spreadsheets/d/", url):
        ctx.invoke(sheet_clone, url=url, output=output)

    # Google Forms
    elif re.search(r"docs\.google\.com/forms/d/", url):
        ctx.invoke(form_clone, url=url, output=output, fmt=fmt)

    # Gmail drafts (must come before general mail pattern)
    elif re.search(r"mail\.google\.com/mail/[^#]*#drafts/", url):
        ctx.invoke(draft_clone, draft_id_or_url=url, output=output)

    # Gmail threads
    elif re.search(r"mail\.google\.com/mail/", url):
        ctx.invoke(mail_clone, thread_id_or_url=url, output=output)

    # Calendar events
    elif re.search(r"calendar\.google\.com/calendar/", url):
        ctx.invoke(
            cal_event_group.commands["clone"],
            id_or_url=url,
            output_path=output,
        )

    else:
        click.echo(f"Unrecognized URL: {url}", err=True)
        click.echo("Supported: Google Docs/Sheets/Forms, Gmail, Calendar", err=True)
        sys.exit(1)


@docs.section("main")
@main.command()
@click.argument("url")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="Output folder")
@click.option("-f", "--format", "fmt", default="md", help="Output format (for sheets)")
@click.pass_context
def checkout(ctx, url: str, output: Path | None, fmt: str):
    """Checkout a Google resource from URL into a folder of individual files.

    Supports Google Docs, Sheets, and Calendar.

    \b
    Examples:
        gax checkout <docs-url>
        gax checkout <sheets-url> -f csv
        gax checkout <calendar-url> -o Week/
    """
    # Google Docs
    if re.search(r"docs\.google\.com/document/d/", url):
        kwargs = {"url": url}
        if output:
            kwargs["output"] = output
        ctx.invoke(doc.commands["checkout"], **kwargs)

    # Google Sheets
    elif re.search(r"docs\.google\.com/spreadsheets/d/", url):
        kwargs = {"url": url, "fmt": fmt}
        if output:
            kwargs["output"] = output
        ctx.invoke(sheet_checkout, **kwargs)

    # Calendar
    elif re.search(r"calendar\.google\.com/calendar/", url):
        kwargs = {}
        if output:
            kwargs["output"] = output
        ctx.invoke(cal_checkout_cmd, **kwargs)

    else:
        click.echo(f"Unrecognized URL: {url}", err=True)
        click.echo("Supported: Google Docs, Sheets, Calendar", err=True)
        sys.exit(1)


def _collect_commands(
    cmd: click.Command, prefix: str = "", override_name: str | None = None
) -> list[tuple[str, str, list, list]]:
    """Collect all commands as (full_name, help, arguments, options) tuples."""
    results = []
    # Use override_name if provided (for renamed commands), otherwise use cmd.name
    cmd_name = override_name if override_name else cmd.name
    name = f"{prefix} {cmd_name}".strip() if prefix else cmd_name

    if isinstance(cmd, click.Group):
        for subcmd_name in sorted(cmd.list_commands(None)):
            subcmd = cmd.get_command(None, subcmd_name)
            if subcmd:
                # Pass subcmd_name as override to preserve registered names
                results.extend(_collect_commands(subcmd, name, subcmd_name))
    else:
        # Get first line of help only
        help_text = (cmd.help or "").split("\n")[0]
        arguments = []
        options = []
        for param in cmd.params:
            if isinstance(param, click.Argument):
                arg_name = param.name.upper()
                # Show default if present
                if param.default is not None:
                    arguments.append(f"[{arg_name}]")
                elif param.required:
                    arguments.append(arg_name)
                else:
                    arguments.append(f"[{arg_name}]")
            elif isinstance(param, click.Option) and param.help:
                opts = ", ".join(param.opts)
                options.append((opts, param.help))
        results.append((name, help_text, arguments, options))

    return results


@main.command()
@click.option("--md", is_flag=True, help="Output as Markdown (for pandoc)")
@click.pass_context
def man(ctx, md: bool):
    """Print the complete manual (auto-generated from commands)."""
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
        click.echo(_format_man_md(sections))
    else:
        click.echo(_format_man_plain(sections))


def _format_man_plain(
    sections: list[tuple[str, dict[str, tuple[str | None, list]]]],
) -> str:
    """Format manual as plain text."""
    lines = ["GAX(1)", "", "NAME", "    gax - Google Access CLI", ""]
    lines.append("COMMANDS")

    for section_title, groups in sections:
        lines.append(f"\n  {section_title}:")
        for group_name, (maturity, commands) in groups.items():
            label = f"{group_name} [{maturity}]" if maturity else group_name
            lines.append(f"\n    {label}:")
            for full_name, help_text, arguments, options in commands:
                args_str = " ".join(arguments)
                if args_str:
                    lines.append(f"      gax {full_name} {args_str}")
                else:
                    lines.append(f"      gax {full_name}")
                if help_text:
                    lines.append(f"          {help_text}")
                for opt, opt_help in options:
                    lines.append(f"          {opt}: {opt_help}")

    lines.extend(_file_section_plain())
    return "\n".join(lines)


def _format_man_md(
    sections: list[tuple[str, dict[str, tuple[str | None, list]]]],
) -> str:
    """Format manual as Markdown (suitable for pandoc conversion to man page)."""
    lines = [
        "---",
        "title: GAX",
        "section: 1",
        "header: User Manual",
        "footer: gax",
        "---",
        "",
        "# NAME",
        "",
        "gax - Google Access CLI",
        "",
        "# SYNOPSIS",
        "",
        "**gax** *command* [*options*] [*args*]",
        "",
        "# DESCRIPTION",
        "",
        "Sync Google Workspace (Sheets, Docs, Gmail, Calendar) to local files "
        "that are human-readable, machine-readable, and git-friendly.",
        "",
        "# COMMANDS",
    ]

    for section_title, groups in sections:
        lines.append("")
        lines.append(f"## {section_title}")

        for group_name, (maturity, commands) in groups.items():
            lines.append("")
            label = f"{group_name} [{maturity}]" if maturity else group_name
            lines.append(f"### {label}")
            lines.append("")
            for full_name, help_text, arguments, options in commands:
                args_str = " ".join(arguments)
                cmd = f"**gax {full_name}**"
                if args_str:
                    cmd += f" *{args_str}*"
                lines.append(cmd)
                if help_text:
                    lines.append(f":   {help_text}")
                for opt, opt_help in options:
                    lines.append(f"    **{opt}**: {opt_help}")
                lines.append("")

    lines.extend(_file_section_md())
    return "\n".join(lines)


def _file_section_plain() -> list[str]:
    return [
        "",
        "FILES",
        "    .sheet.gax.md         Spreadsheet data",
        "    .doc.gax.md           Document",
        "    .tab.gax.md           Single document tab",
        "    .mail.gax.md          Email thread",
        "    .draft.gax.md         Email draft",
        "    .cal.gax.md           Calendar event",
        "    .form.gax.md          Google Form definition",
        "    .gax.md               Mail list (TSV with YAML header)",
        "    .label.mail.gax.md    Gmail labels state",
        "    .filter.mail.gax.md   Gmail filters state",
        "",
        "    ~/.config/gax/credentials.json    OAuth credentials",
        "    ~/.config/gax/token.json          Access token",
        "",
        "SEE ALSO",
        "    gax <command> --help",
    ]


def _file_section_md() -> list[str]:
    return [
        "# FILES",
        "",
        "| Extension | Description |",
        "|-----------|-------------|",
        "| .sheet.gax.md | Spreadsheet data |",
        "| .doc.gax.md | Document |",
        "| .tab.gax.md | Single document tab |",
        "| .mail.gax.md | Email thread |",
        "| .draft.gax.md | Email draft |",
        "| .cal.gax.md | Calendar event |",
        "| .form.gax.md | Google Form definition |",
        "| .gax.md | Mail list (TSV with YAML header) |",
        "| .label.mail.gax.md | Gmail labels state |",
        "| .filter.mail.gax.md | Gmail filters state |",
        "",
        "| Path | Description |",
        "|------|-------------|",
        "| ~/.config/gax/credentials.json | OAuth credentials |",
        "| ~/.config/gax/token.json | Access token |",
        "",
        "# SEE ALSO",
        "",
        "**gax** *command* **--help**",
    ]


# --- Auth commands ---


@docs.section("utility")
@main.group()
def auth_cmd():
    """Authentication management"""
    pass


# Rename to 'auth' for CLI
main.add_command(auth_cmd, name="auth")


@auth_cmd.command()
def login():
    """Authenticate with Google (opens browser)."""
    try:
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
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


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


# --- GSheet commands ---


def _extract_spreadsheet_id(url: str) -> str:
    """Extract spreadsheet ID from Google Sheets URL."""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    # Maybe it's already an ID
    if re.fullmatch(r"[a-zA-Z0-9-_]+", url):
        return url
    raise ValueError(f"Could not parse spreadsheet ID from: {url}")


@docs.section("resource")
@main.group()
def sheet():
    """Google Sheets operations"""
    pass


@sheet.command("clone")
@click.argument("url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <title>.sheet.gax.md)",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    default="md",
    help="Output format: md, csv, tsv, psv, json, jsonl",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Suppress multi-tab status message",
)
def sheet_clone(url: str, output: Path | None, fmt: str, quiet: bool):
    """Clone first tab from a spreadsheet to a .sheet.gax.md file.

    For all tabs, use 'gax sheet checkout'.
    """
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        click.echo(f"Fetching spreadsheet: {spreadsheet_id}")

        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)
        title = info["title"]
        all_tabs = info["tabs"]
        first_tab = all_tabs[0]

        # Fetch only the first tab
        formatter = get_format(fmt)
        df = client.read(spreadsheet_id, first_tab["title"])
        data = formatter.write(df)

        from .formats import get_content_type

        section = Section(
            headers={
                "type": "gax/sheet",
                "title": title,
                "source": url,
                "tab": first_tab["title"],
                "content-type": get_content_type(fmt),
            },
            content=data,
        )

        if output:
            file_path = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.sheet.gax.md")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        content = format_section(section.headers, section.content)
        file_path.write_text(content, encoding="utf-8")

        rows = len(data.strip().split("\n")) - 1
        click.echo(f"Created: {file_path}")
        click.echo(f"Rows: {rows}")

        if not quiet and len(all_tabs) > 1:
            click.echo(
                f'  Tab "{first_tab["title"]}" cloned (1 of {len(all_tabs)} tabs).\n'
                f"  For all tabs: gax sheet checkout {url}"
            )

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def sheet_pull(file: Path):
    """Pull latest data for all tabs in a multipart file or checkout folder."""
    from .ui import success as ui_success

    try:
        if file.is_dir():
            ok, message = _pull_folder(file)
            if ok:
                ui_success(message)
            else:
                click.echo(f"Error: {message}", err=True)
                sys.exit(1)
        else:
            rows = pull_all(file)
            ui_success(f"Pulled {rows} rows to {file}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("checkout")
@click.argument("url")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output folder (default: <title>.sheet.gax.md.d)",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    default="md",
    help="Output format: md, csv, tsv, psv, json, jsonl",
)
def sheet_checkout(url: str, output: Path | None, fmt: str):
    """Checkout all tabs to individual files in a folder.

    Creates a folder with individual .tab.sheet.gax.md files for each tab.
    Incremental: skips existing files.

    \b
    Examples:
        gax sheet checkout <url>
        gax sheet checkout <url> -o MyBudget/
        gax sheet checkout <url> -f csv
    """
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        title = info["title"]
        tabs = info["tabs"]

        # Determine output folder
        if output:
            folder = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            folder = Path(f"{safe_name}.sheet.gax.md.d")

        # Create folder
        folder.mkdir(parents=True, exist_ok=True)

        # Write .gax.yaml metadata file
        import yaml

        metadata = {
            "type": "gax/sheet-checkout",
            "spreadsheet_id": spreadsheet_id,
            "url": url,
            "title": title,
            "format": fmt,
            "checked_out": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        metadata_path = folder / ".gax.yaml"
        with open(metadata_path, "w") as f:
            yaml.dump(
                metadata,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        import logging
        from .ui import operation, success as ui_success

        _logger = logging.getLogger(__name__)

        click.echo(f"Checking out {len(tabs)} tabs to {folder}/")

        created = 0
        skipped = 0

        with operation("Checking out tabs", total=len(tabs)) as op:
            for tab_info in tabs:
                tab_name = tab_info["title"]

                # Generate filename
                safe_tab_name = re.sub(r'[<>:"/\\|?*]', "-", tab_name)
                safe_tab_name = re.sub(r"\s+", "_", safe_tab_name)
                file_path = folder / f"{safe_tab_name}.tab.sheet.gax.md"

                # Skip if exists
                if file_path.exists():
                    skipped += 1
                    op.advance()
                    continue

                try:
                    _logger.info(f"Fetching tab: {tab_name}")
                    # Read tab data
                    df = client.read(spreadsheet_id, tab_name)

                    # Format data
                    formatter = get_format(fmt)
                    data = formatter.write(df)

                    # Create config
                    config = SheetConfig(
                        spreadsheet_id=spreadsheet_id,
                        tab=tab_name,
                        format=fmt,
                        url=url,
                    )

                    # Write file
                    content = format_content(config, data)
                    file_path.write_text(content, encoding="utf-8")

                    created += 1

                except Exception as e:
                    click.echo(f"  Error with tab '{tab_name}': {e}", err=True)

                op.advance()

        ui_success(f"Checked out: {created}, Skipped: {skipped} (already present)")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("push")
@click.argument("folder", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def sheet_push(folder: Path, with_formulas: bool, yes: bool):
    """Push all tabs in a checkout folder to Google Sheets.

    Shows a diff preview of changes and prompts for confirmation before pushing.

    \b
    Examples:
        gax sheet push Budget.sheet.gax.md.d
        gax sheet push Budget.sheet.gax.md.d -y
        gax sheet push Budget.sheet.gax.md.d --with-formulas
    """
    from .gsheet.folder_push import push_folder

    try:
        success, message = push_folder(
            folder, with_formulas=with_formulas, auto_approve=yes
        )
        if success:
            click.echo(message)
        else:
            click.echo(f"Error: {message}", err=True)
            sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("plan")
@click.argument("folder", type=click.Path(exists=True, path_type=Path), required=False)
def sheet_plan(folder):
    """Show what changes would be pushed to Google Sheets.

    Similar to 'terraform plan' - previews changes without applying them.
    If no folder is specified, looks for a .sheet.gax.md.d folder in the current directory.

    \b
    Examples:
        gax sheet plan
        gax sheet plan Budget.sheet.gax.md.d
    """
    from .gsheet.folder_push import create_push_plan

    try:
        # If no folder specified, find .sheet.gax.md.d in current directory
        if folder is None:
            candidates = list(Path.cwd().glob("*.sheet.gax.md.d"))
            if len(candidates) == 0:
                click.echo(
                    "Error: No .sheet.gax.md.d folder found in current directory",
                    err=True,
                )
                sys.exit(1)
            elif len(candidates) > 1:
                click.echo(
                    "Error: Multiple .sheet.gax.md.d folders found. Please specify one:",
                    err=True,
                )
                for c in candidates:
                    click.echo(f"  {c.name}")
                sys.exit(1)
            folder = candidates[0]

        # Create and display plan
        plan = create_push_plan(folder)
        click.echo("\n" + plan.format_summary())

        if plan.has_changes:
            click.echo(
                "\nRun 'gax sheet apply' to push these changes, or 'gax sheet push <folder>' with confirmation."
            )

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("apply")
@click.argument("folder", type=click.Path(exists=True, path_type=Path), required=False)
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def sheet_apply(folder, with_formulas: bool, yes: bool):
    """Apply planned changes by pushing to Google Sheets.

    Similar to 'terraform apply' - shows plan and applies changes with confirmation.
    If no folder is specified, looks for a .sheet.gax.md.d folder in the current directory.

    \b
    Examples:
        gax sheet apply
        gax sheet apply Budget.sheet.gax.md.d
        gax sheet apply Budget.sheet.gax.md.d --with-formulas
    """
    from .gsheet.folder_push import create_push_plan, apply_push_plan

    try:
        # If no folder specified, find .sheet.gax.md.d in current directory
        if folder is None:
            candidates = list(Path.cwd().glob("*.sheet.gax.md.d"))
            if len(candidates) == 0:
                click.echo(
                    "Error: No .sheet.gax.md.d folder found in current directory",
                    err=True,
                )
                sys.exit(1)
            elif len(candidates) > 1:
                click.echo(
                    "Error: Multiple .sheet.gax.md.d folders found. Please specify one:",
                    err=True,
                )
                for c in candidates:
                    click.echo(f"  {c.name}")
                sys.exit(1)
            folder = candidates[0]

        # Create and display plan
        plan = create_push_plan(folder)
        click.echo("\n" + plan.format_summary())

        if not plan.has_changes:
            click.echo("Nothing to apply.")
            return

        # Confirm
        if not yes and not click.confirm("\nApply these changes?"):
            click.echo("Cancelled.")
            return

        # Apply
        total_rows = apply_push_plan(plan, with_formulas=with_formulas)
        click.echo(f"\nPushed {len(plan.changes)} tab(s), {total_rows} rows total")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.group()
def tab():
    """Single tab operations"""
    pass


@tab.command("list")
@click.argument("url")
def tab_list(url: str):
    """List tabs in a spreadsheet (TSV output)."""
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        client = GSheetClient()
        info = client.get_spreadsheet_info(spreadsheet_id)

        click.echo(f"# {info['title']}")
        click.echo("index\tid\ttitle")
        for t in info["tabs"]:
            click.echo(f"{t['index']}\t{t['id']}\t{t['title']}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("clone")
@click.argument("url")
@click.argument("tab_name")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <tab>.sheet.gax.md)",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    default="md",
    help="Output format: md, csv, tsv, psv, json, jsonl",
)
def tab_clone(url: str, tab_name: str, output: Path | None, fmt: str):
    """Clone a single tab to a .sheet.gax.md file."""
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        click.echo(f"Fetching: {tab_name}")

        client = GSheetClient()
        df = client.read(spreadsheet_id, tab_name)

        formatter = get_format(fmt)
        data = formatter.write(df)

        config = SheetConfig(
            spreadsheet_id=spreadsheet_id,
            tab=tab_name,
            format=fmt,
            url=url,
        )

        content = format_content(config, data)

        if output:
            file_path = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", tab_name)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.sheet.gax.md")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        file_path.write_text(content, encoding="utf-8")
        click.echo(f"Created: {file_path}")
        click.echo(f"Rows: {len(df)}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def tab_pull(file: Path):
    """Pull latest data for a single tab."""
    try:
        rows = gsheet_pull(file)
        click.echo(f"Pulled {rows} rows to {file}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@tab.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def tab_push(file: Path, with_formulas: bool, yes: bool):
    """Push local data to a single tab."""
    try:
        # Preview: count rows in local file
        from .gsheet.frontmatter import parse_file
        from .formats import get_format

        config, data = parse_file(file)
        fmt = get_format(config.format)
        df = fmt.read(data)
        row_count = len(df)

        click.echo(f"Push {row_count} rows from {file} to {config.tab}?")
        if not yes and not click.confirm("Proceed?"):
            click.echo("Aborted.")
            return

        rows = gsheet_push(file, with_formulas=with_formulas)
        click.echo(f"Pushed {rows} rows")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


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

    try:
        from .ui import success

        file_path = Draft().new(to=to_addr, subject=subject, output=output)
        success(f"Created: {file_path}")
        click.echo(f"Edit the file, then run: gax draft push {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@draft.command("clone")
@click.argument("draft_id_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: <subject>.draft.gax.md)",
)
def draft_clone(draft_id_or_url, output):
    """Clone an existing draft from Gmail.

    Examples:

        gax draft clone r-1234567890123456789
        gax draft clone "https://mail.google.com/mail/u/0/#drafts/..."
        gax draft clone r-1234567890 -o my_draft.draft.gax.md
    """
    try:
        from .ui import success

        file_path = Draft().clone(url=draft_id_or_url, output=output)
        success(f"Created: {file_path}")
    except (ValueError, Exception) as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@draft.command("list")
@click.option("--limit", default=100, help="Maximum results (default: 100)")
def draft_list(limit):
    """List Gmail drafts (TSV output).

    Output columns: draft_id, thread_id, date, to, subject
    """
    try:
        Draft().list(sys.stdout, limit=limit)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@draft.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
def draft_push(file, yes):
    """Push local draft to Gmail.

    If the draft doesn't exist in Gmail yet, creates it.
    If it exists, shows diff and updates it (with confirmation).

    Examples:

        gax draft push my_draft.draft.gax.md
        gax draft push my_draft.draft.gax.md -y
    """
    try:
        from .ui import success

        d = Draft()
        diff_text = d.diff(file)
        if diff_text is None:
            click.echo("No differences to push.")
            return
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                return
        d.push(file)
        success("Pushed successfully.")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@draft.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def draft_pull(file):
    """Pull latest content from Gmail draft.

    Updates the local .draft.gax.md file with the remote draft content.

    Example:

        gax draft pull my_draft.draft.gax.md
    """
    try:
        from .ui import success

        Draft().pull(file)
        success(f"Updated: {file}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


# =============================================================================
# Contacts commands
# =============================================================================


@docs.section("resource")
@click.group()
def contacts():
    """Google Contacts operations."""
    pass


@contacts.command("clone")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "jsonl"]),
    default="md",
    help="Output format: md (view-only) or jsonl (editable)",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: contacts.<format>)",
)
def contacts_clone(fmt, output):
    """Clone all contacts to a local file.

    \b
    Formats:
      md     Human-readable markdown (default, view-only)
      jsonl  JSON Lines format (editable, scriptable)
    """
    try:
        from .ui import success

        file_path = Contacts().clone(fmt=fmt, output=output)
        success(f"Created: {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@contacts.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def contacts_pull(file):
    """Pull latest contacts from Google.

    Updates the file with current contact data, preserving format.
    """
    try:
        from .ui import success

        Contacts().pull(file)
        success(f"Updated: {file}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@contacts.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def contacts_push(file, yes):
    """Push local JSONL contacts to Google.

    Compares local contacts with remote, shows diff, and applies changes.
    Only works with JSONL format files.
    """
    try:
        from .ui import success

        c = Contacts()
        diff_text = c.diff(file)
        if diff_text is None:
            click.echo("No changes to push.")
            return
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Push these changes?"):
                click.echo("Aborted.")
                return
        c.push(file)
        success("Pushed successfully.")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


# =============================================================================
# File commands (Google Drive)
# =============================================================================


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
def file_clone(url_or_id, output):
    """Clone a file from Google Drive.

    Downloads the file and creates a tracking .gax.md file.

    Examples:

        gax file clone https://drive.google.com/file/d/abc123/view
        gax file clone abc123 -o report.pdf
    """
    try:
        from .ui import success

        file_path = File().clone(url=url_or_id, output=output)
        success(f"Created: {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@file_group.command("pull")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
def file_pull(file_path):
    """Pull latest version of a file from Google Drive.

    Requires a .gax.md tracking file (created by 'gax file clone').

    Example:

        gax file pull report.pdf
    """
    try:
        from .ui import success

        File().pull(file_path)
        success(f"Updated: {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@file_group.command("push")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("--public", is_flag=True, help="Make file publicly accessible")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def file_push(file_path, public, yes):
    """Push local file to Google Drive.

    If file has a .gax.md tracking file, updates existing file.
    Otherwise, uploads as a new file.

    Examples:

        gax file push report.pdf
        gax file push report.pdf --public
        gax file push report.pdf -y
    """
    try:
        from .ui import success

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

        File().push(file_path, public=public)
        success("Pushed successfully.")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


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
def mail_clone(thread_id_or_url, output):
    """Clone a single email thread to a local .mail.gax.md file.

    \b
    Examples:
        gax mail clone 19d0bed1cddbab6d
        gax mail clone "https://mail.google.com/..."
        gax mail clone 19d0bed1cddbab6d -o thread.mail.gax.md
    """
    try:
        from .ui import success

        file_path = Thread().clone(url=thread_id_or_url, output=output)
        success(f"Created: {file_path}")
    except (ValueError, Exception) as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mail_group.command("pull")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def mail_pull(path):
    """Pull latest messages for .mail.gax.md file(s).

    Single file:

        gax mail pull thread.mail.gax.md

    Folder (updates all .mail.gax.md files):

        gax mail pull Inbox/
    """
    try:
        from .ui import success

        Thread().pull(path)
        success(f"Updated: {path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mail_group.command("reply")
@click.argument("file_or_url")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    help="Output file (default: Re_<subject>.draft.gax.md)",
)
def mail_reply(file_or_url, output):
    """Create a reply draft from a thread.

    Examples:

        gax mail reply Project_Update.mail.gax.md
        gax mail reply "https://mail.google.com/mail/u/0/#inbox/abc123"
        gax mail reply thread.mail.gax.md -o my_reply.draft.gax.md
    """
    try:
        from .ui import success

        out_path = Thread().reply(file_or_url, output=output)
        success(f"Created: {out_path}")
        click.echo(f"Edit the file, then run: gax draft push {out_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


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
        try:
            Mailbox().list(sys.stdout, query=query, limit=limit)
        except ValueError as e:
            from .ui import error

            error(str(e))
            sys.exit(1)


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
def mailbox_fetch(output, query, limit):
    """Fetch full threads matching query into a folder."""
    try:
        from .ui import success

        cloned, skipped = Mailbox().fetch(query=query, limit=limit, output=output)
        success(f"Cloned: {cloned}, Skipped: {skipped} (already present)")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


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
def mailbox_clone_cmd(output, query, limit):
    """Clone threads from Gmail for bulk labeling."""
    try:
        from .ui import success

        file_path = Mailbox().clone(query=query, limit=limit, output=Path(output))
        success(f"Cloned to: {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mailbox_group.command("pull")
@click.argument("file", type=click.Path(exists=True))
def mailbox_pull(file):
    """Update a .gax.md file by re-fetching from Gmail."""
    try:
        from .ui import success

        Mailbox().pull(Path(file))
        success(f"Updated: {file}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mailbox_group.command("plan")
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "-o",
    "--output",
    default="mailbox.plan.yaml",
    help="Output file (default: mailbox.plan.yaml)",
)
def mailbox_plan_cmd(file, output):
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
        from .ui import error

        error(str(e))
        sys.exit(1)


@mailbox_group.command("apply")
@click.argument("plan_file", type=click.Path(exists=True))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def mailbox_apply(plan_file, yes):
    """Apply label changes from plan."""
    import yaml

    try:
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

        from .ui import success, error

        succeeded, failed = Mailbox().apply_plan(plan)
        success(f"Applied: {succeeded} threads")
        if failed:
            error(f"Failed: {failed} threads")

    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


# =============================================================================
# Label commands
# =============================================================================


@docs.section("resource")
@click.group("mail-label")
def mail_label():
    """Gmail label management (declarative)."""
    pass


@mail_label.command("list")
def label_list():
    """List Gmail labels (TSV output)."""
    try:
        Label().list(sys.stdout)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@mail_label.command("clone")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: mail-labels.gax.md)",
)
@click.option("--all", "include_all", is_flag=True, help="Include system labels")
def label_clone(output, include_all):
    """Clone Gmail labels to a .gax.md file."""
    try:
        from .ui import success

        file_path = Label().clone(output=output, include_all=include_all)
        success(f"Created: {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mail_label.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--all", "include_all", is_flag=True, help="Include system labels")
def label_pull(file, include_all):
    """Pull latest labels to existing file."""
    try:
        from .ui import success

        Label().pull(file, include_all=include_all)
        success(f"Updated: {file}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mail_label.command("plan")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default="labels.plan.yaml",
    help="Output plan file",
)
@click.option("--delete", "allow_delete", is_flag=True, help="Include deletions")
def label_plan(file, output, allow_delete):
    """Preview label changes (diff)."""
    try:
        diff_text = Label().diff(file, allow_delete=allow_delete)
        if diff_text is None:
            click.echo("No changes to apply.")
            return
        click.echo(diff_text)
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mail_label.command("apply")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@click.option("--delete", "allow_delete", is_flag=True, help="Include deletions")
def label_apply(file, yes, allow_delete):
    """Apply label changes to Gmail."""
    try:
        from .ui import success

        lbl = Label()
        diff_text = lbl.diff(file, allow_delete=allow_delete)
        if diff_text is None:
            click.echo("No changes to apply.")
            return
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Apply these changes?"):
                click.echo("Aborted.")
                return
        lbl.push(file, allow_delete=allow_delete)
        success("Done.")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


# =============================================================================
# Filter commands
# =============================================================================


@docs.section("resource")
@click.group("mail-filter")
def mail_filter():
    """Gmail filter management (declarative).

    Note: Gmail applies ALL matching filters simultaneously, not sequentially.
    Filter order has no significance - there is no "stop processing" feature.
    """
    pass


@mail_filter.command("list")
def filter_list():
    """List Gmail filters (TSV output)."""
    try:
        Filter().list(sys.stdout)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@mail_filter.command("clone")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: mail-filters.gax.md)",
)
def filter_clone(output):
    """Clone Gmail filters to a .gax.md file."""
    try:
        from .ui import success

        file_path = Filter().clone(output=output)
        success(f"Created: {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mail_filter.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def filter_pull(file):
    """Pull latest filters to existing file."""
    try:
        from .ui import success

        Filter().pull(file)
        success(f"Updated: {file}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mail_filter.command("plan")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def filter_plan(file):
    """Preview filter changes (diff)."""
    try:
        diff_text = Filter().diff(file)
        if diff_text is None:
            click.echo("No changes to apply.")
            return
        click.echo(diff_text)
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@mail_filter.command("apply")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def filter_apply(file, yes):
    """Apply filter changes to Gmail."""
    try:
        from .ui import success

        flt = Filter()
        diff_text = flt.diff(file)
        if diff_text is None:
            click.echo("No changes to apply.")
            return
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Apply these changes?"):
                click.echo("Aborted.")
                return
        flt.push(file)
        success("Done.")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


# =============================================================================
# Calendar commands
# =============================================================================


@docs.section("resource")
@click.group(name="cal")
def cal_group():
    """Google Calendar sync commands."""
    pass


@cal_group.command(name="calendars")
def cal_calendars_cmd():
    """List available calendars."""
    try:
        Cal().calendars(sys.stdout)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cal_group.command(name="list")
@click.argument("calendar", required=False)
@click.option(
    "--days", "-d", default=None, type=int, help="Number of days to show (default: 7)"
)
@click.option("--from", "date_from", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "date_to", default=None, help="End date (YYYY-MM-DD)")
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["md", "tsv"]),
    default="md",
    help="Output format (default: md)",
)
@click.option("-v", "--verbose", is_flag=True, help="Include event descriptions")
def cal_list_cmd(
    calendar: str | None,
    days: int | None,
    date_from: str | None,
    date_to: str | None,
    fmt: str,
    verbose: bool,
):
    """List events from a calendar.

    CALENDAR is a calendar name, ID, or numeric index (from 'gax cal calendars').
    Defaults to the primary calendar.

    \b
    Examples:
        gax cal list                  # Primary calendar, next 7 days
        gax cal list -d 14            # Next 14 days
        gax cal list Work             # "Work" calendar
        gax cal list --from 2026-03-01 --to 2026-03-15
        gax cal list -f tsv           # TSV output
    """
    from .gcal import (
        resolve_time_range,
        resolve_calendar_id,
        list_events,
        format_events_tsv,
        format_events_markdown,
    )

    try:
        time_min, time_max = resolve_time_range(days, date_from, date_to)
        calendar_id = resolve_calendar_id(calendar)
        events = list_events(
            time_min=time_min, time_max=time_max, calendar_id=calendar_id
        )

        if fmt == "tsv":
            click.echo(format_events_tsv(events, include_desc=verbose), nl=False)
        else:
            click.echo(format_events_markdown(events, include_desc=verbose), nl=False)
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@cal_group.command(name="clone")
@click.argument("calendar", required=False)
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: calendar.cal.gax.md)",
)
@click.option(
    "--days", "-d", default=None, type=int, help="Number of days (default: 7)"
)
@click.option("--from", "date_from", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "date_to", default=None, help="End date (YYYY-MM-DD)")
@click.option("-v", "--verbose", is_flag=True, help="Include event descriptions")
def cal_clone_cmd(
    calendar: str | None,
    output: Path | None,
    days: int | None,
    date_from: str | None,
    date_to: str | None,
    verbose: bool,
):
    """Clone events to a .cal.gax.md file.

    Creates a file with all events that can be updated with 'gax cal pull'.
    CALENDAR defaults to primary calendar.

    \b
    Examples:
        gax cal clone
        gax cal clone Work -o week.cal.gax.md -d 7
        gax cal clone --from 2026-03-01 --to 2026-03-31 -o march.cal.gax.md
    """
    try:
        from .ui import success

        file_path = Cal().clone(
            output=output,
            calendar=calendar,
            days=days,
            date_from=date_from,
            date_to=date_to,
            verbose=verbose,
        )
        success(f"Created: {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@cal_group.command(name="pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def cal_pull_cmd(file: Path):
    """Pull latest events to existing file.

    \b
    Example:
        gax cal pull week.cal.gax.md
    """
    try:
        from .ui import success

        Cal().pull(file)
        success(f"Updated: {file}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@cal_group.command(name="checkout")
@click.argument("calendar", required=False)
@click.option(
    "-o",
    "--output",
    default="calendar.cal.gax.md.d",
    type=click.Path(path_type=Path),
    help="Output folder (default: calendar.cal.gax.md.d)",
)
@click.option(
    "--days", "-d", default=None, type=int, help="Number of days (default: 7)"
)
@click.option("--from", "date_from", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "date_to", default=None, help="End date (YYYY-MM-DD)")
def cal_checkout_cmd(
    calendar: str | None,
    output: Path,
    days: int | None,
    date_from: str | None,
    date_to: str | None,
):
    """Checkout events as individual .cal.gax.md files into a folder.

    Each event becomes a separate file that can be edited and pushed.
    CALENDAR defaults to primary calendar.

    \b
    Examples:
        gax cal checkout
        gax cal checkout -o Week/
        gax cal checkout Work -o Week/ -d 7
    """
    try:
        from .ui import success

        cloned, skipped = Cal().checkout(
            output=output,
            calendar=calendar,
            days=days,
            date_from=date_from,
            date_to=date_to,
        )
        success(f"Checked out: {cloned}, Skipped: {skipped} (already present)")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@cal_group.group(name="event")
def cal_event_group():
    """Event operations (clone, new, pull, push, delete)."""
    pass


@cal_event_group.command(name="clone")
@click.argument("id_or_url")
@click.option(
    "--cal", "-c", "calendar", default="primary", help="Calendar ID (default: primary)"
)
@click.option(
    "-o",
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    help="Output file path",
)
def cal_event_clone_cmd(id_or_url: str, calendar: str, output_path: Path | None):
    """Clone an event to a local .cal.gax.md file."""
    try:
        from .ui import success

        file_path = Cal().event_clone(id_or_url, calendar=calendar, output=output_path)
        success(f"Cloned event to {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@cal_event_group.command(name="new")
@click.option(
    "--cal", "-c", "calendar", default="primary", help="Calendar ID (default: primary)"
)
@click.option(
    "-o",
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    help="Output file path",
)
def cal_event_new_cmd(calendar: str, output_path: Path | None):
    """Create a new event file (edit and push to create upstream)."""
    try:
        from .ui import success

        file_path = Cal().event_new(calendar=calendar, output=output_path)
        success(f"Created event template at {file_path}")
        click.echo(f"Edit the file, then run: gax cal event push {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@cal_event_group.command(name="pull")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
def cal_event_pull_cmd(file_path: Path):
    """Pull latest event data from API."""
    try:
        from .ui import success

        Cal().event_pull(file_path)
        success(f"Pulled latest data to {file_path}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@cal_event_group.command(name="push")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def cal_event_push_cmd(file_path: Path, yes: bool):
    """Push local changes to API."""
    try:
        from .ui import success

        c = Cal()
        diff_text = c.event_diff(file_path)
        if diff_text is None:
            click.echo("No changes to push.")
            return
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Push these changes?"):
                click.echo("Cancelled.")
                return

        link = c.event_push(file_path)
        success(f"Pushed event: {link}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@cal_event_group.command(name="delete")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def cal_event_delete_cmd(file_path: Path, yes: bool):
    """Delete event from calendar."""
    from .gcal import yaml_to_event

    try:
        from .ui import success

        content = file_path.read_text()
        local_event = yaml_to_event(content)

        if not yes:
            click.echo(f"Delete event '{local_event.title}' from calendar?")
            click.echo("This will also delete the local file.")
            if not click.confirm("Proceed?"):
                click.echo("Cancelled.")
                return

        title = Cal().event_delete(file_path)
        success(f"Deleted event '{title}'")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


# =============================================================================
# Form commands
# =============================================================================


@docs.section("resource")
@docs.maturity("unstable")
@click.group()
def form():
    """Google Forms operations"""
    pass


@form.command("clone")
@click.argument("url")
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    help="Output file (default: <title>.form.gax.md)",
)
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["md", "yaml"]),
    default="md",
    help="Content format: md (readable, default) or yaml (round-trip safe)",
)
def form_clone(url, output, fmt):
    """Clone a Google Form to a local .form.gax.md file.

    By default, creates a human-readable markdown representation.
    Use --format yaml for faithful round-trip representation (required for push).
    """
    try:
        from .ui import success

        file_path = Form().clone(url=url, output=output, format=fmt)
        success(f"Created: {file_path}")
        if fmt == "md":
            click.echo("Note: Use --format yaml for round-trip safe format")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@form.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def form_pull(file):
    """Pull latest form definition from Google Forms."""
    try:
        from .ui import success

        Form().pull(file)
        success(f"Updated: {file}")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@form.command("plan")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "-o",
    "--output",
    type=click.Path(path_type=Path),
    default="form.plan.yaml",
    help="Output plan file",
)
def form_plan(file, output):
    """Preview form changes (diff)."""
    try:
        diff_text = Form().diff(file)
        if diff_text is None:
            click.echo("No changes to apply.")
            return
        click.echo(diff_text)
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


@form.command("apply")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def form_apply(file, yes):
    """Apply form changes to Google Forms."""
    try:
        from .ui import success

        f = Form()
        diff_text = f.diff(file)
        if diff_text is None:
            click.echo("No changes to apply.")
            return
        if not yes:
            click.echo(diff_text)
            if not click.confirm("Apply these changes?"):
                click.echo("Aborted.")
                return
        f.push(file)
        success("Done.")
    except ValueError as e:
        from .ui import error

        error(str(e))
        sys.exit(1)


# Register doc, mail, cal, form, file, and contacts command groups
main.add_command(doc)
main.add_command(mail_group, name="mail")
main.add_command(mailbox_group, name="mailbox")
main.add_command(mail_label)  # Flattened from mail.label (ADR 020)
main.add_command(mail_filter)  # Flattened from mail.filter (ADR 020)
main.add_command(cal_group)
main.add_command(form)
main.add_command(draft)  # Flattened from mail.draft (ADR 020)
main.add_command(contacts)
main.add_command(file_group, name="file")


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
