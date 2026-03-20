"""CLI interface for gax"""

import re
import sys
import click
from pathlib import Path

from .gsheet import pull as gsheet_pull, push as gsheet_push, clone_all, pull_all
from .gsheet.client import GSheetClient
from .multipart import format_multipart
from .frontmatter import SheetConfig, format_content
from .formats import get_format
from . import auth
from .gdoc import doc
from .mail import mail


@click.group()
@click.version_option()
def main():
    """gax - Google Access CLI"""
    pass


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
    "--format", "fmt", default="csv", help="Output format: csv, tsv, psv, json, jsonl"
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
    "--format", "fmt", default="csv", help="Output format: csv, tsv, psv, json, jsonl"
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


# Register doc and mail command groups
main.add_command(doc)
main.add_command(mail)


if __name__ == "__main__":
    main()
