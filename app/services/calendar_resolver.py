"""Calendar resolver — builds concrete ``CalendarAdapter`` instances from DB.

The agent never constructs provider-specific adapters directly. Given a couple
and a database session, this resolver loads the active ``calendar_connections``
for both partners and builds the matching ``GoogleCalendarAdapter`` /
``CalDAVAdapter``, returning them alongside the user metadata (phone, tz) the
agent needs for downstream display and SMS delivery.

This is the seam between the deterministic adapter layer (Phase 1) and the
LangGraph agent (Phase 3). See ``ldr-date-devplan.md`` §6.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import CalDAVAdapter, CalendarAdapter, GoogleCalendarAdapter
from app.models import CalendarConnection, CalendarProvider, ConnectionStatus
from app.services.couple_store import CoupleStore


@dataclass
class ResolvedCalendar:
    """A calendar adapter for one partner, plus the metadata the agent needs.

    - ``user_id`` / ``phone_number`` / ``timezone`` come from the owning user.
    - ``adapter`` is the provider-specific adapter behind the common interface.
    """

    user_id: int
    phone_number: str
    timezone: str
    adapter: CalendarAdapter


class CalendarResolver:
    """Builds ``ResolvedCalendar`` objects for a couple's active connections."""

    async def get_active_adapters(
        self, db: AsyncSession, couple_id: int
    ) -> list[ResolvedCalendar]:
        """Return a ``ResolvedCalendar`` per *active* calendar connection for
        a couple.

        Partners without an active connection simply contribute no adapter —
        the caller decides how to treat that (see ``fetch_availability``).
        """
        couple = await CoupleStore(db).get_couple(couple_id)
        if couple is None:
            return []

        partner_ids = {couple.partner_a_user_id, couple.partner_b_user_id}

        result = await db.execute(
            select(CalendarConnection).where(
                CalendarConnection.user_id.in_(partner_ids),
                CalendarConnection.status == ConnectionStatus.active,
            )
        )
        connections = list(result.scalars().all())

        resolved: list[ResolvedCalendar] = []
        couples_store = CoupleStore(db)
        for conn in connections:
            user = await couples_store.get_user(conn.user_id)
            if user is None:
                continue
            adapter = self._build_adapter(conn)
            resolved.append(
                ResolvedCalendar(
                    user_id=user.id,
                    phone_number=user.phone_number,
                    timezone=user.timezone,
                    adapter=adapter,
                )
            )
        return resolved

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_adapter(self, conn: CalendarConnection) -> CalendarAdapter:
        """Instantiate the provider-specific adapter for a connection."""
        if conn.provider == CalendarProvider.google:
            if not conn.oauth_token:
                raise ValueError(
                    f"Google calendar connection {conn.id} has no oauth_token"
                )
            return GoogleCalendarAdapter(
                oauth_token_json=conn.oauth_token,
                refresh_token=conn.refresh_token,
            )

        if conn.provider == CalendarProvider.caldav:
            if not conn.caldav_credentials:
                raise ValueError(
                    f"CalDAV connection {conn.id} has no caldav_credentials"
                )
            creds = json.loads(conn.caldav_credentials)
            return CalDAVAdapter(
                url=creds["url"],
                username=creds["username"],
                password=creds["password"],
                calendar_name=creds.get("calendar_name", "Home"),
            )

        raise ValueError(f"Unknown calendar provider: {conn.provider}")

    async def persist_tokens(
        self, db: AsyncSession, resolved: list[ResolvedCalendar]
    ) -> None:
        """Persist any refreshed OAuth tokens back to the database.

        Call this *after* using the adapters returned by
        ``get_active_adapters()``.  Google tokens are refreshed in-memory
        when the access token expires; this method writes them back to
        the ``calendar_connections`` table so the refresh is not lost on
        restart.

        CalDAV adapters have no token to persist (password-based auth).
        """
        for rc in resolved:
            adapter = rc.adapter
            if not isinstance(adapter, GoogleCalendarAdapter):
                continue

            refreshed_json = adapter.token_json
            # Only persist if the token actually changed (i.e. was refreshed).
            # We check against the DB value to avoid unnecessary writes.
            result = await db.execute(
                select(CalendarConnection.oauth_token).where(
                    CalendarConnection.user_id == rc.user_id,
                    CalendarConnection.provider == CalendarProvider.google,
                    CalendarConnection.status == ConnectionStatus.active,
                )
            )
            current_token = result.scalar_one_or_none()
            if current_token is None or current_token == refreshed_json:
                continue

            await db.execute(
                update(CalendarConnection)
                .where(
                    CalendarConnection.user_id == rc.user_id,
                    CalendarConnection.provider == CalendarProvider.google,
                )
                .values(oauth_token=refreshed_json)
            )