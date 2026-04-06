"""Google Calendar sync for gax.

Implements calendar viewing and event editing (ADR 007).
"""

import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import click
import yaml
from googleapiclient.discovery import build

from .auth import get_authenticated_credentials
from .ui import operation, success, error

logger = logging.getLogger(__name__)


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
    time_min: datetime,
    time_max: datetime,
    calendar_id: str = "primary",
    service=None,
) -> list[dict]:
    """List events from a single calendar within a time range.

    Args:
        time_min: Start of time range (inclusive)
        time_max: End of time range (exclusive)
        calendar_id: Calendar ID to query (default: "primary")
        service: Optional Calendar API service

    Returns:
        List of event dicts with calendar name.
    """
    if service is None:
        service = get_calendar_service()

    # Resolve calendar name for display
    all_calendars = list_calendars(service=service)
    cal_id_to_name = {c["id"]: c["name"] for c in all_calendars}
    cal_name = cal_id_to_name.get(calendar_id, calendar_id)

    events = []
    page_token = None

    while True:
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
            pageToken=page_token,
        ).execute()

        for event in result.get("items", []):
            event["_calendar_name"] = cal_name
            event["_calendar_id"] = calendar_id
            events.append(event)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return events


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


def _get_rsvp_status(event: dict) -> str:
    """Get RSVP status for current user.

    Returns: accepted, declined, tentative, needsAction, or empty string.
    """
    attendees = event.get("attendees", [])
    for attendee in attendees:
        if attendee.get("self"):
            return attendee.get("responseStatus", "")
    return ""


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

    # RSVP prefix (only for non-accepted states)
    rsvp = _get_rsvp_status(event)
    rsvp_prefix = ""
    if rsvp == "declined":
        rsvp_prefix = "DECLINED "
    elif rsvp == "tentative":
        rsvp_prefix = "[?] "
    elif rsvp == "needsAction":
        rsvp_prefix = "[!] "

    title = event.get("summary", "(No title)")

    # Status indicator (event status, not RSVP)
    status = event.get("status", "confirmed")
    if status == "cancelled":
        title = f"~~{title}~~"

    # Calendar name (short form)
    cal_name = event.get("_calendar_name", "")
    cal_short = cal_name.split("@")[0] if "@" in cal_name else cal_name

    # Location
    location = event.get("location", "")
    loc_short = location[:30] + "..." if len(location) > 33 else location

    # Build line: [rsvp] time  title  [location]  @calendar
    parts = [f"{rsvp_prefix}{time_range}  {title}"]
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
    header = "calendar\tdate\tstart\tend\trsvp\ttitle\tid\tstatus\tlocation"
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

        rsvp = _get_rsvp_status(event)
        title = event.get("summary", "").replace("\t", " ")
        event_id = event.get("id", "")
        status = event.get("status", "confirmed")
        location = event.get("location", "").replace("\t", " ")

        row = (
            f"{cal_name}\t{date_str}\t{start_time}\t{end_time}\t{rsvp}\t"
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


def _resolve_calendar_id(calendar: str | None) -> str:
    """Resolve calendar name or index to a calendar ID.

    Supports:
        - Name: "Moss"
        - Full ID: "abc123@group.calendar.google.com"
        - Numeric index (1-based): "2"

    Returns "primary" if calendar is None.
    """
    if not calendar:
        return "primary"

    calendars = list_calendars()

    # Try numeric index (1-based)
    if calendar.isdigit():
        idx = int(calendar) - 1
        if 0 <= idx < len(calendars):
            return calendars[idx]["id"]
        click.echo(f"Calendar index out of range: {calendar} (have {len(calendars)})", err=True)
        raise SystemExit(1)

    # Try name or ID match
    for cal in calendars:
        if cal["name"] == calendar or cal["id"] == calendar:
            return cal["id"]

    click.echo(f"Calendar not found: {calendar}", err=True)
    raise SystemExit(1)


def _parse_date(s: str) -> date:
    """Parse a YYYY-MM-DD date string."""
    return date.fromisoformat(s)


def _resolve_time_range(
    days: int | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[datetime, datetime]:
    """Compute time_min/time_max from CLI options.

    --days and --from/--to are mutually exclusive.
    """
    has_range = date_from is not None or date_to is not None
    has_days = days is not None

    if has_range and has_days:
        click.echo("Error: --days cannot be combined with --from/--to", err=True)
        raise SystemExit(1)

    if has_range:
        if date_from is not None:
            t_min = datetime.combine(_parse_date(date_from), datetime.min.time(), tzinfo=timezone.utc)
        else:
            t_min = datetime.now(timezone.utc)
        if date_to is not None:
            t_max = datetime.combine(_parse_date(date_to) + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        else:
            t_max = t_min + timedelta(days=7)
        return t_min, t_max

    # Default: --days mode (default 7)
    now = datetime.now(timezone.utc)
    return now, now + timedelta(days=days if days is not None else 7)


@cal_cli.command(name="list")
@click.argument("calendar", required=False)
@click.option("--days", "-d", default=None, type=int, help="Number of days to show (default: 7)")
@click.option("--from", "date_from", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "date_to", default=None, help="End date (YYYY-MM-DD)")
@click.option(
    "--format", "-f", "fmt",
    type=click.Choice(["md", "tsv"]),
    default="md",
    help="Output format (default: md)"
)
@click.option("-v", "--verbose", is_flag=True, help="Include event descriptions")
def list_cmd(calendar: str | None, days: int | None, date_from: str | None, date_to: str | None, fmt: str, verbose: bool):
    """List events from a calendar.

    CALENDAR is a calendar name, ID, or numeric index (from 'gax cal calendars').
    Defaults to the primary calendar.

    \b
    Examples:
        gax cal list                  # Primary calendar, next 7 days
        gax cal list -d 14            # Next 14 days
        gax cal list Work             # "Work" calendar
        gax cal list Work -d 3        # "Work" calendar, next 3 days
        gax cal list --from 2026-03-01 --to 2026-03-15
        gax cal list -f tsv           # TSV output
    """
    time_min, time_max = _resolve_time_range(days, date_from, date_to)
    calendar_id = _resolve_calendar_id(calendar)
    events = list_events(time_min=time_min, time_max=time_max, calendar_id=calendar_id)

    if fmt == "tsv":
        click.echo(format_events_tsv(events, include_desc=verbose), nl=False)
    else:
        click.echo(format_events_markdown(events, include_desc=verbose), nl=False)


@cal_cli.command(name="clone")
@click.argument("calendar", required=False)
@click.option("-o", "--output", default="calendar.cal.gax", help="Output file (default: calendar.cal.gax)")
@click.option("--days", "-d", default=None, type=int, help="Number of days (default: 7)")
@click.option("--from", "date_from", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "date_to", default=None, help="End date (YYYY-MM-DD)")
@click.option("-v", "--verbose", is_flag=True, help="Include event descriptions")
def clone_cmd(calendar: str | None, output: str, days: int | None, date_from: str | None, date_to: str | None, verbose: bool):
    """Clone events to a .cal.gax file.

    Creates a file with all events that can be updated with 'gax cal pull'.
    CALENDAR defaults to primary calendar.

    \b
    Examples:
        gax cal clone
        gax cal clone Work -o week.cal.gax -d 7
        gax cal clone --from 2026-03-01 --to 2026-03-31 -o march.cal.gax
    """
    from pathlib import Path

    time_min, time_max = _resolve_time_range(days, date_from, date_to)

    path = Path(output)
    if path.exists():
        click.echo(f"Error: {output} already exists. Use 'gax cal pull' to update.", err=True)
        raise SystemExit(1)

    count = _clone_events_to_file(
        path, time_min=time_min, time_max=time_max,
        calendar=calendar, verbose=verbose,
        days=days, date_from=date_from, date_to=date_to,
    )
    success(f"Cloned {count} events to {output}")


@cal_cli.command(name="pull")
@click.argument("file", type=click.Path(exists=True))
def pull_cmd(file: str):
    """Pull latest events to existing file.

    \b
    Example:
        gax cal pull week.cal.gax
    """
    from pathlib import Path

    path = Path(file)
    time_min, time_max, calendar, verbose = _parse_cal_list_file(path)
    # Recover original header values for re-serialization
    import yaml as _yaml
    _header = _yaml.safe_load(path.read_text().split("---", 2)[1])
    count = _clone_events_to_file(
        path, time_min=time_min, time_max=time_max,
        calendar=calendar, verbose=verbose,
        days=_header.get("days"),
        date_from=str(_header["from"]) if "from" in _header else None,
        date_to=str(_header["to"]) if "to" in _header else None,
    )
    success(f"Pulled {count} events to {file}")


@cal_cli.command(name="checkout")
@click.argument("calendar", required=False)
@click.option("-o", "--output", default="calendar.cal.gax.d", type=click.Path(path_type=Path), help="Output folder (default: calendar.cal.gax.d)")
@click.option("--days", "-d", default=None, type=int, help="Number of days (default: 7)")
@click.option("--from", "date_from", default=None, help="Start date (YYYY-MM-DD)")
@click.option("--to", "date_to", default=None, help="End date (YYYY-MM-DD)")
def checkout_cmd(calendar: str | None, output: Path, days: int | None, date_from: str | None, date_to: str | None):
    """Checkout events as individual .cal.gax files into a folder.

    Each event becomes a separate file that can be edited and pushed.
    CALENDAR defaults to primary calendar.

    \b
    Examples:
        gax cal checkout
        gax cal checkout -o Week/
        gax cal checkout Work -o Week/ -d 7
        gax cal checkout --from 2026-03-01 --to 2026-03-31 -o March/

    \b
    Workflow:
        1. checkout -> create folder with .cal.gax files
        2. edit files as needed
        3. gax push <file> to update calendar
    """
    time_min, time_max = _resolve_time_range(days, date_from, date_to)
    # Create folder
    output.mkdir(parents=True, exist_ok=True)

    # Get events
    calendar_id = _resolve_calendar_id(calendar)
    events = list_events(time_min=time_min, time_max=time_max, calendar_id=calendar_id)

    if not events:
        click.echo("No events found.")
        return

    click.echo(f"Found {len(events)} events")

    # Get existing event IDs in output folder
    existing_ids = set()
    for f in output.glob("*.cal.gax"):
        try:
            content = f.read_text()
            if "id:" in content:
                for line in content.split("\n"):
                    if line.startswith("id:"):
                        existing_ids.add(line.split(":", 1)[1].strip())
                        break
        except Exception:
            pass

    # Clone each event
    cloned = 0
    skipped = 0

    with operation("Checking out events", total=len(events)) as op:
        for event in events:
            event_id = event.get("id", "")
            if event_id in existing_ids:
                skipped += 1
                op.advance()
                continue

            try:
                cal_id = event.get("_calendar_id", "primary")
                cal_name = event.get("_calendar_name", cal_id)
                event_data = api_event_to_dataclass(event, cal_id, cal_name)

                # Generate filename
                title = event.get("summary", "event")
                safe_title = re.sub(r"[^\w\s-]", "", title)[:30].strip()
                safe_title = re.sub(r"\s+", "_", safe_title)
                start = event.get("start", {})
                date_str = start.get("dateTime", start.get("date", ""))[:10]
                filename = f"{date_str}_{safe_title}.cal.gax"

                logger.info(f"Writing {filename}")

                file_path = output / filename

                # Avoid overwriting
                if file_path.exists():
                    file_path = output / f"{date_str}_{safe_title}_{event_id[:8]}.cal.gax"

                content = event_to_yaml(event_data)
                file_path.write_text(content)
                cloned += 1
                click.echo(f"  {filename}")

            except Exception as e:
                click.echo(f"  Error cloning event: {e}", err=True)

            op.advance()

    success(f"Checked out: {cloned}, Skipped: {skipped} (already present)")


def _clone_events_to_file(
    path: "Path",
    *,
    time_min: datetime,
    time_max: datetime,
    calendar: str | None = None,
    verbose: bool = False,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    """Clone events to file. Returns event count."""
    calendar_id = _resolve_calendar_id(calendar)
    events = list_events(time_min=time_min, time_max=time_max, calendar_id=calendar_id)

    # Build header — store either days or from/to for pull
    header: dict = {
        "type": "gax/cal-list",
        "content-type": "text/tab-separated-values",
    }
    if date_from is not None or date_to is not None:
        if date_from is not None:
            header["from"] = date_from
        if date_to is not None:
            header["to"] = date_to
    else:
        header["days"] = days if days is not None else 7
    header["pulled"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def _parse_cal_list_file(path: "Path") -> tuple[datetime, datetime, str | None, bool]:
    """Parse cal list file header. Returns (time_min, time_max, calendar, verbose)."""
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

    date_from = header.get("from")
    date_to = header.get("to")
    days = header.get("days")
    calendar = header.get("calendar")
    verbose = header.get("verbose", False)

    time_min, time_max = _resolve_time_range(
        days,
        str(date_from) if date_from is not None else None,
        str(date_to) if date_to is not None else None,
    )
    return time_min, time_max, calendar, verbose


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

    success(f"Cloned event to {output_path}")


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

    success(f"Created event template at {output_path}")
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

    success(f"Pulled latest data to {file_path}")


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
        success(f"Updated event: {result.get('htmlLink', '')}")
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

        success(f"Created event: {result.get('htmlLink', '')}")


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

    success(f"Deleted event '{local_event.title}'")
