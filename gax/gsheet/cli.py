"""CLI commands for Google Sheets operations."""

import sys
import click
from pathlib import Path

from ..ui import gax_command, confirm_and_push, confirm_and_pull, success
from .. import docs
from . import Sheet, SheetTab
from .sheet import pull_all, _extract_spreadsheet_id
from .client import GSheetClient


def _find_sheet_folder() -> Path:
    """Find a .sheet.gax.md.d folder in the current directory."""
    candidates = list(Path.cwd().glob("*.sheet.gax.md.d"))
    if len(candidates) == 0:
        raise ValueError("No .sheet.gax.md.d folder found in current directory")
    elif len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        raise ValueError(f"Multiple .sheet.gax.md.d folders found: {names}")
    return candidates[0]


@docs.section("resource")
@click.group()
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
@gax_command
def sheet_clone(url: str, output: Path | None, fmt: str, quiet: bool):
    """Clone first tab from a spreadsheet to a .sheet.gax.md file.

    For all tabs, use 'gax sheet checkout'.
    """
    file_path = SheetTab.from_url(url).clone(output=output, fmt=fmt)
    click.echo(f"Created: {file_path}")

    if not quiet:
        spreadsheet_id = _extract_spreadsheet_id(url)
        info = GSheetClient().get_spreadsheet_info(spreadsheet_id)
        if len(info["tabs"]) > 1:
            first_tab = info["tabs"][0]["title"]
            click.echo(
                f'  Tab "{first_tab}" cloned (1 of {len(info["tabs"])} tabs).\n'
                f"  For all tabs: gax sheet checkout {url}"
            )


@sheet.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation, overwrite local state")
@gax_command
def sheet_pull(file: Path, yes: bool):
    """Pull latest data for all tabs in a multipart file or checkout folder."""
    if file.is_dir():
        confirm_and_pull(Sheet(path=file), yes=yes)
    else:
        rows = pull_all(file)
        success(f"Pulled {rows} rows to {file}")


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
@gax_command
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
    folder = Sheet.from_url(url).checkout(output=output, fmt=fmt)
    success(f"Checked out to: {folder}")


@sheet.command("push")
@click.argument("folder", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@gax_command
def sheet_push(folder: Path, with_formulas: bool, yes: bool):
    """Push all tabs in a checkout folder to Google Sheets.

    Shows a diff preview of changes and prompts for confirmation before pushing.

    \b
    Examples:
        gax sheet push Budget.sheet.gax.md.d
        gax sheet push Budget.sheet.gax.md.d -y
        gax sheet push Budget.sheet.gax.md.d --with-formulas
    """
    confirm_and_push(Sheet(path=folder), yes=yes, with_formulas=with_formulas)


@sheet.command("plan")
@click.argument("folder", type=click.Path(exists=True, path_type=Path), required=False)
@gax_command
def sheet_plan(folder):
    """Show what changes would be pushed to Google Sheets.

    Similar to 'terraform plan' - previews changes without applying them.
    If no folder is specified, looks for a .sheet.gax.md.d folder in the current directory.

    \b
    Examples:
        gax sheet plan
        gax sheet plan Budget.sheet.gax.md.d
    """
    if folder is None:
        folder = _find_sheet_folder()

    diff_text = Sheet(path=folder).diff()
    if diff_text is None:
        click.echo("No changes to push.")
    else:
        click.echo("\n" + diff_text)
        click.echo(
            "\nRun 'gax sheet apply' to push these changes, or 'gax sheet push <folder>' with confirmation."
        )


@sheet.command("apply")
@click.argument("folder", type=click.Path(exists=True, path_type=Path), required=False)
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@gax_command
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
    if folder is None:
        folder = _find_sheet_folder()

    confirm_and_push(Sheet(path=folder), yes=yes, with_formulas=with_formulas)


@sheet.group()
def tab():
    """Single tab operations"""
    pass


@tab.command("list")
@click.argument("url")
@gax_command
def tab_list(url: str):
    """List tabs in a spreadsheet (TSV output)."""
    Sheet.from_url(url).tab_list(sys.stdout)


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
@gax_command
def tab_clone(url: str, tab_name: str, output: Path | None, fmt: str):
    """Clone a single tab to a .sheet.gax.md file."""
    path = SheetTab.from_url(url).clone(output=output, tab_name=tab_name, fmt=fmt)
    success(f"Created: {path}")


@tab.command("pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@gax_command
def tab_pull(file: Path):
    """Pull latest data for a single tab."""
    SheetTab(path=file).pull()
    success(f"Updated: {file}")


@tab.command("push")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--with-formulas", is_flag=True, help="Interpret formulas (e.g. =SUM(A1:A10))"
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt")
@gax_command
def tab_push(file: Path, with_formulas: bool, yes: bool):
    """Push local data to a single tab."""
    from .frontmatter import parse_file
    from ..formats import get_format as get_fmt

    config, data = parse_file(file)
    fmt = get_fmt(config.format)
    df = fmt.read(data)
    row_count = len(df)

    click.echo(f"Push {row_count} rows from {file} to {config.tab}?")
    if not yes and not click.confirm("Proceed?"):
        click.echo("Aborted.")
        return

    SheetTab(path=file).push(with_formulas=with_formulas)
    click.echo(f"Pushed {row_count} rows")
