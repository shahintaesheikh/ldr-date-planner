"""Calendar adapter protocol — common async interface for all calendar providers.

Every calendar provider (Google, CalDAV/Apple) implements this interface so
the rest of the system (LangGraph agent, FastAPI helpers) never touches
provider-specific logic.

See ldr-date-devplan.md §6 for the design rationale.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class TimeRange:
    """A time range with start and end datetimes (both timezone-aware)."""

    start: datetime
    end: datetime


class CalendarAdapter:
    """Common async interface for calendar provider adapters.

    Subclasses must implement all methods.  The agent graph nodes call these
    through the abstract interface — never through a provider-specific import.
    """

    async def get_busy_blocks(self, start: datetime, end: datetime) -> list[TimeRange]:
        """Fetch busy/free blocks from the calendar within [start, end).

        Returns a list of *busy* periods (i.e. times the user is unavailable).
        Free times are the inverse of these blocks within the query range.

        All datetimes are timezone-aware (UTC).
        """
        raise NotImplementedError

    async def create_event(
        self,
        start: datetime,
        end: datetime,
        title: str,
        description: str | None = None,
    ) -> str:
        """Create a new calendar event.

        Returns the provider-specific event ID (used for later updates / deletes).
        """
        raise NotImplementedError

    async def update_event(self, event_id: str, changes: dict) -> str:
        """Partially update an existing event.

        *changes* is a dict of fields to update (PATCH semantics — only
        provided fields are changed).  Supported keys:

        - ``summary`` (str)       — new title
        - ``description`` (str)   — new description
        - ``start`` (datetime)    — new start time
        - ``end`` (datetime)      — new end time

        Returns the event ID (unchanged).
        """
        raise NotImplementedError