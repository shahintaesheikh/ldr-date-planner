"""Google Calendar API adapter (async).

Implements the ``CalendarAdapter`` protocol for Google Calendar v3 using
``google-api-python-client``.

The Google API client is synchronous under the hood.  Every network call is
dispatched to a thread pool via ``asyncio.to_thread()`` so the adapter
presents a clean async interface — safe for use in concurrent agent
execution.

Auth
----
The adapter is instantiated with an existing OAuth 2.0 token (JSON) and an
optional refresh token, both previously stored in the database by the OAuth
flow (``/auth/google`` → ``/auth/google/callback``).  Token refresh happens
automatically via ``google-auth`` when the access token expires.

Usage
-----
.. code-block:: python

    from app.adapters.google import GoogleCalendarAdapter

    adapter = GoogleCalendarAdapter(
        oauth_token_json=conn.oauth_token,
        refresh_token=conn.refresh_token,
    )

    # Read
    busy = await adapter.get_busy_blocks(start, end)

    # Write
    event_id = await adapter.create_event(start, end, title="Date night")

    # Update
    await adapter.update_event(event_id, {"summary": "Date night (rescheduled)"})
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.adapters.protocol import CalendarAdapter, TimeRange

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleCalendarAdapter(CalendarAdapter):
    """Adapter for Google Calendar API v3 (async wrapper).

    Parameters
    ----------
    oauth_token_json:
        JSON string from ``Credentials.to_json()``, previously stored in the
        ``calendar_connections.oauth_token`` column.
    refresh_token:
        The OAuth refresh token, stored in the
        ``calendar_connections.refresh_token`` column.  May be ``None`` for
        short-lived tokens (though Google always issues a refresh token when
        ``access_type="offline"`` is used).
    """

    def __init__(
        self,
        oauth_token_json: str,
        refresh_token: str | None = None,
    ) -> None:
        info = json.loads(oauth_token_json)
        self._creds = Credentials.from_authorized_user_info(info, SCOPES)

        # The stored JSON may not include the refresh_token (it's a separate
        # column in our schema), so inject it if we have one.
        if refresh_token and not self._creds.refresh_token:
            self._creds.refresh_token = refresh_token

        # Build is synchronous but only loads the discovery document (cached
        # locally after first request) — no network call to build the object.
        self._service = build("calendar", "v3", credentials=self._creds)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def token_json(self) -> str:
        """Current credentials as JSON (may have been refreshed).

        Callers should persist this back to the database after operations
        that might have triggered a token refresh.
        """
        return self._creds.to_json()

    # ------------------------------------------------------------------
    # CalendarAdapter protocol (async)
    # ------------------------------------------------------------------

    async def get_busy_blocks(self, start: datetime, end: datetime) -> list[TimeRange]:
        """Fetch busy blocks using the freebusy query endpoint.

        This is more efficient than listing all events and filtering — it
        returns only the time ranges where the user is busy, not the full
        event objects.
        """
        await self._refresh_if_expired()

        try:
            body = {
                "timeMin": _iso(start),
                "timeMax": _iso(end),
                "items": [{"id": "primary"}],
            }
            resp = await asyncio.to_thread(
                lambda: self._service.freebusy().query(body=body).execute()
            )
        except HttpError as exc:
            _raise_adapter_error(exc)

        busy = (
            resp.get("calendars", {})
            .get("primary", {})
            .get("busy", [])
        )

        return [
            TimeRange(start=_parse_iso(b["start"]), end=_parse_iso(b["end"]))
            for b in busy
        ]

    async def create_event(
        self,
        start: datetime,
        end: datetime,
        title: str,
        description: str | None = None,
    ) -> str:
        await self._refresh_if_expired()

        event: dict = {
            "summary": title,
            "start": {"dateTime": _iso(start), "timeZone": "UTC"},
            "end": {"dateTime": _iso(end), "timeZone": "UTC"},
        }
        if description:
            event["description"] = description

        try:
            created = await asyncio.to_thread(
                lambda: self._service.events()
                .insert(calendarId="primary", body=event)
                .execute()
            )
        except HttpError as exc:
            _raise_adapter_error(exc)

        return created["id"]

    async def update_event(self, event_id: str, changes: dict) -> str:
        await self._refresh_if_expired()

        body: dict = {}
        if "summary" in changes:
            body["summary"] = changes["summary"]
        if "description" in changes:
            body["description"] = changes["description"]
        if "start" in changes:
            body["start"] = {
                "dateTime": _iso(changes["start"]),
                "timeZone": "UTC",
            }
        if "end" in changes:
            body["end"] = {
                "dateTime": _iso(changes["end"]),
                "timeZone": "UTC",
            }

        try:
            await asyncio.to_thread(
                lambda: self._service.events()
                .patch(calendarId="primary", eventId=event_id, body=body)
                .execute()
            )
        except HttpError as exc:
            _raise_adapter_error(exc)

        return event_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh_if_expired(self) -> None:
        """Refresh the access token if it has expired and a refresh token is available."""
        if (
            self._creds
            and not self._creds.valid
            and self._creds.expired
            and self._creds.refresh_token
        ):
            await asyncio.to_thread(lambda: self._creds.refresh(Request()))


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """Format a datetime as RFC 3339 (ISO 8601) with timezone info."""
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    """Parse an RFC 3339 / ISO 8601 string back to a timezone-aware datetime."""
    # Python 3.11+ handles both 'Z' suffix and '+00:00' offset.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _raise_adapter_error(exc: HttpError) -> None:
    """Translate common Google API HTTP errors into meaningful exceptions."""
    status = exc.resp.status

    if status == 401:
        raise PermissionError(
            "Google Calendar token expired or invalid — re-authentication required"
        ) from exc
    if status == 403 or status == 429:
        raise RuntimeError(
            f"Google Calendar rate limit exceeded (HTTP {status}) — retry with backoff"
        ) from exc
    if status == 404:
        raise LookupError(
            "Google Calendar resource not found (HTTP 404)"
        ) from exc
    if status == 409:
        raise ValueError(
            "Google Calendar event ID conflict — use a different event ID"
        ) from exc
    if status == 410:
        raise RuntimeError(
            "Google Calendar sync token expired — full resync required"
        ) from exc

    # Re-raise anything else as-is
    raise RuntimeError(f"Google Calendar API error (HTTP {status}): {exc}") from exc