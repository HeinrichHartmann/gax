"""Unit tests for gax.syncstate."""

from datetime import datetime, timedelta, timezone

import pytest

from gax.syncstate import (
    SyncState,
    format_stale_warning,
    is_stale,
    read_sync,
    write_sync_header,
)

_NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
_TS = "2026-07-26T12:00:00Z"


# ---------------------------------------------------------------------------
# write_sync_header
# ---------------------------------------------------------------------------


class TestWriteSyncHeader:
    def test_adds_sync_block_with_time(self, freezegun_utc):
        result = write_sync_header({})
        assert "sync" in result
        assert "time" in result["sync"]

    def test_preserves_existing_headers(self, freezegun_utc):
        result = write_sync_header({"type": "event", "id": "abc"})
        assert result["type"] == "event"
        assert result["id"] == "abc"
        assert "sync" in result

    def test_includes_rev_when_provided(self, freezegun_utc):
        result = write_sync_header({}, rev="etag-123")
        assert result["sync"]["rev"] == "etag-123"

    def test_omits_rev_when_empty(self, freezegun_utc):
        result = write_sync_header({})
        assert "rev" not in result["sync"]

    def test_does_not_mutate_input(self, freezegun_utc):
        original = {"type": "doc"}
        write_sync_header(original)
        assert "sync" not in original


# ---------------------------------------------------------------------------
# read_sync — new format
# ---------------------------------------------------------------------------


class TestReadSyncNewFormat:
    def test_reads_time_and_rev(self):
        headers = {"sync": {"time": _TS, "rev": "r1"}}
        state = read_sync(headers)
        assert state.time == _NOW
        assert state.rev == "r1"

    def test_reads_time_without_rev(self):
        headers = {"sync": {"time": _TS}}
        state = read_sync(headers)
        assert state.time == _NOW
        assert state.rev == ""

    def test_empty_sync_block(self):
        headers = {"sync": {}}
        state = read_sync(headers)
        assert state.time is None
        assert state.rev == ""


# ---------------------------------------------------------------------------
# read_sync — legacy format
# ---------------------------------------------------------------------------


class TestReadSyncLegacy:
    def test_reads_synced_field(self):
        headers = {"synced": _TS}
        state = read_sync(headers)
        assert state.time == _NOW
        assert state.rev == ""

    def test_reads_checked_out_field(self):
        headers = {"checked_out": _TS}
        state = read_sync(headers)
        assert state.time == _NOW
        assert state.rev == ""

    def test_prefers_sync_block_over_synced(self):
        headers = {"sync": {"time": _TS, "rev": "r2"}, "synced": "2020-01-01T00:00:00Z"}
        state = read_sync(headers)
        assert state.rev == "r2"

    def test_synced_takes_priority_over_checked_out(self):
        headers = {"synced": _TS, "checked_out": "2020-01-01T00:00:00Z"}
        state = read_sync(headers)
        assert state.time == _NOW


# ---------------------------------------------------------------------------
# read_sync — missing state
# ---------------------------------------------------------------------------


class TestReadSyncMissing:
    def test_empty_headers(self):
        state = read_sync({})
        assert state.time is None
        assert state.rev == ""

    def test_unrelated_headers(self):
        state = read_sync({"type": "form", "id": "x"})
        assert state.time is None


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


class TestIsStale:
    def test_never_synced_is_stale(self):
        assert is_stale(SyncState(time=None)) is True

    def test_fresh_is_not_stale(self):
        now = datetime.now(timezone.utc)
        state = SyncState(time=now - timedelta(minutes=30))
        assert is_stale(state) is False

    def test_just_inside_boundary_is_not_stale(self):
        now = datetime.now(timezone.utc)
        state = SyncState(time=now - timedelta(hours=1) + timedelta(seconds=10))
        assert is_stale(state) is False

    def test_one_second_over_boundary_is_stale(self):
        now = datetime.now(timezone.utc)
        state = SyncState(time=now - timedelta(hours=1, seconds=1))
        assert is_stale(state) is True

    def test_custom_max_age(self):
        now = datetime.now(timezone.utc)
        state = SyncState(time=now - timedelta(minutes=10))
        assert is_stale(state, max_age=timedelta(minutes=5)) is True
        assert is_stale(state, max_age=timedelta(minutes=15)) is False


# ---------------------------------------------------------------------------
# format_stale_warning
# ---------------------------------------------------------------------------


class TestFormatStaleWarning:
    def test_never_synced(self):
        msg = format_stale_warning(SyncState(time=None), "events/2026-07.md")
        assert "never synced" in msg
        assert "events/2026-07.md" in msg

    def test_shows_minutes(self):
        now = datetime.now(timezone.utc)
        state = SyncState(time=now - timedelta(minutes=45))
        msg = format_stale_warning(state, "foo.md")
        assert "45m" in msg
        assert "foo.md" in msg

    def test_shows_hours(self):
        now = datetime.now(timezone.utc)
        state = SyncState(time=now - timedelta(hours=2))
        msg = format_stale_warning(state, "bar.md")
        assert "2h" in msg

    def test_shows_hours_and_minutes(self):
        now = datetime.now(timezone.utc)
        state = SyncState(time=now - timedelta(hours=1, minutes=30))
        msg = format_stale_warning(state, "baz.md")
        assert "1h30m" in msg

    def test_includes_warning_prefix(self):
        state = SyncState(time=None)
        msg = format_stale_warning(state, "x.md")
        assert msg.startswith("warning:")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def freezegun_utc(monkeypatch):
    """Monkeypatch datetime.now in syncstate to return a fixed time."""
    import gax.syncstate as ss

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW

    monkeypatch.setattr(ss, "datetime", _FakeDatetime)
    return _NOW
