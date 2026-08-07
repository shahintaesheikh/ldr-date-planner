"""CalDAV / iCloud calendar adapter (async).

Implements the ``CalendarAdapter`` protocol for any CalDAV server — primarily
Apple iCloud — using the ``caldav`` library's ``AsyncDAVClient``.

Auth
----
iCloud requires an **app-specific password**, not the user's normal Apple ID
password (see the skill doc at ``.pi/skills/caldav-icloud.md`` for setup
instructions).

Credentials (URL, username, app-specific password) are stored in the
``calendar_connections.caldav_credentials`` column as a JSON blob and
retrieved at runtime.

Usage
-----
.. code-block:: python

    from app.adapters.caldav import CalDAVAdapter

    adapter = CalDAVAdapter(
        url="https://caldav.icloud.com/",
        username="user@icloud.com",
        password="xxxx-xxxx-xxxx-xxxx",  # app-specific password
        calendar_name="Home",            # optional, defaults to "Home"
    )

    # Read
    busy = await adapter.get_busy_blocks(start, end)

    # Write
    event_id = await adapter.create_event(start, end, title="Date night")

    # Update
    await adapter.update_event(event_id, {"summary": "Date night (rescheduled)"})
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from caldav import Calendar  # for type checks only
from caldav.async_davclient import AsyncDAVClient
from caldav.lib import error as caldav_error

from app.adapters.protocol import CalendarAdapter, TimeRange


class CalDAVAdapter(CalendarAdapter):
    """Adapter for CalDAV servers (Apple iCloud, etc.).

    Parameters
    ----------
    url:
        CalDAV server root URL (e.g. ``https://caldav.icloud.com/``).
    username:
        Apple ID email address (for iCloud) or CalDAV username.
    password:
        App-specific password (for iCloud) or CalDAV password.
    calendar_name:
        Name of the calendar to operate on (e.g. ``"Home"``, ``"Work"``).
        Defaults to ``"Home"``.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        calendar_name: str = "Home",
    ) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._calendar_name = calendar_name

    # ------------------------------------------------------------------
    # CalendarAdapter protocol (async)
    # ------------------------------------------------------------------

    async def get_busy_blocks(self, start: datetime, end: datetime) -> list[TimeRange]:
        """Fetch busy blocks by searching for events in the time range.

        CalDAV has no dedicated freebusy endpoint — we use
        ``cal.search(event=True, start=..., end=..., expand=True)`` and treat
        every event as a busy block.
        """
        events = await self._run(
            lambda cal: cal.search(
                event=True,
                start=start.date() if isinstance(start, datetime) else start,
                end=end.date() if isinstance(end, datetime) else end,
                expand=True,
            )
        )

        blocks: list[TimeRange] = []
        for event in events:
            try:
                ical = event.get_icalendar_component()
            except Exception:
                # Skip events we can't parse
                continue

            # VTIMEZONE and other non-event components have no DTSTART
            dtstart_raw = ical.get("dtstart")
            dtend_raw = ical.get("dtend")
            if dtstart_raw is None or dtend_raw is None:
                continue

            dtstart = dtstart_raw.dt
            dtend = dtend_raw.dt

            # All-day events come as ``date`` objects; convert to ``datetime``
            # for uniform ``TimeRange`` representation.
            if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
                dtstart = datetime.combine(dtstart, datetime.min.time()).replace(
                    tzinfo=None
                )
            if isinstance(dtend, date) and not isinstance(dtend, datetime):
                dtend = datetime.combine(dtend, datetime.min.time()).replace(
                    tzinfo=None
                )

            blocks.append(
                TimeRange(start=_ensure_tz(dtstart), end=_ensure_tz(dtend))
            )

        return blocks

    async def create_event(
        self,
        start: datetime,
        end: datetime,
        title: str,
        description: str | None = None,
    ) -> str:
        """Create a new calendar event.

        We generate our own UID (v4 UUID, hex) so the event can be reliably
        found for later updates — see the CalDAV skill's "UIDs are yours
        to manage" gotcha.
        """
        event_uid = uuid.uuid4().hex

        kwargs: dict = {
            "dtstart": start,
            "dtend": end,
            "summary": title,
            "uid": event_uid,
        }
        if description:
            kwargs["description"] = description

        await self._run(lambda cal: cal.add_event(**kwargs))

        return event_uid

    async def update_event(self, event_id: str, changes: dict) -> str:
        """Partially update an existing event by UID.

        Strategy
        --------
        1. Fetch the event via ``cal.get_event_by_uid(event_id)`` (async).
        2. Modify the iCalendar component in-memory (sync, no network call).
        3. Set the modified raw data back on the event.
        4. Call ``event.save()`` (async — pushes an HTTP PUT to the server).

        We avoid ``edit_icalendar_component()`` here — it uses a sync context
        manager whose ``__exit__`` would not properly await the async
        ``save()`` coroutine.
        """
        async with self._session() as client:
            cal = await self._resolve_calendar(client)
            event = await cal.get_event_by_uid(event_id)

            # Get the icalendar component (sync, in-memory)
            ical = event.get_icalendar_component()

            # Apply changes
            if "summary" in changes:
                ical["SUMMARY"] = changes["summary"]
            if "description" in changes:
                ical["DESCRIPTION"] = changes["description"]
            if "start" in changes:
                ical["DTSTART"].dt = changes["start"]
            if "end" in changes:
                ical["DTEND"].dt = changes["end"]

            # Replace the raw data and push to the server
            raw = ical.to_ical()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            event.data = raw
            await event.save()

        return event_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _session(self):
        """Return an async context manager for a fresh ``AsyncDAVClient``.

        The client is opened and closed per-call.  For the agent's
        ``fetch_availability`` node this is fine — the overhead of a single
        PROPFIND per connection is negligible compared to the subsequent
        REPORT body.
        """
        return AsyncDAVClient(
            url=self._url,
            username=self._username,
            password=self._password,
        )

    async def _resolve_calendar(self, client: AsyncDAVClient) -> Calendar:
        """Resolve the principal and locate the target calendar."""
        principal = await client.get_principal()
        calendars = await client.get_calendars(principal)
        return self._find_calendar(calendars)

    async def _run(self, fn):
        """Open a session, resolve the calendar, and call
        ``fn(calendar)`` within it.
        """
        async with self._session() as client:
            cal = await self._resolve_calendar(client)
            return await fn(cal)

    def _find_calendar(self, calendars: list) -> Calendar:
        """Locate the target calendar by name, falling back to the first
        calendar if none match.
        """
        for cal in calendars:
            if cal.name == self._calendar_name:
                return cal
        if calendars:
            return calendars[0]
        raise LookupError(
            f"No calendars found on the CalDAV server "
            f"(searched for '{self._calendar_name}')"
        )


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _ensure_tz(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware; assume UTC if naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=__import__("datetime").timezone.utc)
    return dt


def _raise_caldav_error(exc: Exception) -> None:
    """Translate common CalDAV errors into the same Python exception types
    used by ``GoogleCalendarAdapter`` so callers handle errors uniformly.

    Mapping
    -------
    +------------------------------------+-------------------+
    | caldav exception                   | Python exception  |
    +====================================+===================+
    | AuthorizationError                 | PermissionError   |
    | NotFoundError                      | LookupError       |
    | ConflictError / DAVError(409)      | ValueError        |
    | DAVError(403|429)                  | RuntimeError      |
    | RequestError / any other DAVError  | RuntimeError      |
    +------------------------------------+-------------------+
    """
    if isinstance(exc, caldav_error.AuthorizationError):
        raise PermissionError(
            f"CalDAV authentication failed — check credentials: {exc}"
        ) from exc
    if isinstance(exc, caldav_error.NotFoundError):
        raise LookupError(f"CalDAV resource not found: {exc}") from exc
    if isinstance(exc, caldav_error.DAVError):
        status = getattr(exc, "status", None) or getattr(exc, "reason", None)
        if status and status in (403, 429):
            raise RuntimeError(
                f"CalDAV rate limit exceeded (HTTP {status}) — retry with backoff"
            ) from exc
        if status == 409:
            raise ValueError(f"CalDAV event ID conflict: {exc}") from exc
        raise RuntimeError(f"CalDAV error: {exc}") from exc

    raise RuntimeError(f"CalDAV unexpected error: {exc}") from exc