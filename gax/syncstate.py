"""Shared syncstate utilities: read/write sync headers and staleness checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional


_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
_DEFAULT_MAX_AGE = timedelta(hours=1)


@dataclass
class SyncState:
    time: Optional[datetime]  # UTC, or None if never synced
    rev: str = ""             # opaque revision string (e.g. etag, headRevisionId)


def write_sync_header(headers: dict, rev: str = "") -> dict:
    """Return a copy of *headers* with a fresh ``sync`` block written in.

    The block has the form::

        sync:
          time: 2026-07-26T12:00:00Z
          rev: <rev>

    ``rev`` is omitted from the block when empty.
    """
    now = datetime.now(timezone.utc).strftime(_ISO_FMT)
    sync_block: dict = {"time": now}
    if rev:
        sync_block["rev"] = rev
    return {**headers, "sync": sync_block}


def read_sync(headers: dict) -> SyncState:
    """Parse sync state from *headers*, accepting both new and legacy formats.

    New format (preferred)::

        sync: {time: "...", rev: "..."}

    Legacy fallbacks (time-only, no rev):

    * ``synced: "..."``
    * ``checked_out: "..."``

    Returns a :class:`SyncState` with ``time=None`` when no sync info is
    present.
    """
    sync = headers.get("sync")
    if isinstance(sync, dict):
        raw_time = sync.get("time", "")
        rev = sync.get("rev", "")
        return SyncState(time=_parse_iso(raw_time), rev=rev)

    # Legacy: plain string fields, time only
    for field in ("synced", "checked_out"):
        raw = headers.get(field)
        if raw:
            return SyncState(time=_parse_iso(raw), rev="")

    return SyncState(time=None, rev="")


def is_stale(state: SyncState, max_age: timedelta = _DEFAULT_MAX_AGE) -> bool:
    """Return True if *state* is older than *max_age* or was never synced."""
    if state.time is None:
        return True
    age = datetime.now(timezone.utc) - state.time
    return age > max_age


def rev_guard(state: SyncState, fetch_rev: Callable[[], str]) -> tuple[bool, str]:
    """Check whether the remote revision has moved since *state* was written.

    Args:
        state:     The :class:`SyncState` read from the local file header.
        fetch_rev: Zero-argument callable that returns the *current* remote
                   revision string (e.g. Drive ``modifiedTime``, Gmail
                   ``historyId``).  Called only when ``state.rev`` is set.

    Returns:
        ``(changed, remote_rev)`` — *changed* is True when the remote rev
        differs from ``state.rev``; *remote_rev* is the value returned by
        *fetch_rev* (empty string when not applicable).
    """
    if not state.rev:
        return False, ""
    remote_rev = fetch_rev()
    return remote_rev != state.rev, remote_rev


def format_stale_warning(state: SyncState, path: str) -> str:
    """Return a human-readable staleness warning string."""
    if state.time is None:
        return f"warning: {path}: never synced"
    age = datetime.now(timezone.utc) - state.time
    minutes = int(age.total_seconds() // 60)
    if minutes < 60:
        age_str = f"{minutes}m"
    else:
        hours = minutes // 60
        rem = minutes % 60
        age_str = f"{hours}h{rem}m" if rem else f"{hours}h"
    return f"warning: {path}: last synced {age_str} ago (>{_DEFAULT_MAX_AGE})"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_iso(raw: str) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp string, returning None on failure."""
    if not raw:
        return None
    # Handle both "Z" suffix and "+00:00" offset
    normalized = raw.strip().replace("+00:00", "Z")
    for fmt in (_ISO_FMT, "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
