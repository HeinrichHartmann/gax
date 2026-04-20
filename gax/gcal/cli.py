"""CLI commands for Google Calendar operations."""

import sys
import click
from pathlib import Path

from ..cli_lib import handle_errors, success
from .. import docs
from . import Cal, Event


@docs.section("resource")
@click.group(name="cal")
def cal_group():
    """Google Calendar sync commands."""
    pass


@cal_group.command(name="calendars")
@handle_errors
def cal_calendars_cmd():
    """List available calendars."""
    Cal().calendars(sys.stdout)


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
@handle_errors
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
    from . import (
        resolve_time_range,
        resolve_calendar_id,
        list_events,
        format_events_tsv,
        format_events_markdown,
    )

    time_min, time_max = resolve_time_range(days, date_from, date_to)
    calendar_id = resolve_calendar_id(calendar)
    events = list_events(
        time_min=time_min, time_max=time_max, calendar_id=calendar_id
    )

    if fmt == "tsv":
        click.echo(format_events_tsv(events, include_desc=verbose), nl=False)
    else:
        click.echo(format_events_markdown(events, include_desc=verbose), nl=False)


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
@handle_errors
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
    file_path = Cal().clone(
        output=output,
        calendar=calendar,
        days=days,
        date_from=date_from,
        date_to=date_to,
        verbose=verbose,
    )
    success(f"Created: {file_path}")


@cal_group.command(name="pull")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@handle_errors
def cal_pull_cmd(file: Path):
    """Pull latest events to existing file.

    \b
    Example:
        gax cal pull week.cal.gax.md
    """
    Cal(path=file).pull()
    success(f"Updated: {file}")


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
@handle_errors
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
    folder = Cal().checkout(
        output=output,
        calendar=calendar,
        days=days,
        date_from=date_from,
        date_to=date_to,
    )
    success(f"Checked out to: {folder}")


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
@handle_errors
def cal_event_clone_cmd(id_or_url: str, calendar: str, output_path: Path | None):
    """Clone an event to a local .cal.gax.md file."""
    file_path = Event.from_url_or_id(id_or_url).clone(calendar=calendar, output=output_path)
    success(f"Cloned event to {file_path}")


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
@handle_errors
def cal_event_new_cmd(calendar: str, output_path: Path | None):
    """Create a new event file (edit and push to create upstream)."""
    file_path = Event().new(calendar=calendar, output=output_path)
    success(f"Created event template at {file_path}")
    click.echo(f"Edit the file, then run: gax cal event push {file_path}")


@cal_event_group.command(name="pull")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@handle_errors
def cal_event_pull_cmd(file_path: Path):
    """Pull latest event data from API."""
    Event(path=file_path).pull()
    success(f"Pulled latest data to {file_path}")


@cal_event_group.command(name="push")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def cal_event_push_cmd(file_path: Path, yes: bool):
    """Push local changes to API."""
    ev = Event(path=file_path)
    diff_text = ev.diff()
    if diff_text is None:
        click.echo("No changes to push.")
        return
    if not yes:
        click.echo(diff_text)
        if not click.confirm("Push these changes?"):
            click.echo("Cancelled.")
            return

    link = ev.push()
    success(f"Pushed event: {link}")


@cal_event_group.command(name="delete")
@click.argument("file_path", type=click.Path(exists=True, path_type=Path))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
@handle_errors
def cal_event_delete_cmd(file_path: Path, yes: bool):
    """Delete event from calendar."""
    from . import yaml_to_event

    content = file_path.read_text()
    local_event = yaml_to_event(content)

    if not yes:
        click.echo(f"Delete event '{local_event.title}' from calendar?")
        click.echo("This will also delete the local file.")
        if not click.confirm("Proceed?"):
            click.echo("Cancelled.")
            return

    title = Event(path=file_path).delete()
    success(f"Deleted event '{title}'")
