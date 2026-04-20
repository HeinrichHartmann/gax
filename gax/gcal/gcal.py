"""Google Calendar sync for gax.

Resource module -- follows the draft.py reference pattern.

Calendar viewing and event editing (ADR 007).

Module structure
================

  Data classes         -- Conference, CalendarEvent
  API helpers          -- service, list/get/create/update/delete events
  Inverse pairs        -- api_event_to_dataclass / event_to_api_body
  Format functions     -- markdown and TSV event formatting
  Event file format    -- event_to_yaml / yaml_to_event
  URL/ID parsing       -- extract_event_id
  Resolution helpers   -- resolve_calendar_id, resolve_time_range
  Cal(Resource)        -- resource class (the public interface for cli.py)

Design decisions
================

Same conventions as draft.py (see its docstring for full rationale).
Additional notes specific to calendar:

  Two file types: gax/cal (single event YAML) and gax/cal-list
  (TSV list with frontmatter). The list file stores query parameters
  (days, from/to, calendar) in frontmatter so pull can re-fetch.

  Event push handles both create (no ID) and update (has ID).
  After creating, the local file is updated with the new event ID.

  Calendar resolution supports name, full ID, or numeric index.
  Time range supports --days or --from/--to (mutually exclusive).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from googleapiclient.discovery import build

from ..auth import get_authenticated_credentials
from ..resource import Resource

logger = logging.getLogger(__name__)


# =============================================================================
# Data classes
# =============================================================================


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
# API helpers
# =============================================================================


def get_calendar_service():
    """Get authenticated Calendar API service."""
    creds = get_authenticated_credentials()
    return build("calendar", "v3", credentials=creds)


def list_calendars(*, service=None) -> list[dict]:
    """List all calendars. Returns list of {id, name, primary} dicts."""
    if service is None:
        service = get_calendar_service()

    result = service.calendarList().list().execute()
    calendars = []

    for cal in result.get("items", []):
        calendars.append(
            {
                "id": cal["id"],
                "name": cal.get("summary", cal["id"]),
                "primary": cal.get("primary", False),
            }
        )

    return calendars


def list_events(
    *,
    time_min: datetime,
    time_max: datetime,
    calendar_id: str = "primary",
    service=None,
) -> list[dict]:
    """List events from a single calendar within a time range."""
    if service is None:
        service = get_calendar_service()

    # Resolve calendar name for display
    all_calendars = list_calendars(service=service)
    cal_id_to_name = {c["id"]: c["name"] for c in all_calendars}
    cal_name = cal_id_to_name.get(calendar_id, calendar_id)

    events = []
    page_token = None

    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )

        for event in result.get("items", []):
            event["_calendar_name"] = cal_name
            event["_calendar_id"] = calendar_id
            events.append(event)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return events


def get_event(event_id: str, calendar_id: str = "primary", *, service=None) -> dict:
    """Get a single event by ID."""
    if service is None:
        service = get_calendar_service()

    return service.events().get(calendarId=calendar_id, eventId=event_id).execute()


def create_event(event: CalendarEvent, *, service=None) -> dict:
    """Create a new event. Returns created event dict."""
    if service is None:
        service = get_calendar_service()

    body = event_to_api_body(event)

    return (
        service.events()
        .insert(calendarId=event.calendar or "primary", body=body)
        .execute()
    )


def update_event(event: CalendarEvent, *, service=None) -> dict:
    """Update an existing event. Returns updated event dict."""
    if service is None:
        service = get_calendar_service()

    body = event_to_api_body(event)

    return (
        service.events()
        .update(calendarId=event.calendar or "primary", eventId=event.id, body=body)
        .execute()
    )


def delete_event(event_id: str, calendar_id: str = "primary", *, service=None) -> None:
    """Delete an event."""
    if service is None:
        service = get_calendar_service()

    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


# =============================================================================
# Inverse pair: CalendarEvent <-> API
# =============================================================================


def api_event_to_dataclass(
    event: dict, calendar_id: str, calendar_name: str
) -> CalendarEvent:
    """Convert API event dict to CalendarEvent dataclass."""
    start = event.get("start", {})
    end = event.get("end", {})

    start_str = start.get("dateTime", start.get("date", ""))
    end_str = end.get("dateTime", end.get("date", ""))
    tz = start.get("timeZone", "UTC")

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
                    type=conf_data.get("conferenceSolution", {})
                    .get("key", {})
                    .get("type", ""),
                    uri=ep.get("uri", ""),
                )
                break

    attendees = [a.get("email", "") for a in event.get("attendees", [])]

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


def event_to_api_body(event: CalendarEvent) -> dict[str, Any]:
    """Convert CalendarEvent to API request body."""
    body: dict[str, Any] = {
        "summary": event.title,
        "status": event.status,
    }

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
    """Format events as compact agenda view (markdown)."""
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

    lines = []

    for date_str in sorted(by_date.keys()):
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
    """Get RSVP status for current user."""
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

    if "T" in start_str:
        start_time = start_str.split("T")[1][:5]  # HH:MM
        end_time = end_str.split("T")[1][:5] if "T" in end_str else ""
        time_range = f"{start_time}-{end_time}"
    else:
        time_range = "all-day    "  # Pad to align

    # RSVP prefix
    rsvp = _get_rsvp_status(event)
    rsvp_prefix = ""
    if rsvp == "declined":
        rsvp_prefix = "DECLINED "
    elif rsvp == "tentative":
        rsvp_prefix = "[?] "
    elif rsvp == "needsAction":
        rsvp_prefix = "[!] "

    title = event.get("summary", "(No title)")

    status = event.get("status", "confirmed")
    if status == "cancelled":
        title = f"~~{title}~~"

    cal_name = event.get("_calendar_name", "")
    cal_short = cal_name.split("@")[0] if "@" in cal_name else cal_name

    location = event.get("location", "")
    loc_short = location[:30] + "..." if len(location) > 33 else location

    parts = [f"{rsvp_prefix}{time_range}  {title}"]
    if loc_short:
        parts.append(f"  ({loc_short})")
    if cal_short:
        parts.append(f"  @{cal_short}")

    line = "".join(parts)

    if include_desc:
        desc = event.get("description", "")
        if desc:
            desc_clean = desc.replace("\n", " ").replace("\r", "")[:80]
            if len(desc) > 80:
                desc_clean += "..."
            line += f"\n             {desc_clean}"

    return line


def format_events_tsv(events: list[dict], include_desc: bool = False) -> str:
    """Format events as TSV."""
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
# Event file format -- event_to_yaml / yaml_to_event (inverse pair)
# =============================================================================


def event_to_yaml(event: CalendarEvent) -> str:
    """Convert CalendarEvent to split YAML (header + body).

    Header contains gax sync metadata; body contains user-editable event data.
    """
    header: dict[str, Any] = {
        "type": "gax/cal",
        "id": event.id,
        "calendar": event.calendar,
        "source": event.source,
        "synced": event.synced,
    }

    body: dict[str, Any] = {
        "title": event.title,
        "start": event.start,
        "end": event.end,
        "timezone": event.timezone,
    }

    if event.location:
        body["location"] = event.location

    if event.recurrence:
        body["recurrence"] = event.recurrence

    if event.attendees:
        body["attendees"] = event.attendees

    body["status"] = event.status

    if event.conference:
        body["conference"] = {
            "type": event.conference.type,
            "uri": event.conference.uri,
        }

    if event.description:
        body["description"] = event.description

    header_str = yaml.dump(
        header, default_flow_style=False, allow_unicode=True, sort_keys=False
    )
    body_str = yaml.dump(
        body, default_flow_style=False, allow_unicode=True, sort_keys=False
    )

    return f"---\n{header_str}---\n{body_str}"


def yaml_to_event(content: str) -> CalendarEvent:
    """Parse split YAML (header + body) to CalendarEvent.

    Also handles legacy single-section format for backward compatibility.
    """
    if not content.startswith("---"):
        raise ValueError("Expected YAML frontmatter")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Invalid YAML frontmatter format")

    header = yaml.safe_load(parts[1])
    body = yaml.safe_load(parts[2])
    # Merge header and body — handles both split and legacy single-section format
    data = {**header, **(body or {})}

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


# =============================================================================
# URL/ID parsing
# =============================================================================


def extract_event_id(url_or_id: str) -> tuple[str, str]:
    """Extract event ID and calendar ID from URL or ID.

    Returns (event_id, calendar_id). Calendar ID may be "primary".
    """
    if "calendar.google.com" in url_or_id or "google.com/calendar" in url_or_id:
        match = re.search(r"[?&]eid=([^&]+)", url_or_id)
        if match:
            import base64

            try:
                decoded = base64.urlsafe_b64decode(match.group(1) + "==").decode()
                parts = decoded.split()
                if len(parts) >= 2:
                    return parts[0], parts[1]
                return parts[0], "primary"
            except Exception:
                pass

    return url_or_id, "primary"


# =============================================================================
# Resolution helpers
# =============================================================================


def resolve_calendar_id(calendar: str | None) -> str:
    """Resolve calendar name or index to a calendar ID.

    Supports:
        - Name: "Moss"
        - Full ID: "abc123@group.calendar.google.com"
        - Numeric index (1-based): "2"

    Returns "primary" if calendar is None.
    Raises ValueError if not found.
    """
    if not calendar or calendar == "primary":
        return "primary"

    calendars = list_calendars()

    # Try numeric index (1-based)
    if calendar.isdigit():
        idx = int(calendar) - 1
        if 0 <= idx < len(calendars):
            return calendars[idx]["id"]
        raise ValueError(
            f"Calendar index out of range: {calendar} (have {len(calendars)})"
        )

    # Try name or ID match
    for cal in calendars:
        if cal["name"] == calendar or cal["id"] == calendar:
            return cal["id"]

    raise ValueError(f"Calendar not found: {calendar}")


def resolve_time_range(
    days: int | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[datetime, datetime]:
    """Compute time_min/time_max from CLI options.

    --days and --from/--to are mutually exclusive.
    Raises ValueError on conflict.
    """
    has_range = date_from is not None or date_to is not None
    has_days = days is not None

    if has_range and has_days:
        raise ValueError("--days cannot be combined with --from/--to")

    if has_range:
        if date_from is not None:
            t_min = datetime.combine(
                date.fromisoformat(date_from),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
        else:
            t_min = datetime.now(timezone.utc)
        if date_to is not None:
            t_max = datetime.combine(
                date.fromisoformat(date_to) + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
        else:
            t_max = t_min + timedelta(days=7)
        return t_min, t_max

    # Default: --days mode (default 7)
    now = datetime.now(timezone.utc)
    return now, now + timedelta(days=days if days is not None else 7)


def parse_cal_list_file(path: Path) -> tuple[datetime, datetime, str | None, bool]:
    """Parse cal list file header. Returns (time_min, time_max, calendar, verbose)."""
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

    time_min, time_max = resolve_time_range(
        days,
        str(date_from) if date_from is not None else None,
        str(date_to) if date_to is not None else None,
    )
    return time_min, time_max, calendar, verbose


# =============================================================================
# Resource class -- the public interface for cli.py.
# =============================================================================


class Cal(Resource):
    """Google Calendar resource.

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "cal"
    URL_PATTERN = r"calendar\.google\.com/calendar/"
    FILE_TYPE = "gax/cal-list"

    def calendars(self, out, **kw) -> None:
        """List available calendars to file descriptor."""
        cals = list_calendars()
        for cal in cals:
            primary = " (primary)" if cal["primary"] else ""
            out.write(f"{cal['name']}{primary}\n")
            out.write(f"  {cal['id']}\n")

    def clone(
        self,
        output: Path | None = None,
        *,
        calendar: str | None = None,
        days: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        verbose: bool = False,
        **kw,
    ) -> Path:
        """Clone events to a .cal.gax.md list file."""
        time_min, time_max = resolve_time_range(days, date_from, date_to)

        file_path = output or Path("calendar.cal.gax.md")
        if file_path.exists():
            raise ValueError(f"File already exists: {file_path}")

        count = self._clone_events_to_file(
            file_path,
            time_min=time_min,
            time_max=time_max,
            calendar=calendar,
            verbose=verbose,
            days=days,
            date_from=date_from,
            date_to=date_to,
        )
        logger.info(f"Events: {count}")
        return file_path

    def pull(self, **kw) -> None:
        """Pull latest events to existing list file."""
        path = self.path
        time_min, time_max, calendar, verbose = parse_cal_list_file(path)

        # Recover original header values for re-serialization
        header = yaml.safe_load(path.read_text().split("---", 2)[1])

        self._clone_events_to_file(
            path,
            time_min=time_min,
            time_max=time_max,
            calendar=calendar,
            verbose=verbose,
            days=header.get("days"),
            date_from=str(header["from"]) if "from" in header else None,
            date_to=str(header["to"]) if "to" in header else None,
        )

    def checkout(
        self,
        output: Path | None = None,
        *,
        calendar: str | None = None,
        days: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        **kw,
    ) -> Path:
        """Checkout events as individual files into a folder."""
        time_min, time_max = resolve_time_range(days, date_from, date_to)
        folder = output or Path("calendar.cal.gax.md.d")
        folder.mkdir(parents=True, exist_ok=True)

        calendar_id = resolve_calendar_id(calendar)
        events = list_events(
            time_min=time_min, time_max=time_max, calendar_id=calendar_id
        )

        if not events:
            logger.info("Checked out: 0, Skipped: 0")
            return folder

        # Get existing event IDs in output folder
        existing_ids = set()
        for f in folder.glob("*.cal.gax.md"):
            try:
                content = f.read_text()
                if "id:" in content:
                    for line in content.split("\n"):
                        if line.startswith("id:"):
                            existing_ids.add(line.split(":", 1)[1].strip())
                            break
            except Exception:
                pass

        cloned = 0
        skipped = 0

        for event in events:
            event_id = event.get("id", "")
            if event_id in existing_ids:
                skipped += 1
                continue

            cal_id = event.get("_calendar_id", "primary")
            cal_name = event.get("_calendar_name", cal_id)
            event_data = api_event_to_dataclass(event, cal_id, cal_name)

            title = event.get("summary", "event")
            safe_title = re.sub(r"[^\w\s-]", "", title)[:30].strip()
            safe_title = re.sub(r"\s+", "_", safe_title)
            start = event.get("start", {})
            date_str = start.get("dateTime", start.get("date", ""))[:10]
            filename = f"{date_str}_{safe_title}.cal.gax.md"

            file_path = folder / filename
            if file_path.exists():
                file_path = (
                    folder / f"{date_str}_{safe_title}_{event_id[:8]}.cal.gax.md"
                )

            content = event_to_yaml(event_data)
            file_path.write_text(content)
            cloned += 1
            logger.info(f"Writing {filename}")

        logger.info(f"Checked out: {cloned}, Skipped: {skipped}")
        return folder

    def _clone_events_to_file(
        self,
        path: Path,
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
        calendar_id = resolve_calendar_id(calendar)
        events = list_events(
            time_min=time_min, time_max=time_max, calendar_id=calendar_id
        )

        # Build header -- store either days or from/to for pull
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

        tsv_body = format_events_tsv(events, include_desc=verbose)

        with open(path, "w") as f:
            f.write("---\n")
            yaml.dump(
                header,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            f.write("---\n")
            f.write(tsv_body)

        return len(events)


# =============================================================================
# Event resource — single calendar event (clone/pull/diff/push/delete).
# =============================================================================


class Event(Resource):
    """Google Calendar event resource.

    Constructed via from_url(url) or from_file(path).
    Operations use instance state (self.url, self.path).
    """

    name = "event"
    URL_PATTERN = r"calendar\.google\.com/calendar/"
    FILE_TYPE = "gax/cal"
    FILE_EXTENSIONS = (".cal.gax.md",)
    HAS_GENERIC_DISPATCH = False

    @classmethod
    def from_id(cls, id_value: str) -> "Event":
        """Construct from a Calendar event ID."""
        if re.fullmatch(r"[A-Za-z0-9_-]+", id_value):
            return cls(url=id_value)
        raise ValueError(f"Not a Calendar event ID: {id_value}")

    def clone(
        self, output: Path | None = None, *, calendar: str = "primary", **kw
    ) -> Path:
        """Clone a single event to a .cal.gax.md file."""
        event_id, cal_id = extract_event_id(self.url)
        if calendar != "primary":
            cal_id = calendar

        # Get calendar name
        cals = list_calendars()
        cal_name = cal_id
        for cal in cals:
            if cal["id"] == cal_id:
                cal_name = cal["name"]
                break

        api_event = get_event(event_id, cal_id)
        event = api_event_to_dataclass(api_event, cal_id, cal_name)

        if not output:
            safe_title = re.sub(r"[^\w\s-]", "", event.title)[:30].strip()
            safe_title = re.sub(r"\s+", "_", safe_title)
            output = Path(f"{safe_title}.cal.gax.md")

        if output.exists():
            raise ValueError(f"File already exists: {output}")

        content = event_to_yaml(event)
        output.write_text(content)
        return output

    def new(self, *, calendar: str = "primary", output: Path | None = None) -> Path:
        """Create a new event template file."""
        now = datetime.now(timezone.utc)
        start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        end = start + timedelta(hours=1)

        event = CalendarEvent(
            id="",
            calendar=calendar,
            source="",
            synced="",
            title="New Event",
            start=start.isoformat().replace("+00:00", "Z"),
            end=end.isoformat().replace("+00:00", "Z"),
            timezone="UTC",
            status="confirmed",
        )

        file_path = output or Path("new_event.cal.gax.md")
        content = event_to_yaml(event)
        file_path.write_text(content)
        return file_path

    def pull(self, **kw) -> None:
        """Pull latest event data from API."""
        content = self.path.read_text()
        local_event = yaml_to_event(content)

        if not local_event.id:
            raise ValueError("Event has no ID (not yet pushed upstream)")

        api_event = get_event(local_event.id, local_event.calendar)

        cals = list_calendars()
        cal_name = local_event.calendar
        for cal in cals:
            if cal["id"] == local_event.calendar:
                cal_name = cal["name"]
                break

        updated_event = api_event_to_dataclass(
            api_event, local_event.calendar, cal_name
        )
        new_content = event_to_yaml(updated_event)
        self.path.write_text(new_content)

    def diff(self, **kw) -> str | None:
        """Preview changes between local event file and remote.

        Returns a human-readable diff string, or None if no changes.
        For new events (no id), returns a summary of what will be created.
        """
        content = self.path.read_text()
        local = yaml_to_event(content)

        if not local.id:
            return f"New event: {local.title}\n{local.start} — {local.end}"

        api_event = get_event(local.id, local.calendar)
        remote = api_event_to_dataclass(api_event, local.calendar, local.calendar)

        # Compare editable fields
        fields = [
            ("title", local.title, remote.title),
            ("start", local.start, remote.start),
            ("end", local.end, remote.end),
            ("timezone", local.timezone, remote.timezone),
            ("location", local.location, remote.location),
            ("status", local.status, remote.status),
            ("recurrence", local.recurrence, remote.recurrence),
            ("attendees", local.attendees, remote.attendees),
            ("description", local.description, remote.description),
        ]

        lines = []
        for name, local_val, remote_val in fields:
            if local_val != remote_val:
                lines.append(f"{name}: {remote_val} -> {local_val}")

        return "\n".join(lines) if lines else None

    def push(self, **kw) -> str:
        """Push local event changes to API. Returns result URL."""
        content = self.path.read_text()
        local_event = yaml_to_event(content)

        if local_event.id:
            result = update_event(local_event)
            return result.get("htmlLink", "")
        else:
            result = create_event(local_event)

            # Update local file with new ID
            local_event.id = result["id"]
            local_event.source = (
                f"https://calendar.google.com/calendar/event?eid={result['id']}"
            )
            local_event.synced = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )

            new_content = event_to_yaml(local_event)
            self.path.write_text(new_content)

            return result.get("htmlLink", "")

    def delete(self) -> str:
        """Delete event from calendar and local file. Returns event title."""
        content = self.path.read_text()
        local_event = yaml_to_event(content)

        if not local_event.id:
            raise ValueError("Event has no ID (not on calendar)")

        delete_event(local_event.id, local_event.calendar)
        self.path.unlink()
        return local_event.title
