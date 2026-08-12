"""Calendar connection helpers for the SMS onboarding flow.

Provides two functions used by ``onboarding_node``:

- ``connect_google(user_id, code)`` — validates Google OAuth token, stores
  ``CalendarConnection``.  (The OAuth flow itself is handled by the browser
  redirect; ``code`` is the authorization code from the callback.)
- ``connect_apple(user_id, email, password)`` — validates via a real PROPFIND
  against the iCloud CalDAV server, then stores ``CalendarConnection``.

Apple passwords are validated at entry time (not deferred) so typos are caught
immediately — the user re-texts a corrected password in the same turn, rather
than finding out days later on the first date-availability check.

See ``.pi/sms-auth.md`` § "Design Decisions" for the rationale.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.caldav import CalDAVAdapter
from app.models import CalendarConnection, CalendarProvider, ConnectionStatus

logger = logging.getLogger(__name__)


async def connect_apple(
    db: AsyncSession,
    user_id: int,
    email: str,
    password: str,
    calendar_name: str = "Home",
) -> tuple[bool, str]:
    """Validate Apple Calendar credentials via a real PROPFIND and persist.

    Args:
        db: Database session (must be in an active transaction).
        user_id: The user's id.
        email: Apple ID email address.
        password: App-specific password (``xxxx-xxxx-xxxx-xxxx`` format).
        calendar_name: Name of the iCloud calendar to use (default ``"Home"``).

    Returns:
        A tuple of ``(success, message)``.  On success, message is empty.
        On failure, ``message`` describes the error (e.g. "That password
        didn't work. Double-check it at appleid.apple.com and try again.").
    """
    adapter = CalDAVAdapter(
        url="https://caldav.icloud.com/",
        username=email,
        password=password,
        calendar_name=calendar_name,
    )

    try:
        # Validate by fetching busy blocks for the next 24 hours.
        now = datetime.now(timezone.utc)
        await adapter.get_busy_blocks(now, now + timedelta(days=1))
    except PermissionError:
        return False, (
            "That password didn't work. Double-check it at "
            "appleid.apple.com and try again."
        )
    except Exception as exc:
        logger.warning("Apple Calendar validation failed for user %d: %s", user_id, exc)
        return False, (
            "Couldn't connect to Apple Calendar right now. "
            "Please try again later."
        )

    # Persist the connection.
    creds = json.dumps({
        "url": "https://caldav.icloud.com/",
        "username": email,
        "password": password,
        "calendar_name": calendar_name,
    })

    result = await db.execute(
        select(CalendarConnection).where(
            CalendarConnection.user_id == user_id,
            CalendarConnection.provider == CalendarProvider.caldav,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.caldav_credentials = creds
        existing.status = ConnectionStatus.active
    else:
        conn = CalendarConnection(
            user_id=user_id,
            provider=CalendarProvider.caldav,
            caldav_credentials=creds,
            status=ConnectionStatus.active,
        )
        db.add(conn)

    await db.flush()
    return True, ""


async def connect_google(
    db: AsyncSession,
    user_id: int,
    oauth_token_json: str,
    refresh_token: str | None = None,
) -> None:
    """Store or update a Google Calendar connection after successful OAuth.

    The OAuth flow itself is handled by the browser redirect
    (``/auth/google?user_id=...``).  This function is called by the callback
    route after the token exchange succeeds.

    Args:
        db: Database session (must be in an active transaction).
        user_id: The user's id.
        oauth_token_json: The ``Credentials.to_json()`` string.
        refresh_token: The OAuth refresh token (may be ``None``).
    """
    result = await db.execute(
        select(CalendarConnection).where(
            CalendarConnection.user_id == user_id,
            CalendarConnection.provider == CalendarProvider.google,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.oauth_token = oauth_token_json
        existing.refresh_token = refresh_token
        existing.status = ConnectionStatus.active
    else:
        conn = CalendarConnection(
            user_id=user_id,
            provider=CalendarProvider.google,
            oauth_token=oauth_token_json,
            refresh_token=refresh_token,
            status=ConnectionStatus.active,
        )
        db.add(conn)

    await db.flush()