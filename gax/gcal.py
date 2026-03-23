"""Google Calendar sync for gax.

Implements calendar viewing and event editing (ADR 007).
"""

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import click
import yaml
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials


@dataclass
class Conference:
    """Video conference info."""

    type: str  # hangoutsMeet, etc.
    uri: str


@dataclass
class CalendarEvent:
    """A calendar event."""

    id: str
    calendar: str
    source: str
    synced: str  # ISO format
    title: str
    start: str  # ISO format with offset
    end: str  # ISO format with offset
    timezone: str
    location: str = ""
    recurrence: str = ""  # RRULE
    attendees: list[str] = field(default_factory=list)
    status: str = "confirmed"  # confirmed, tentative, cancelled
    conference: Optional[Conference] = None
    description: str = ""


# =============================================================================
# Calendar API functions
# =============================================================================


def get_calendar_service():
    """Get authenticated Calendar API service."""
    creds = get_authenticated_credentials()
    return build("calendar", "v3", credentials=creds)


def list_calendars(*, service=None) -> list[dict]:
    """List all calendars.

    Returns:
        List of {id, name, primary} dicts.
    """
    if service is None:
        service = get_calendar_service()

    result = service.calendarList().list().execute()
    calendars = []

    for cal in result.get("items", []):
        calendars.append({
            "id": cal["id"],
            "name": cal.get("summary", cal["id"]),
            "primary": cal.get("primary", False),
        })

    return calendars


def list_events(
    *,
    days: int = 7,
    calendar_id: str | None = None,
    service=None,
) -> list[dict]:
    """List upcoming events.

    Args:
        days: Number of days to look ahead
        calendar_id: Optional calendar ID to filter (None = all calendars)
        service: Optional Calendar API service

    Returns:
        List of event dicts with calendar name.
    """
    if service is None:
        service = get_calendar_service()

    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days)

    # Get calendars to query
    if calendar_id:
        calendars = [{"id": calendar_id, "name": calendar_id}]
    else:
        calendars = list_calendars(service=service)

    all_events = []

    for cal in calendars:
        try:
            result = service.events().list(
                calendarId=cal["id"],
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            for event in result.get("items", []):
                event["_calendar_name"] = cal["name"]
                event["_calendar_id"] = cal["id"]
                all_events.append(event)
        except Exception:
            # Skip calendars we can't access
            pass

    # Sort by start time
    def get_start(e):
        start = e.get("start", {})
        return start.get("dateTime", start.get("date", ""))

    all_events.sort(key=get_start)

    return all_events


def get_event(event_id: str, calendar_id: str = "primary", *, service=None) -> dict:
    """Get a single event by ID.

    Args:
        event_id: Google Calendar event ID
        calendar_id: Calendar ID (default: primary)
        service: Optional Calendar API service

    Returns:
        Event dict from API.
    """
    if service is None:
        service = get_calendar_service()

    return service.events().get(
        calendarId=calendar_id,
        eventId=event_id,
    ).execute()


def create_event(event: CalendarEvent, *, service=None) -> dict:
    """Create a new event.

    Args:
        event: CalendarEvent to create
        service: Optional Calendar API service

    Returns:
        Created event dict from API.
    """
    if service is None:
        service = get_calendar_service()

    body = _event_to_api_body(event)

    return service.events().insert(
        calendarId=event.calendar or "primary",
        body=body,
    ).execute()


def update_event(event: CalendarEvent, *, service=None) -> dict:
    """Update an existing event.

    Args:
        event: CalendarEvent with updated fields
        service: Optional Calendar API service

    Returns:
        Updated event dict from API.
    """
    if service is None:
        service = get_calendar_service()

    body = _event_to_api_body(event)

    return service.events().update(
        calendarId=event.calendar or "primary",
        eventId=event.id,
        body=body,
    ).execute()


def delete_event(
    event_id: str,
    calendar_id: str = "primary",
    *,
    service=None,
) -> None:
    """Delete an event.

    Args:
        event_id: Event ID to delete
        calendar_id: Calendar ID
        service: Optional Calendar API service
    """
    if service is None:
        service = get_calendar_service()

    service.events().delete(
        calendarId=calendar_id,
        eventId=event_id,
    ).execute()


def _event_to_api_body(event: CalendarEvent) -> dict:
    """Convert CalendarEvent to API request body."""
    body = {
        "summary": event.title,
        "status": event.status,
    }

    # Handle start/end times
    if "T" in event.start:
        body["start"] = {"dateTime": event.start, "timeZone": event.timezone}
        body["end"] = {"dateTime": event.end, "timeZone": event.timezone}
    else:
        # All-day event
        body["start"] = {"date": event.start}
        body["end"] = {"date": event.end}

    if event.location:
        body["location"] = event.location

    if event.description:
        body["description"] = event.description

    if event.attendees:
        body["attendees"] = [{"email": email} for email in event.attendees]

    if event.recurrence:
        body["recurrence"] = [event.recurrence]

    return body


# =============================================================================
# Format functions
# =============================================================================


def format_events_markdown(events: list[dict], include_desc: bool = False) -> str:
    """Format events as compact agenda view.

    Args:
        events: List of event dicts from API
        include_desc: Include event descriptions

    Returns:
        Markdown string grouped by date, calendar info on right.
    """
    if not events:
        return "No upcoming events.\n"

    # Group by date (multiplexing all calendars)
    by_date: dict[str, list] = {}

    for event in events:
        start = event.get("start", {})
        start_str = start.get("dateTime", start.get("date", ""))

        if "T" in start_str:
            date_str = start_str.split("T")[0]
        else:
            date_str = start_str

        if date_str not in by_date:
            by_date[date_str] = []
        by_date[date_str].append(event)

    # Format output
    lines = []

    for date_str in sorted(by_date.keys()):
        # Format date with day of week
        try:
            dt = datetime.fromisoformat(date_str)
            day_name = dt.strftime("%a")
            lines.append(f"## {day_name} {date_str}")
        except Exception:
            lines.append(f"## {date_str}")
        lines.append("")

        for event in sorted(by_date[date_str], key=_event_sort_key):
            lines.append(_format_event_line(event, include_desc))

        lines.append("")

    return "\n".join(lines)


def _event_sort_key(event: dict) -> str:
    """Sort key for events (by start time)."""
    start = event.get("start", {})
    return start.get("dateTime", start.get("date", ""))


def _format_event_line(event: dict, include_desc: bool = False) -> str:
    """Format a single event as compact line."""
    start = event.get("start", {})
    end = event.get("end", {})

    start_str = start.get("dateTime", start.get("date", ""))
    end_str = end.get("dateTime", end.get("date", ""))

    # Time range
    if "T" in start_str:
        start_time = start_str.split("T")[1][:5]  # HH:MM
        end_time = end_str.split("T")[1][:5] if "T" in end_str else ""
        time_range = f"{start_time}-{end_time}"
    else:
        time_range = "all-day    "  # Pad to align

    title = event.get("summary", "(No title)")

    # Status indicator
    status = event.get("status", "confirmed")
    if status == "tentative":
        title = f"{title} [?]"
    elif status == "cancelled":
        title = f"~~{title}~~"

    # Calendar name (short form)
    cal_name = event.get("_calendar_name", "")
    cal_short = cal_name.split("@")[0] if "@" in cal_name else cal_name

    # Location
    location = event.get("location", "")
    loc_short = location[:30] + "..." if len(location) > 33 else location

    # Build line: time  title  [location]  @calendar
    parts = [f"{time_range}  {title}"]
    if loc_short:
        parts.append(f"  ({loc_short})")
    if cal_short:
        parts.append(f"  @{cal_short}")

    line = "".join(parts)

    # Add description on next line if requested
    if include_desc:
        desc = event.get("description", "")
        if desc:
            # Truncate and clean up description
            desc_clean = desc.replace("\n", " ").replace("\r", "")[:80]
            if len(desc) > 80:
                desc_clean += "..."
            line += f"\n             {desc_clean}"

    return line


def format_events_tsv(events: list[dict], include_desc: bool = False) -> str:
    """Format events as TSV.

    Args:
        events: List of event dicts from API
        include_desc: Include description column

    Returns:
        TSV string with header row.
    """
    header = "calendar\tdate\tstart\tend\ttitle\tid\tstatus\tlocation"
    if include_desc:
        header += "\tdescription"
    lines = [header]

    for event in events:
        cal_name = event.get("_calendar_name", "")
        start = event.get("start", {})
        end = event.get("end", {})

        start_str = start.get("dateTime", start.get("date", ""))
        end_str = end.get("dateTime", end.get("date", ""))

        if "T" in start_str:
            date_str = start_str.split("T")[0]
            start_time = start_str.split("T")[1][:5]
            end_time = end_str.split("T")[1][:5] if "T" in end_str else ""
        else:
            date_str = start_str
            start_time = ""
            end_time = ""

        title = event.get("summary", "").replace("\t", " ")
        event_id = event.get("id", "")
        status = event.get("status", "confirmed")
        location = event.get("location", "").replace("\t", " ")

        row = (
            f"{cal_name}\t{date_str}\t{start_time}\t{end_time}\t"
            f"{title}\t{event_id}\t{status}\t{location}"
        )
        if include_desc:
            desc = event.get("description", "").replace("\t", " ").replace("\n", " ")
            row += f"\t{desc}"
        lines.append(row)

    return "\n".join(lines) + "\n"


# =============================================================================
# Event file format (.cal.gax)
# =============================================================================


def event_to_yaml(event: CalendarEvent) -> str:
    """Convert CalendarEvent to YAML file content.

    Returns YAML frontmatter only (no body).
    """
    data = {
        "type": "gax/cal",
        "id": event.id,
        "calendar": event.calendar,
        "source": event.source,
        "synced": event.synced,
        "title": event.title,
        "start": event.start,
        "end": event.end,
        "timezone": event.timezone,
    }

    if event.location:
        data["location"] = event.location

    if event.recurrence:
        data["recurrence"] = event.recurrence

    if event.attendees:
        data["attendees"] = event.attendees

    data["status"] = event.status

    if event.conference:
        data["conference"] = {
            "type": event.conference.type,
            "uri": event.conference.uri,
        }

    if event.description:
        data["description"] = event.description

    return "---\n" + yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False) + "---\n"


def yaml_to_event(content: str) -> CalendarEvent:
    """Parse YAML file content to CalendarEvent."""
    # Extract YAML frontmatter
    if not content.startswith("---"):
        raise ValueError("Expected YAML frontmatter")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid YAML frontmatter format")

    yaml_content = parts[1]
    data = yaml.safe_load(yaml_content)

    conference = None
    if "conference" in data:
        conf_data = data["conference"]
        conference = Conference(
            type=conf_data.get("type", ""),
            uri=conf_data.get("uri", ""),
        )

    return CalendarEvent(
        id=data.get("id", ""),
        calendar=data.get("calendar", "primary"),
        source=data.get("source", ""),
        synced=data.get("synced", ""),
        title=data.get("title", ""),
        start=data.get("start", ""),
        end=data.get("end", ""),
        timezone=data.get("timezone", "UTC"),
        location=data.get("location", ""),
        recurrence=data.get("recurrence", ""),
        attendees=data.get("attendees", []),
        status=data.get("status", "confirmed"),
        conference=conference,
        description=data.get("description", ""),
    )


def api_event_to_dataclass(
    event: dict,
    calendar_id: str,
    calendar_name: str,
) -> CalendarEvent:
    """Convert API event dict to CalendarEvent dataclass."""
    start = event.get("start", {})
    end = event.get("end", {})

    start_str = start.get("dateTime", start.get("date", ""))
    end_str = end.get("dateTime", end.get("date", ""))
    tz = start.get("timeZone", "UTC")

    # Build source URL
    event_id = event.get("id", "")
    source = f"https://calendar.google.com/calendar/event?eid={event_id}"

    # Conference data
    conference = None
    conf_data = event.get("conferenceData", {})
    if conf_data:
        entry_points = conf_data.get("entryPoints", [])
        for ep in entry_points:
            if ep.get("entryPointType") == "video":
                conference = Conference(
                    type=conf_data.get("conferenceSolution", {}).get("key", {}).get("type", ""),
                    uri=ep.get("uri", ""),
                )
                break

    # Attendees
    attendees = [a.get("email", "") for a in event.get("attendees", [])]

    # Recurrence
    recurrence = ""
    if event.get("recurrence"):
        recurrence = event["recurrence"][0] if event["recurrence"] else ""

    return CalendarEvent(
        id=event_id,
        calendar=calendar_id,
        source=source,
        synced=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        title=event.get("summary", ""),
        start=start_str,
        end=end_str,
        timezone=tz,
        location=event.get("location", ""),
        recurrence=recurrence,
        attendees=attendees,
        status=event.get("status", "confirmed"),
        conference=conference,
        description=event.get("description", ""),
    )


# =============================================================================
# URL/ID parsing
# =============================================================================


def extract_event_id(url_or_id: str) -> tuple[str, str]:
    """Extract event ID and calendar ID from URL or ID.

    Args:
        url_or_id: Calendar event URL or event ID

    Returns:
        Tuple of (event_id, calendar_id). Calendar ID may be "primary".
    """
    # Google Calendar event URL formats:
    # https://calendar.google.com/calendar/event?eid=...
    # https://www.google.com/calendar/event?eid=...

    if "calendar.google.com" in url_or_id or "google.com/calendar" in url_or_id:
        match = re.search(r"[?&]eid=([^&]+)", url_or_id)
        if match:
            # eid is base64 encoded
            import base64
            try:
                decoded = base64.urlsafe_b64decode(match.group(1) + "==").decode()
                # Format is "eventId calendarId"
                parts = decoded.split()
                if len(parts) >= 2:
                    return parts[0], parts[1]
                return parts[0], "primary"
            except Exception:
                pass

    # Assume it's just an event ID
    return url_or_id, "primary"


# =============================================================================
# CLI commands
# =============================================================================


@click.group(name="cal")
def cal_cli():
    """Google Calendar sync commands."""
    pass


@cal_cli.command(name="calendars")
def calendars_cmd():
    """List available calendars."""
    calendars = list_calendars()

    for cal in calendars:
        primary = " (primary)" if cal["primary"] else ""
        click.echo(f"{cal['name']}{primary}")
        click.echo(f"  {cal['id']}")


def _resolve_calendar_id(calendar: str | None) -> str | None:
    """Resolve calendar name to ID."""
    if not calendar:
        return None
    calendars = list_calendars()
    for cal in calendars:
        if cal["name"] == calendar or cal["id"] == calendar:
            return cal["id"]
    click.echo(f"Calendar not found: {calendar}", err=True)
    raise SystemExit(1)


@cal_cli.group(name="list", invoke_without_command=True)
@click.option("--days", "-d", default=7, help="Number of days to show (default: 7)")
@click.option("--cal", "-c", "calendar", help="Filter by calendar name or ID")
@click.option(
    "--format", "-f", "fmt",
    type=click.Choice(["md", "tsv"]),
    default="md",
    help="Output format (default: md)"
)
@click.option("-v", "--verbose", is_flag=True, help="Include event descriptions")
@click.pass_context
def list_group(ctx, days: int, calendar: str | None, fmt: str, verbose: bool):
    """List upcoming events.

    Without subcommand, lists events to stdout.

    \b
    Examples:
        gax cal list                  # List next 7 days
        gax cal list -d 14            # List next 14 days
        gax cal list -c Moss          # Filter by calendar
        gax cal list -v               # Include descriptions
        gax cal list clone week.cal.gax  # Clone to file
    """
    # Store options for subcommands
    ctx.ensure_object(dict)
    ctx.obj["days"] = days
    ctx.obj["calendar"] = calendar
    ctx.obj["fmt"] = fmt
    ctx.obj["verbose"] = verbose

    # If no subcommand, run default behavior
    if ctx.invoked_subcommand is None:
        calendar_id = _resolve_calendar_id(calendar)
        events = list_events(days=days, calendar_id=calendar_id)

        if fmt == "tsv":
            click.echo(format_events_tsv(events, include_desc=verbose), nl=False)
        else:
            click.echo(format_events_markdown(events, include_desc=verbose), nl=False)


@list_group.command(name="clone")
@click.argument("file", default="calendar.cal.gax")
@click.option("--days", "-d", default=7, help="Number of days (default: 7)")
@click.option("--cal", "-c", "calendar", help="Filter by calendar name or ID")
@click.option("-v", "--verbose", is_flag=True, help="Include event descriptions")
def list_clone_cmd(file: str, days: int, calendar: str | None, verbose: bool):
    """Clone upcoming events to a .cal.gax file.

    Creates a file with all events that can be updated with 'gax pull'.

    \b
    Examples:
        gax cal list clone
        gax cal list clone week.cal.gax -d 7
        gax cal list clone moss.cal.gax -c Moss
        gax cal list clone -v         # Include descriptions
    """
    from pathlib import Path

    path = Path(file)
    if path.exists():
        click.echo(f"Error: {file} already exists. Use 'gax pull' to update.", err=True)
        raise SystemExit(1)

    count = _clone_events_to_file(path, days=days, calendar=calendar, verbose=verbose)
    click.echo(f"Cloned {count} events to {file}")


@list_group.command(name="pull")
@click.argument("file", type=click.Path(exists=True))
def list_pull_cmd(file: str):
    """Pull latest events to existing file.

    \b
    Example:
        gax cal list pull week.cal.gax
    """
    from pathlib import Path

    path = Path(file)
    days, calendar, verbose = _parse_cal_list_file(path)
    count = _clone_events_to_file(path, days=days, calendar=calendar, verbose=verbose)
    click.echo(f"Pulled {count} events to {file}")


def _clone_events_to_file(
    path: "Path",
    days: int = 7,
    calendar: str | None = None,
    verbose: bool = False,
) -> int:
    """Clone events to file. Returns event count."""
    calendar_id = _resolve_calendar_id(calendar)
    events = list_events(days=days, calendar_id=calendar_id)

    # Build header
    header = {
        "type": "gax/cal-list",
        "content-type": "text/tab-separated-values",
        "days": days,
        "pulled": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if calendar:
        header["calendar"] = calendar
    if verbose:
        header["verbose"] = True

    # Format TSV body
    tsv_body = format_events_tsv(events, include_desc=verbose)

    # Write file with frontmatter
    import yaml
    with open(path, "w") as f:
        f.write("---\n")
        yaml.dump(header, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        f.write("---\n")
        f.write(tsv_body)

    return len(events)


def _parse_cal_list_file(path: "Path") -> tuple[int, str | None, bool]:
    """Parse cal list file header. Returns (days, calendar, verbose)."""
    import yaml

    content = path.read_text()
    if not content.startswith("---"):
        raise ValueError("Expected YAML frontmatter")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid frontmatter format")

    header = yaml.safe_load(parts[1])
    if header.get("type") != "gax/cal-list":
        raise ValueError(f"Expected type: gax/cal-list, got: {header.get('type')}")

    return header.get("days", 7), header.get("calendar"), header.get("verbose", False)


@cal_cli.group(name="event")
def event_group():
    """Event operations (clone, new, pull, push, delete)."""
    pass


@event_group.command(name="clone")
@click.argument("id_or_url")
@click.option("--cal", "-c", "calendar", default="primary", help="Calendar ID (default: primary)")
@click.option("-o", "--output", "output_path", help="Output file path")
def event_clone_cmd(id_or_url: str, calendar: str, output_path: str | None):
    """Clone an event to a local .cal.gax file."""
    event_id, cal_id = extract_event_id(id_or_url)
    if calendar != "primary":
        cal_id = calendar

    # Get calendar name
    calendars = list_calendars()
    cal_name = cal_id
    for cal in calendars:
        if cal["id"] == cal_id:
            cal_name = cal["name"]
            break

    # Fetch event
    api_event = get_event(event_id, cal_id)
    event = api_event_to_dataclass(api_event, cal_id, cal_name)

    # Generate output path
    if not output_path:
        safe_title = re.sub(r"[^\w\s-]", "", event.title)[:30].strip()
        safe_title = re.sub(r"\s+", "_", safe_title)
        output_path = f"{safe_title}.cal.gax"

    # Check for existing file
    if Path(output_path).exists():
        click.echo(f"Error: {output_path} already exists. Use 'pull' to update.", err=True)
        sys.exit(1)

    # Write file
    content = event_to_yaml(event)
    Path(output_path).write_text(content)

    click.echo(f"Cloned event to {output_path}")


@event_group.command(name="new")
@click.option("--cal", "-c", "calendar", default="primary", help="Calendar ID (default: primary)")
@click.option("-o", "--output", "output_path", help="Output file path")
def event_new_cmd(calendar: str, output_path: str | None):
    """Create a new event file (edit and push to create upstream)."""
    # Create template event
    now = datetime.now(timezone.utc)
    start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end = start + timedelta(hours=1)

    event = CalendarEvent(
        id="",  # Empty = new event
        calendar=calendar,
        source="",
        synced="",
        title="New Event",
        start=start.isoformat().replace("+00:00", "Z"),
        end=end.isoformat().replace("+00:00", "Z"),
        timezone="UTC",
        status="confirmed",
    )

    # Generate output path
    if not output_path:
        output_path = "new_event.cal.gax"

    # Write file
    content = event_to_yaml(event)
    Path(output_path).write_text(content)

    click.echo(f"Created event template at {output_path}")
    click.echo("Edit the file, then run: gax cal event push " + output_path)


@event_group.command(name="pull")
@click.argument("file_path", type=click.Path(exists=True))
def event_pull_cmd(file_path: str):
    """Pull latest event data from API."""
    path = Path(file_path)
    content = path.read_text()
    local_event = yaml_to_event(content)

    if not local_event.id:
        click.echo("Error: Event has no ID (not yet pushed upstream)", err=True)
        raise SystemExit(1)

    # Fetch from API
    api_event = get_event(local_event.id, local_event.calendar)

    # Get calendar name
    calendars = list_calendars()
    cal_name = local_event.calendar
    for cal in calendars:
        if cal["id"] == local_event.calendar:
            cal_name = cal["name"]
            break

    updated_event = api_event_to_dataclass(api_event, local_event.calendar, cal_name)

    # Write updated file
    new_content = event_to_yaml(updated_event)
    path.write_text(new_content)

    click.echo(f"Pulled latest data to {file_path}")


@event_group.command(name="push")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def event_push_cmd(file_path: str, yes: bool):
    """Push local changes to API."""
    path = Path(file_path)
    content = path.read_text()
    local_event = yaml_to_event(content)

    if local_event.id:
        # Update existing event
        if not yes:
            click.echo(f"Update event '{local_event.title}'?")
            if not click.confirm("Proceed?"):
                click.echo("Cancelled.")
                return

        result = update_event(local_event)
        click.echo(f"Updated event: {result.get('htmlLink', '')}")
    else:
        # Create new event
        if not yes:
            click.echo(f"Create new event '{local_event.title}'?")
            if not click.confirm("Proceed?"):
                click.echo("Cancelled.")
                return

        result = create_event(local_event)

        # Update local file with new ID
        local_event.id = result["id"]
        local_event.source = f"https://calendar.google.com/calendar/event?eid={result['id']}"
        local_event.synced = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        new_content = event_to_yaml(local_event)
        path.write_text(new_content)

        click.echo(f"Created event: {result.get('htmlLink', '')}")


@event_group.command(name="delete")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def event_delete_cmd(file_path: str, yes: bool):
    """Delete event from calendar."""
    path = Path(file_path)
    content = path.read_text()
    local_event = yaml_to_event(content)

    if not local_event.id:
        click.echo("Error: Event has no ID (not on calendar)", err=True)
        raise SystemExit(1)

    if not yes:
        click.echo(f"Delete event '{local_event.title}' from calendar?")
        click.echo("This will also delete the local file.")
        if not click.confirm("Proceed?"):
            click.echo("Cancelled.")
            return

    delete_event(local_event.id, local_event.calendar)
    path.unlink()

    click.echo(f"Deleted event '{local_event.title}'")
