"""CLI interface for gax"""

import glob
import re
import sys
import click
from pathlib import Path

from .gsheet import pull as gsheet_pull, push as gsheet_push, clone_all, pull_all
from .gsheet.client import GSheetClient
from .multipart import format_multipart, parse_multipart
from .frontmatter import SheetConfig, format_content, parse_content
from .formats import get_format
from . import auth
from .gdoc import doc
from .mail import mail
from .gcal import cal_cli


@click.group()
@click.version_option()
def main():
    """gax - Google Access CLI"""
    pass


def _detect_file_type(file_path: Path) -> str | None:
    """Detect .gax file type from YAML header or extension.

    Supports:
    - Multipart format (---/---/---) with type in first section header
    - Simple YAML with type field (e.g., .gax.yaml files)

    Returns type string (e.g., 'gax/doc') or None if unknown.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    # Try to parse as multipart to get type from header
    if content.startswith("---"):
        sections = parse_multipart(content)
        if sections:
            file_type = sections[0].headers.get("type")
            if file_type:
                return file_type

            # Infer from header fields
            headers = sections[0].headers
            if "thread_id" in headers:
                return "gax/mail"
            if "draft_id" in headers:
                return "gax/draft"
            if "spreadsheet_id" in headers or "tab" in headers:
                return "gax/sheet"
            if "document_id" in headers or "source" in headers:
                # Check source URL pattern
                source = headers.get("source", "")
                if "docs.google.com/document" in source:
                    return "gax/doc"
                if "docs.google.com/spreadsheets" in source:
                    return "gax/sheet"

        # Try frontmatter-style for single-tab sheets
        try:
            config, _ = parse_content(content)
            if config.spreadsheet_id:
                return "gax/sheet-tab"
        except Exception:
            pass

        # Check for relabel/label/filter files (YAML-only format)
        for line in content.split("\n"):
            if line.startswith("type:"):
                file_type = line.split(":", 1)[1].strip()
                return file_type
            if line.startswith("query:"):
                return "gax/list"
    else:
        # For simple YAML without leading ---, still check for type field
        for line in content.split("\n")[:20]:  # Check first 20 lines
            if line.startswith("type:"):
                file_type = line.split(":", 1)[1].strip()
                return file_type

    # Fallback to extension
    name = file_path.name.lower()
    if name.endswith(".doc.gax") or name.endswith(".tab.gax"):
        return "gax/doc"
    if name.endswith(".sheet.gax"):
        return "gax/sheet"
    if name.endswith(".mail.gax"):
        return "gax/mail"
    if name.endswith(".draft.gax"):
        return "gax/draft"
    if name.endswith(".cal.gax"):
        return "gax/cal"

    return None


def _pull_file(file_path: Path, verbose: bool = False) -> tuple[bool, str]:
    """Pull a single .gax file. Returns (success, message)."""
    file_type = _detect_file_type(file_path)

    if not file_type:
        return False, f"Unknown file type for {file_path}"

    try:
        # Handle labels and filters first (YAML-only, not multipart)
        if file_type == "gax/labels":
            from .label import label_pull_to_file
            count = label_pull_to_file(file_path)
            return True, f"{count} labels"

        if file_type == "gax/filters":
            from .filter import filter_pull_to_file
            count = filter_pull_to_file(file_path)
            return True, f"{count} filters"
        if file_type == "gax/doc":
            from .gdoc import pull_doc, extract_doc_id

            content = file_path.read_text(encoding="utf-8")
            sections = parse_multipart(content)
            if not sections:
                return False, "No sections found"
            source_url = sections[0].headers.get("source", "")
            if not source_url:
                return False, "No source URL found"
            document_id = extract_doc_id(source_url)
            new_sections = pull_doc(document_id, source_url)
            new_content = format_multipart(new_sections)
            file_path.write_text(new_content, encoding="utf-8")
            return True, f"{len(new_sections)} tabs"

        elif file_type == "gax/sheet":
            rows = pull_all(file_path)
            return True, f"{rows} rows"

        elif file_type == "gax/sheet-tab":
            rows = gsheet_pull(file_path)
            return True, f"{rows} rows"

        elif file_type == "gax/mail":
            from .mail import pull_thread, _mail_section_to_multipart

            content = file_path.read_text(encoding="utf-8")
            sections = parse_multipart(content)
            if not sections:
                return False, "No sections found"
            thread_id = sections[0].headers.get("thread_id", "")
            if not thread_id:
                return False, "No thread_id found"
            new_sections = pull_thread(thread_id)
            new_content = format_multipart([_mail_section_to_multipart(s) for s in new_sections])
            file_path.write_text(new_content, encoding="utf-8")
            return True, f"{len(new_sections)} messages"

        elif file_type == "gax/draft":
            from .draft import parse_draft, get_draft, format_draft

            content = file_path.read_text(encoding="utf-8")
            config, _ = parse_draft(content)
            if not config.draft_id:
                return False, "No draft_id in file"
            remote_config, remote_body = get_draft(config.draft_id)
            new_content = format_draft(remote_config, remote_body)
            file_path.write_text(new_content, encoding="utf-8")
            return True, "updated"

        elif file_type == "gax/list":
            from .mail import _parse_gax_header, _relabel_fetch_threads, _write_gax_file
            from .auth import get_authenticated_credentials
            from googleapiclient.discovery import build

            header = _parse_gax_header(file_path)
            if not header["query"]:
                return False, "No query found"
            creds = get_authenticated_credentials()
            service = build("gmail", "v1", credentials=creds)
            labels_result = service.users().labels().list(userId="me").execute()
            label_id_to_name = {lbl["id"]: lbl["name"] for lbl in labels_result.get("labels", [])}
            thread_data = _relabel_fetch_threads(service, header["query"], header["limit"], label_id_to_name)
            _write_gax_file(file_path, header["query"], header["limit"], thread_data)
            return True, f"{len(thread_data)} threads"

        elif file_type == "gax/cal":
            from .gcal import yaml_to_event, api_event_to_dataclass, event_to_yaml
            from .auth import get_authenticated_credentials
            from googleapiclient.discovery import build

            content = file_path.read_text(encoding="utf-8")
            local_event = yaml_to_event(content)
            if not local_event.id:
                return False, "No event ID found"
            creds = get_authenticated_credentials()
            service = build("calendar", "v3", credentials=creds)
            api_event = service.events().get(calendarId=local_event.calendar, eventId=local_event.id).execute()
            updated_event = api_event_to_dataclass(api_event, local_event.calendar, "")
            new_content = event_to_yaml(updated_event)
            file_path.write_text(new_content, encoding="utf-8")
            return True, "updated"

        else:
            return False, f"Unsupported type: {file_type}"

    except Exception as e:
        return False, str(e)


@main.command("pull")
@click.argument("files", nargs=-1, required=True)
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
def unified_pull(files: tuple[str, ...], verbose: bool):
    """Pull/update .gax file(s) from their sources.

    Automatically detects file type from YAML header and calls
    the appropriate pull command.

    \b
    Examples:
        gax pull file.doc.gax           # Pull a single doc
        gax pull *.gax                   # Pull all .gax files
        gax pull inbox.gax notes.doc.gax # Pull multiple files
    """
    # Expand globs and '.'
    all_files: list[Path] = []
    for pattern in files:
        if pattern == ".":
            # Current directory - find all .gax files
            all_files.extend(Path(".").glob("*.gax"))
        elif "*" in pattern or "?" in pattern:
            # Glob pattern
            all_files.extend(Path(p) for p in glob.glob(pattern))
        else:
            all_files.append(Path(pattern))

    if not all_files:
        click.echo("No .gax files found.", err=True)
        sys.exit(1)

    success_count = 0
    for file_path in all_files:
        if not file_path.exists():
            click.echo(f"Error: {file_path} not found", err=True)
            continue

        file_type = _detect_file_type(file_path)
        type_str = f"({file_type})" if file_type else "(unknown)"

        if verbose:
            click.echo(f"Pulling {file_path} {type_str}...", nl=False)

        success, message = _pull_file(file_path, verbose)

        if verbose:
            if success:
                click.echo(f" {message}")
            else:
                click.echo(f" ERROR: {message}")
        else:
            if success:
                click.echo(f"Pulling {file_path} {type_str}... {message}")
            else:
                click.echo(f"Error: {file_path}: {message}", err=True)

        if success:
            success_count += 1

    if len(all_files) > 1:
        click.echo(f"Done: {success_count}/{len(all_files)} files updated")


def _collect_commands(cmd: click.Command, prefix: str = "") -> list[tuple[str, str, list]]:
    """Collect all commands as (full_name, help, options) tuples."""
    results = []
    name = f"{prefix} {cmd.name}".strip() if prefix else cmd.name

    if isinstance(cmd, click.Group):
        for subcmd_name in sorted(cmd.list_commands(None)):
            subcmd = cmd.get_command(None, subcmd_name)
            if subcmd:
                results.extend(_collect_commands(subcmd, name))
    else:
        # Get first line of help only
        help_text = (cmd.help or "").split("\n")[0]
        options = []
        for param in cmd.params:
            if isinstance(param, click.Option) and param.help:
                opts = ", ".join(param.opts)
                options.append((opts, param.help))
        results.append((name, help_text, options))

    return results


@main.command()
@click.pass_context
def man(ctx):
    """Print the complete manual (auto-generated from commands)."""
    root = ctx.find_root().command

    lines = ["GAX(1)", "", "NAME", "    gax - Google Access CLI", ""]

    # Group commands by top-level
    groups: dict[str, list] = {}
    for cmd_name in sorted(root.list_commands(ctx)):
        if cmd_name == "man":
            continue
        cmd = root.get_command(ctx, cmd_name)
        if cmd:
            commands = _collect_commands(cmd)
            if commands:
                groups[cmd_name] = commands

    lines.append("COMMANDS")

    for group_name, commands in groups.items():
        lines.append(f"\n  {group_name}:")
        for full_name, help_text, options in commands:
            lines.append(f"    gax {full_name}")
            if help_text:
                lines.append(f"        {help_text}")
            for opt, opt_help in options:
                lines.append(f"        {opt}: {opt_help}")

    lines.extend([
        "",
        "FILES",
        "    .sheet.gax    Spreadsheet data (single or multipart)",
        "    .doc.gax      Document (all tabs, multipart)",
        "    .tab.gax      Single document tab",
        "    .mail.gax     Email thread",
        "    .draft.gax    Email draft",
        "    .cal.gax      Calendar event",
        "",
        "    ~/.config/gax/credentials.json    OAuth credentials",
        "    ~/.config/gax/token.json          Access token",
        "",
        "SEE ALSO",
        "    gax <command> --help",
    ])

    click.echo("\n".join(lines))


# --- Auth commands ---


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
    help="Output file (default: <title>.sheet.gax)",
)
@click.option(
    "--format", "fmt", default="md", help="Output format: md, csv, tsv, psv, json, jsonl"
)
def sheet_clone(url: str, output: Path | None, fmt: str):
    """Clone all tabs from a spreadsheet to a multipart .sheet.gax file."""
    try:
        spreadsheet_id = _extract_spreadsheet_id(url)
        click.echo(f"Fetching spreadsheet: {spreadsheet_id}")

        title, sections = clone_all(spreadsheet_id, url, fmt)

        if output:
            file_path = output
        else:
            safe_name = re.sub(r'[<>:"/\\|?*]', "-", title)
            safe_name = re.sub(r"\s+", "_", safe_name)
            file_path = Path(f"{safe_name}.sheet.gax")

        if file_path.exists():
            click.echo(f"Error: File already exists: {file_path}", err=True)
            sys.exit(1)

        content = format_multipart(sections)
        file_path.write_text(content, encoding="utf-8")

        total_rows = sum(len(s.content.strip().split("\n")) - 1 for s in sections)
        click.echo(f"Created: {file_path}")
        click.echo(f"Tabs: {len(sections)}, Total rows: {total_rows}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@sheet.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def sheet_pull(file: Path):
    """Pull latest data for all tabs in a multipart file."""
    try:
        rows = pull_all(file)
        click.echo(f"Pulled {rows} rows to {file}")
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
    help="Output file (default: <tab>.sheet.gax)",
)
@click.option(
    "--format", "fmt", default="md", help="Output format: md, csv, tsv, psv, json, jsonl"
)
def tab_clone(url: str, tab_name: str, output: Path | None, fmt: str):
    """Clone a single tab to a .sheet.gax file."""
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
            file_path = Path(f"{safe_name}.sheet.gax")

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
def tab_push(file: Path, with_formulas: bool):
    """Push local data to a single tab."""
    try:
        rows = gsheet_push(file, with_formulas=with_formulas)
        click.echo(f"Pushed {rows} rows from {file}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# Register doc, mail, and cal command groups
main.add_command(doc)
main.add_command(mail)
main.add_command(cal_cli, name="cal")


if __name__ == "__main__":
    main()
