"""Unit tests for calendar adapters (GoogleCalendarAdapter, CalDAVAdapter).

Tests are isolated via mocking — no real API calls are made.  They verify
that each adapter:
- Presents the correct async interface
- Returns expected result types (TimeRange list, event ID strings)
- Handles errors (auth failure, rate limit, not found, conflict)
- Refreshes tokens when expired (Google)
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters import CalDAVAdapter, GoogleCalendarAdapter, TimeRange


# =========================================================================
# Fixtures
# =========================================================================

SAMPLE_TOKEN_JSON = (
    '{"token": "ya29.fake", "refresh_token": "1//fake", '
    '"token_uri": "https://oauth2.googleapis.com/token", '
    '"client_id": "fake.apps.googleusercontent.com", '
    '"client_secret": "fake", "scopes": ["https://www.googleapis.com/auth/calendar"], '
    '"expiry": "2099-01-01T00:00:00Z"}'
)

SAMPLE_CREDENTIALS_JSON = (
    '{"url": "https://caldav.icloud.com/", '
    '"username": "user@icloud.com", '
    '"password": "fake-app-specific-pw"}'
)


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def utc_later() -> datetime:
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# =========================================================================
# GoogleCalendarAdapter
# =========================================================================


class TestGoogleCalendarAdapter:
    """Tests for GoogleCalendarAdapter — all Google API calls are mocked."""

    def test_init_with_valid_token(self):
        """Constructing with a valid token JSON should not raise."""
        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        assert adapter.token_json is not None

    @patch("app.adapters.google.build")
    @patch("app.adapters.google.Credentials.from_authorized_user_info")
    async def test_get_busy_blocks_returns_time_ranges(
        self, mock_creds, mock_build, utc_now, utc_later
    ):
        """get_busy_blocks should return a list of TimeRange objects."""
        # --- mock credentials ---
        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = True
        mock_creds_instance.expired = False
        mock_creds_instance.refresh_token = "fake_refresh"
        mock_creds.return_value = mock_creds_instance

        # --- mock the freebusy response ---
        mock_freebusy = MagicMock()
        mock_freebusy.query.return_value.execute.return_value = {
            "calendars": {
                "primary": {
                    "busy": [
                        {"start": "2026-06-01T10:30:00Z", "end": "2026-06-01T11:00:00Z"},
                        {"start": "2026-06-01T11:30:00Z", "end": "2026-06-01T12:00:00Z"},
                    ]
                }
            }
        }
        mock_service = MagicMock()
        mock_service.freebusy.return_value = mock_freebusy
        mock_build.return_value = mock_service

        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        blocks = await adapter.get_busy_blocks(utc_now, utc_later)

        assert len(blocks) == 2
        assert all(isinstance(b, TimeRange) for b in blocks)
        assert blocks[0].start == datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc)
        assert blocks[0].end == datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)

    @patch("app.adapters.google.build")
    @patch("app.adapters.google.Credentials.from_authorized_user_info")
    async def test_create_event_returns_event_id(
        self, mock_creds, mock_build, utc_now, utc_later
    ):
        """create_event should return the event ID from the API response."""
        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = True
        mock_creds_instance.expired = False
        mock_creds_instance.refresh_token = "fake_refresh"
        mock_creds.return_value = mock_creds_instance

        mock_insert = MagicMock()
        mock_insert.execute.return_value = {"id": "event_123"}
        mock_events = MagicMock()
        mock_events.insert.return_value = mock_insert
        mock_service = MagicMock()
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        event_id = await adapter.create_event(
            utc_now, utc_later, title="Test event", description="A test"
        )

        assert event_id == "event_123"
        mock_events.insert.assert_called_once()

    @patch("app.adapters.google.build")
    @patch("app.adapters.google.Credentials.from_authorized_user_info")
    async def test_update_event_calls_patch(
        self, mock_creds, mock_build, utc_now, utc_later
    ):
        """update_event should call events().patch() with the right body."""
        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = True
        mock_creds_instance.expired = False
        mock_creds_instance.refresh_token = "fake_refresh"
        mock_creds.return_value = mock_creds_instance

        mock_patch = MagicMock()
        mock_patch.execute.return_value = None
        mock_events = MagicMock()
        mock_events.patch.return_value = mock_patch
        mock_service = MagicMock()
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        result = await adapter.update_event(
            "event_123",
            {"summary": "Updated title", "description": "Updated desc"},
        )

        assert result == "event_123"
        mock_events.patch.assert_called_once_with(
            calendarId="primary",
            eventId="event_123",
            body={"summary": "Updated title", "description": "Updated desc"},
        )

    @patch("app.adapters.google.build")
    @patch("app.adapters.google.Credentials.from_authorized_user_info")
    async def test_token_refresh_when_expired(
        self, mock_creds, mock_build, utc_now, utc_later
    ):
        """Should call creds.refresh() when the token is expired."""
        import google.auth.transport.requests

        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = False
        mock_creds_instance.expired = True
        mock_creds_instance.refresh_token = "fake_refresh"
        mock_creds.return_value = mock_creds_instance

        mock_freebusy = MagicMock()
        mock_freebusy.query.return_value.execute.return_value = {
            "calendars": {"primary": {"busy": []}}
        }
        mock_service = MagicMock()
        mock_service.freebusy.return_value = mock_freebusy
        mock_build.return_value = mock_service

        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        await adapter.get_busy_blocks(utc_now, utc_later)

        mock_creds_instance.refresh.assert_called_once()

    @patch("app.adapters.google.build")
    @patch("app.adapters.google.Credentials.from_authorized_user_info")
    async def test_http_401_raises_permission_error(
        self, mock_creds, mock_build, utc_now, utc_later
    ):
        """HTTP 401 from the API should raise PermissionError."""
        from googleapiclient.errors import HttpError

        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = True
        mock_creds_instance.expired = False
        mock_creds_instance.refresh_token = "fake_refresh"
        mock_creds.return_value = mock_creds_instance

        resp = MagicMock()
        resp.status = 401
        mock_freebusy = MagicMock()
        mock_freebusy.query.return_value.execute.side_effect = HttpError(
            resp, b"Unauthorized"
        )
        mock_service = MagicMock()
        mock_service.freebusy.return_value = mock_freebusy
        mock_build.return_value = mock_service

        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        with pytest.raises(PermissionError, match="re-authentication"):
            await adapter.get_busy_blocks(utc_now, utc_later)

    @patch("app.adapters.google.build")
    @patch("app.adapters.google.Credentials.from_authorized_user_info")
    async def test_http_404_raises_lookup_error(
        self, mock_creds, mock_build, utc_now, utc_later
    ):
        """HTTP 404 should raise LookupError."""
        from googleapiclient.errors import HttpError

        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = True
        mock_creds_instance.expired = False
        mock_creds_instance.refresh_token = "fake_refresh"
        mock_creds.return_value = mock_creds_instance

        resp = MagicMock()
        resp.status = 404
        mock_freebusy = MagicMock()
        mock_freebusy.query.return_value.execute.side_effect = HttpError(
            resp, b"Not Found"
        )
        mock_service = MagicMock()
        mock_service.freebusy.return_value = mock_freebusy
        mock_build.return_value = mock_service

        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        with pytest.raises(LookupError, match="not found"):
            await adapter.get_busy_blocks(utc_now, utc_later)

    @patch("app.adapters.google.build")
    @patch("app.adapters.google.Credentials.from_authorized_user_info")
    async def test_http_409_raises_value_error(
        self, mock_creds, mock_build, utc_now, utc_later
    ):
        """HTTP 409 should raise ValueError."""
        from googleapiclient.errors import HttpError

        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = True
        mock_creds_instance.expired = False
        mock_creds_instance.refresh_token = "fake_refresh"
        mock_creds.return_value = mock_creds_instance

        resp = MagicMock()
        resp.status = 409
        mock_insert = MagicMock()
        mock_insert.execute.side_effect = HttpError(resp, b"Conflict")
        mock_events = MagicMock()
        mock_events.insert.return_value = mock_insert
        mock_service = MagicMock()
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        with pytest.raises(ValueError, match="event ID conflict"):
            await adapter.create_event(
                utc_now, utc_later, title="Test"
            )

    @patch("app.adapters.google.build")
    @patch("app.adapters.google.Credentials.from_authorized_user_info")
    async def test_http_429_raises_runtime_error(
        self, mock_creds, mock_build, utc_now, utc_later
    ):
        """HTTP 429 should raise RuntimeError (rate limit)."""
        from googleapiclient.errors import HttpError

        mock_creds_instance = MagicMock()
        mock_creds_instance.valid = True
        mock_creds_instance.expired = False
        mock_creds_instance.refresh_token = "fake_refresh"
        mock_creds.return_value = mock_creds_instance

        resp = MagicMock()
        resp.status = 429
        mock_freebusy = MagicMock()
        mock_freebusy.query.return_value.execute.side_effect = HttpError(
            resp, b"Rate limit"
        )
        mock_service = MagicMock()
        mock_service.freebusy.return_value = mock_freebusy
        mock_build.return_value = mock_service

        adapter = GoogleCalendarAdapter(SAMPLE_TOKEN_JSON)
        with pytest.raises(RuntimeError, match="rate limit"):
            await adapter.get_busy_blocks(utc_now, utc_later)


# =========================================================================
# CalDAVAdapter
# =========================================================================


class TestCalDAVAdapter:
    """Tests for CalDAVAdapter — all CalDAV server calls are mocked."""

    @patch("app.adapters.caldav.AsyncDAVClient")
    async def test_get_busy_blocks_returns_time_ranges(
        self, mock_client_cls, utc_now, utc_later
    ):
        """get_busy_blocks should return TimeRange objects from search results."""
        # --- Build a mock event component (VEVENT, not VCALENDAR wrapper) ---
        import icalendar

        def _make_vevent(dtstart, dtend):
            event = icalendar.Event()
            event.add("dtstart", dtstart)
            event.add("dtend", dtend)
            event.add("summary", "Busy period")
            return event

        mock_event_a = MagicMock()
        mock_event_a.get_icalendar_component.return_value = _make_vevent(
            datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        mock_event_b = MagicMock()
        mock_event_b.get_icalendar_component.return_value = _make_vevent(
            datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc),
        )

        # --- Mock the client chain ---
        mock_client = AsyncMock()
        mock_principal = AsyncMock()
        mock_calendar = AsyncMock()
        mock_calendar.name = "Home"
        mock_calendar.search = AsyncMock(return_value=[mock_event_a, mock_event_b])
        mock_client.get_principal.return_value = mock_principal
        mock_client.get_calendars.return_value = [mock_calendar]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        adapter = CalDAVAdapter(
            url="https://caldav.icloud.com/",
            username="test@icloud.com",
            password="fake-app-specific-pw",
            calendar_name="Home",
        )
        blocks = await adapter.get_busy_blocks(utc_now, utc_later)

        assert len(blocks) == 2
        assert all(isinstance(b, TimeRange) for b in blocks)
        assert blocks[0].start == datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc)
        assert blocks[0].end == datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    @patch("app.adapters.caldav.AsyncDAVClient")
    async def test_create_event_returns_uid(
        self, mock_client_cls, utc_now, utc_later
    ):
        """create_event should generate a UID and call add_event."""
        mock_event = MagicMock()

        mock_client = AsyncMock()
        mock_principal = AsyncMock()
        mock_calendar = AsyncMock()
        mock_calendar.name = "Home"
        mock_calendar.add_event = AsyncMock(return_value=mock_event)
        mock_client.get_principal.return_value = mock_principal
        mock_client.get_calendars.return_value = [mock_calendar]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        adapter = CalDAVAdapter(
            url="https://caldav.icloud.com/",
            username="test@icloud.com",
            password="fake-app-specific-pw",
            calendar_name="Home",
        )
        event_id = await adapter.create_event(
            utc_now, utc_later, title="Date night", description="Dinner"
        )

        # Should return the generated UID
        assert isinstance(event_id, str)
        assert len(event_id) == 32  # UUID hex

        # Verify add_event was called with the right args
        mock_calendar.add_event.assert_called_once()
        call_kwargs = mock_calendar.add_event.call_args[1]
        assert call_kwargs["summary"] == "Date night"
        assert call_kwargs["description"] == "Dinner"
        assert call_kwargs["uid"] == event_id

    @patch("app.adapters.caldav.AsyncDAVClient")
    async def test_update_event_calls_save(
        self, mock_client_cls, utc_now, utc_later
    ):
        """update_event should modify the ical component and call save()."""
        import icalendar

        # Build a mock event component (VEVENT, not wrapped in VCALENDAR)
        import icalendar

        source_vevent = icalendar.Event()
        source_vevent.add("dtstart", utc_now)
        source_vevent.add("dtend", utc_later)
        source_vevent.add("summary", "Original title")

        mock_event_obj = MagicMock()
        mock_event_obj.get_icalendar_component.return_value = source_vevent
        mock_event_obj.save = AsyncMock()

        mock_client = AsyncMock()
        mock_principal = AsyncMock()
        mock_calendar = AsyncMock()
        mock_calendar.name = "Home"
        mock_calendar.get_event_by_uid = AsyncMock(return_value=mock_event_obj)
        mock_client.get_principal.return_value = mock_principal
        mock_client.get_calendars.return_value = [mock_calendar]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        adapter = CalDAVAdapter(
            url="https://caldav.icloud.com/",
            username="test@icloud.com",
            password="fake-app-specific-pw",
        )
        result = await adapter.update_event(
            "my-event-uid",
            {"summary": "Updated title", "description": "New desc"},
        )

        assert result == "my-event-uid"

        # Verify the ical component was modified before save
        updated_summary = source_vevent.get("summary")
        assert updated_summary == "Updated title"
        updated_desc = source_vevent.get("description")
        assert updated_desc == "New desc"

        # Verify save was called
        mock_event_obj.save.assert_called_once()

    @patch("app.adapters.caldav.AsyncDAVClient")
    async def test_get_busy_blocks_handles_all_day_events(
        self, mock_client_cls, utc_now, utc_later
    ):
        """All-day events (date objects) should be converted to datetime."""
        import icalendar
        from datetime import date

        vevent = icalendar.Event()
        vevent.add("dtstart", date(2026, 6, 1))
        vevent.add("dtend", date(2026, 6, 2))
        vevent.add("summary", "All-day")

        mock_event_obj = MagicMock()
        mock_event_obj.get_icalendar_component.return_value = vevent

        mock_client = AsyncMock()
        mock_principal = AsyncMock()
        mock_calendar = AsyncMock()
        mock_calendar.name = "Home"
        mock_calendar.search = AsyncMock(return_value=[mock_event_obj])
        mock_client.get_principal.return_value = mock_principal
        mock_client.get_calendars.return_value = [mock_calendar]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        adapter = CalDAVAdapter(
            url="https://caldav.icloud.com/",
            username="test@icloud.com",
            password="fake-app-specific-pw",
        )
        blocks = await adapter.get_busy_blocks(utc_now, utc_later)

        assert len(blocks) == 1
        assert isinstance(blocks[0].start, datetime)
        assert isinstance(blocks[0].end, datetime)

    @patch("app.adapters.caldav.AsyncDAVClient")
    async def test_fallback_to_first_calendar(
        self, mock_client_cls, utc_now, utc_later
    ):
        """If calendar_name doesn't match, fall back to the first calendar."""
        mock_client = AsyncMock()
        mock_principal = AsyncMock()
        mock_cal = AsyncMock()
        mock_cal.name = "Work"  # Different from "Home"
        mock_cal.search = AsyncMock(return_value=[])
        mock_client.get_principal.return_value = mock_principal
        mock_client.get_calendars.return_value = [mock_cal]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        adapter = CalDAVAdapter(
            url="https://caldav.icloud.com/",
            username="test@icloud.com",
            password="fake-app-specific-pw",
            calendar_name="NonExistent",  # Won't match "Work"
        )
        blocks = await adapter.get_busy_blocks(utc_now, utc_later)

        assert blocks == []  # No events found, but didn't crash

    @patch("app.adapters.caldav.AsyncDAVClient")
    async def test_empty_calendars_raises_lookup_error(
        self, mock_client_cls, utc_now, utc_later
    ):
        """If no calendars exist, should raise LookupError."""
        mock_client = AsyncMock()
        mock_principal = AsyncMock()
        mock_client.get_principal.return_value = mock_principal
        mock_client.get_calendars.return_value = []  # No calendars
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        adapter = CalDAVAdapter(
            url="https://caldav.icloud.com/",
            username="test@icloud.com",
            password="fake-app-specific-pw",
        )
        with pytest.raises(LookupError, match="No calendars found"):
            await adapter.get_busy_blocks(utc_now, utc_later)


# =========================================================================
# TimeRange  (basic value-object tests)
# =========================================================================


class TestTimeRange:
    """TimeRange is a simple dataclass — verify it holds values correctly."""

    def test_time_range_creation(self, utc_now, utc_later):
        tr = TimeRange(start=utc_now, end=utc_later)
        assert tr.start == utc_now
        assert tr.end == utc_later

    def test_time_range_repr(self, utc_now, utc_later):
        tr = TimeRange(start=utc_now, end=utc_later)
        r = repr(tr)
        assert "TimeRange" in r
        assert "start=" in r
        assert "end=" in r