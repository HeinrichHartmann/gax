"""CLI interface for gax"""

import re
import sys
import click
from pathlib import Path

from .gsheet import pull as gsheet_pull, push as gsheet_push
from .gsheet.client import GSheetClient
from .frontmatter import SheetConfig, format_content
from .formats import get_format
from . import auth


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
            click.echo("Please download OAuth client credentials from Google Cloud Console:")
            click.echo("  1. Go to https://console.cloud.google.com/apis/credentials")
            click.echo("  2. Create OAuth 2.0 Client ID (Desktop app)")
            click.echo(f"  3. Download JSON and save to: {auth.CREDENTIALS_FILE}")
            sys.exit(1)

        click.echo("Opening browser for authentication...")
        creds = auth.login()
        click.echo(f"Authenticated successfully!")
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

@main.group()
def gsheet():
    """Google Sheets operations"""
    pass


@gsheet.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def pull(file: Path):
    """Pull data from Google Sheets to local file."""
    try:
        rows = gsheet_pull(file)
        click.echo(f"Pulled {rows} rows to {file}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@gsheet.command()
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))")
def push(file: Path, with_formulas: bool):
    """Push data from local file to Google Sheets."""
    try:
        rows = gsheet_push(file, with_formulas=with_formulas)
        click.echo(f"Pushed {rows} rows from {file}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@gsheet.command()
@click.argument("url")
@click.argument("tab")
@click.option("--format", "fmt", default="csv", help="Output format: csv, tsv, psv, json, jsonl")
def init(url: str, tab: str, fmt: str):
    """Initialize a .sheet.gax file from a Google Sheets URL."""
    try:
        # Parse spreadsheet ID from URL
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
        if not match:
            raise ValueError(f"Could not parse spreadsheet ID from URL: {url}")
        spreadsheet_id = match.group(1)

        # Fetch data
        client = GSheetClient()
        df = client.read(spreadsheet_id, tab)

        # Format output
        formatter = get_format(fmt)
        data = formatter.write(df)

        config = SheetConfig(
            spreadsheet_id=spreadsheet_id,
            tab=tab,
            format=fmt,
            url=url,
        )

        content = format_content(config, data)
        click.echo(content)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
