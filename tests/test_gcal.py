"""Unit tests for gcal module -- no API calls needed."""

from unittest.mock import patch

from gax.gcal import (
    Cal,
    CalendarEvent,
    Conference,
    api_event_to_dataclass,
    event_to_api_body,
    event_to_yaml,
    yaml_to_event,
    extract_event_id,
    resolve_time_range,
    format_events_markdown,
    format_events_tsv,
    _get_rsvp_status,
)


# =============================================================================
# Inverse pair: CalendarEvent <-> API body
# =============================================================================


SAMPLE_API_EVENT = {
    "id": "evt123",
    "summary": "Team standup",
    "status": "confirmed",
    "start": {"dateTime": "2026-03-15T09:00:00+01:00", "timeZone": "Europe/Berlin"},
    "end": {"dateTime": "2026-03-15T09:30:00+01:00", "timeZone": "Europe/Berlin"},
    "location": "Room 42",
    "description": "Daily sync",
    "attendees": [
        {"email": "alice@x.com", "self": True, "responseStatus": "accepted"},
        {"email": "bob@x.com", "responseStatus": "needsAction"},
    ],
    "recurrence": ["RRULE:FREQ=DAILY"],
    "conferenceData": {
        "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
        "entryPoints": [
            {"entryPointType": "video", "uri": "https://meet.google.com/abc-def"},
        ],
    },
}


class TestEventApiRoundTrip:
    """api_event_to_dataclass and event_to_api_body should round-trip cleanly."""

    def test_basic_fields(self):
        event = api_event_to_dataclass(SAMPLE_API_EVENT, "cal1", "Work")
        assert event.id == "evt123"
        assert event.title == "Team standup"
        assert event.status == "confirmed"
        assert event.start == "2026-03-15T09:00:00+01:00"
        assert event.end == "2026-03-15T09:30:00+01:00"
        assert event.timezone == "Europe/Berlin"
        assert event.location == "Room 42"
        assert event.description == "Daily sync"
        assert event.calendar == "cal1"
        assert event.recurrence == "RRULE:FREQ=DAILY"
        assert event.attendees == ["alice@x.com", "bob@x.com"]
        assert event.conference is not None
        assert event.conference.type == "hangoutsMeet"
        assert event.conference.uri == "https://meet.google.com/abc-def"

    def test_round_trip_preserves_fields(self):
        event = api_event_to_dataclass(SAMPLE_API_EVENT, "cal1", "Work")
        body = event_to_api_body(event)

        assert body["summary"] == "Team standup"
        assert body["status"] == "confirmed"
        assert body["start"] == {
            "dateTime": "2026-03-15T09:00:00+01:00",
            "timeZone": "Europe/Berlin",
        }
        assert body["end"] == {
            "dateTime": "2026-03-15T09:30:00+01:00",
            "timeZone": "Europe/Berlin",
        }
        assert body["location"] == "Room 42"
        assert body["description"] == "Daily sync"
        assert body["attendees"] == [{"email": "alice@x.com"}, {"email": "bob@x.com"}]
        assert body["recurrence"] == ["RRULE:FREQ=DAILY"]

    def test_all_day_event(self):
        api = {
            "id": "allday1",
            "summary": "Holiday",
            "start": {"date": "2026-03-20"},
            "end": {"date": "2026-03-21"},
        }
        event = api_event_to_dataclass(api, "primary", "Primary")
        assert event.start == "2026-03-20"
        assert event.end == "2026-03-21"

        body = event_to_api_body(event)
        assert body["start"] == {"date": "2026-03-20"}
        assert body["end"] == {"date": "2026-03-21"}

    def test_empty_event(self):
        event = api_event_to_dataclass({}, "primary", "Primary")
        assert event.id == ""
        assert event.title == ""
        body = event_to_api_body(event)
        assert body["summary"] == ""

    def test_no_conference(self):
        api = {
            "id": "simple",
            "summary": "Meeting",
            "start": {"date": "2026-01-01"},
            "end": {"date": "2026-01-02"},
        }
        event = api_event_to_dataclass(api, "primary", "Primary")
        assert event.conference is None


# =============================================================================
# Inverse pair: event_to_yaml / yaml_to_event
# =============================================================================


class TestEventYamlRoundTrip:
    def test_round_trip(self):
        original = CalendarEvent(
            id="evt123",
            calendar="primary",
            source="https://calendar.google.com/calendar/event?eid=evt123",
            synced="2026-03-15T00:00:00Z",
            title="Team standup",
            start="2026-03-15T09:00:00+01:00",
            end="2026-03-15T09:30:00+01:00",
            timezone="Europe/Berlin",
            location="Room 42",
            description="Daily sync",
            attendees=["alice@x.com"],
            status="confirmed",
            conference=Conference(
                type="hangoutsMeet", uri="https://meet.google.com/abc"
            ),
        )

        yaml_content = event_to_yaml(original)
        parsed = yaml_to_event(yaml_content)

        assert parsed.id == original.id
        assert parsed.title == original.title
        assert parsed.start == original.start
        assert parsed.end == original.end
        assert parsed.timezone == original.timezone
        assert parsed.location == original.location
        assert parsed.description == original.description
        assert parsed.attendees == original.attendees
        assert parsed.status == original.status
        assert parsed.conference.type == original.conference.type
        assert parsed.conference.uri == original.conference.uri

    def test_minimal_event(self):
        event = CalendarEvent(
            id="",
            calendar="primary",
            source="",
            synced="",
            title="Minimal",
            start="2026-01-01",
            end="2026-01-02",
            timezone="UTC",
        )
        yaml_content = event_to_yaml(event)
        parsed = yaml_to_event(yaml_content)
        assert parsed.title == "Minimal"
        assert parsed.conference is None
        assert parsed.attendees == []

    def test_invalid_frontmatter(self):
        import pytest

        with pytest.raises(ValueError, match="Expected YAML frontmatter"):
            yaml_to_event("not yaml")

        with pytest.raises(ValueError, match="Invalid YAML frontmatter"):
            yaml_to_event("---\nonly one separator")


# =============================================================================
# URL/ID parsing
# =============================================================================


class TestExtractEventId:
    def test_plain_id(self):
        event_id, cal_id = extract_event_id("evt123abc")
        assert event_id == "evt123abc"
        assert cal_id == "primary"

    def test_calendar_url(self):
        # The URL contains base64-encoded "eventId calendarId"
        import base64

        encoded = (
            base64.urlsafe_b64encode(b"evt123 cal@group.calendar.google.com")
            .decode()
            .rstrip("=")
        )
        url = f"https://calendar.google.com/calendar/event?eid={encoded}"
        event_id, cal_id = extract_event_id(url)
        assert event_id == "evt123"
        assert cal_id == "cal@group.calendar.google.com"


# =============================================================================
# Time range resolution
# =============================================================================


class TestResolveTimeRange:
    def test_default_7_days(self):
        t_min, t_max = resolve_time_range(None, None, None)
        diff = t_max - t_min
        assert diff.days == 7

    def test_custom_days(self):
        t_min, t_max = resolve_time_range(14, None, None)
        diff = t_max - t_min
        assert diff.days == 14

    def test_from_to(self):
        t_min, t_max = resolve_time_range(None, "2026-03-01", "2026-03-15")
        assert t_min.date().isoformat() == "2026-03-01"
        # date_to is inclusive, so t_max is day after
        assert t_max.date().isoformat() == "2026-03-16"

    def test_from_only(self):
        t_min, t_max = resolve_time_range(None, "2026-03-01", None)
        assert t_min.date().isoformat() == "2026-03-01"
        diff = t_max - t_min
        assert diff.days == 7

    def test_days_and_from_conflicts(self):
        import pytest

        with pytest.raises(ValueError, match="cannot be combined"):
            resolve_time_range(7, "2026-03-01", None)


# =============================================================================
# Format functions
# =============================================================================


class TestFormatFunctions:
    def _make_event(self, **overrides):
        event = {
            "summary": "Test Event",
            "start": {"dateTime": "2026-03-15T10:00:00+01:00"},
            "end": {"dateTime": "2026-03-15T11:00:00+01:00"},
            "status": "confirmed",
            "_calendar_name": "Work",
            "_calendar_id": "primary",
        }
        event.update(overrides)
        return event

    def test_markdown_empty(self):
        result = format_events_markdown([])
        assert "No upcoming events" in result

    def test_markdown_groups_by_date(self):
        events = [
            self._make_event(summary="Morning"),
            self._make_event(
                summary="Afternoon",
                start={"dateTime": "2026-03-15T14:00:00+01:00"},
                end={"dateTime": "2026-03-15T15:00:00+01:00"},
            ),
        ]
        result = format_events_markdown(events)
        assert "2026-03-15" in result
        assert "Morning" in result
        assert "Afternoon" in result

    def test_markdown_all_day(self):
        events = [
            self._make_event(
                summary="Holiday",
                start={"date": "2026-03-20"},
                end={"date": "2026-03-21"},
            )
        ]
        result = format_events_markdown(events)
        assert "all-day" in result
        assert "Holiday" in result

    def test_tsv_header(self):
        result = format_events_tsv([])
        assert result.startswith("calendar\tdate\tstart\tend")

    def test_tsv_event(self):
        events = [self._make_event()]
        result = format_events_tsv(events)
        lines = result.strip().split("\n")
        assert len(lines) == 2  # header + 1 event
        fields = lines[1].split("\t")
        assert fields[0] == "Work"  # calendar name
        assert fields[5] == "Test Event"  # title

    def test_tsv_with_description(self):
        events = [self._make_event(description="Some notes")]
        result = format_events_tsv(events, include_desc=True)
        assert "description" in result.split("\n")[0]
        assert "Some notes" in result

    def test_rsvp_status(self):
        event = self._make_event(
            attendees=[
                {"email": "me@x.com", "self": True, "responseStatus": "declined"},
            ]
        )
        assert _get_rsvp_status(event) == "declined"

    def test_rsvp_no_self(self):
        event = self._make_event(
            attendees=[
                {"email": "other@x.com", "responseStatus": "accepted"},
            ]
        )
        assert _get_rsvp_status(event) == ""

    def test_rsvp_no_attendees(self):
        event = self._make_event()
        assert _get_rsvp_status(event) == ""


# =============================================================================
# event_diff
# =============================================================================


class TestEventDiff:
    def _make_local_event(self, **overrides):
        defaults = dict(
            id="evt123",
            calendar="primary",
            source="https://calendar.google.com/calendar/event?eid=evt123",
            synced="2026-03-15T00:00:00Z",
            title="Team standup",
            start="2026-03-15T09:00:00+01:00",
            end="2026-03-15T09:30:00+01:00",
            timezone="Europe/Berlin",
            location="Room 42",
            description="Daily sync",
            attendees=["alice@x.com"],
            status="confirmed",
        )
        defaults.update(overrides)
        return CalendarEvent(**defaults)

    def test_new_event_returns_summary(self, tmp_path):
        event = self._make_local_event(id="", title="Launch party",
                                       start="2026-04-01T18:00:00Z",
                                       end="2026-04-01T20:00:00Z")
        f = tmp_path / "new.cal.gax.md"
        f.write_text(event_to_yaml(event))

        result = Cal().event_diff(f)
        assert result is not None
        assert "New event: Launch party" in result
        assert "2026-04-01T18:00:00Z" in result

    @patch("gax.gcal.get_event")
    @patch("gax.gcal.api_event_to_dataclass")
    def test_no_changes_returns_none(self, mock_to_dc, mock_get, tmp_path):
        local = self._make_local_event()
        mock_get.return_value = {}
        mock_to_dc.return_value = self._make_local_event()

        f = tmp_path / "event.cal.gax.md"
        f.write_text(event_to_yaml(local))

        result = Cal().event_diff(f)
        assert result is None
        mock_get.assert_called_once_with("evt123", "primary")

    @patch("gax.gcal.get_event")
    @patch("gax.gcal.api_event_to_dataclass")
    def test_field_changes(self, mock_to_dc, mock_get, tmp_path):
        local = self._make_local_event(
            title="Renamed standup",
            location="Room 99",
            description="Updated sync",
        )
        mock_get.return_value = {}
        mock_to_dc.return_value = self._make_local_event()

        f = tmp_path / "event.cal.gax.md"
        f.write_text(event_to_yaml(local))

        result = Cal().event_diff(f)
        assert result is not None
        assert "title: Team standup -> Renamed standup" in result
        assert "location: Room 42 -> Room 99" in result
        assert "description: Daily sync -> Updated sync" in result
        # Unchanged fields should not appear
        assert "start:" not in result
        assert "timezone:" not in result
