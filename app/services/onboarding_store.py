"""Onboarding session store — CRUD for the ``onboarding_sessions`` table.

The SMS-native onboarding flow stores transient session state (step, name,
partner phone, etc.) in this table while the user progresses through the
onboarding phases.  ``complete`` sessions remain in the DB for audit (a
background job can purge ``updated_at < now - 30 days`` rows).

See ``.pi/sms-auth.md`` for the full onboarding conversation design.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OnboardingSession, OnboardingStep


class OnboardingStore:
    """Deterministic onboarding session store, scoped to a single database session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_by_phone(self, phone_number: str) -> OnboardingSession | None:
        """Fetch the onboarding session for a given phone number.

        Returns ``None`` if no session exists for this phone.
        """
        result = await self._session.execute(
            select(OnboardingSession).where(
                OnboardingSession.phone_number == phone_number
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, session_id: int) -> OnboardingSession | None:
        """Fetch an onboarding session by its primary key."""
        result = await self._session.execute(
            select(OnboardingSession).where(OnboardingSession.id == session_id)
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def create(
        self,
        phone_number: str,
        step: OnboardingStep = OnboardingStep.await_name,
        data: dict | None = None,
    ) -> OnboardingSession:
        """Create a new onboarding session for *phone_number*.

        If a session already exists for this phone, it is returned unchanged
        (idempotent-create).  Use ``advance_step`` to update an existing
        session's step and data.
        """
        existing = await self.get_by_phone(phone_number)
        if existing is not None:
            return existing

        session = OnboardingSession(
            phone_number=phone_number,
            step=step,
            data=data or {},
        )
        self._session.add(session)
        await self._session.flush()
        return session

    async def advance_step(
        self,
        phone_number: str,
        next_step: OnboardingStep,
        data_updates: dict | None = None,
    ) -> OnboardingSession | None:
        """Advance the session to *next_step* and merge *data_updates* into ``data``.

        If *data_updates* is provided, its keys are merged into the existing
        ``data`` JSONB dict (shallow merge).  ``updated_at`` is automatically
        updated by the ORM column ``onupdate``.

        Returns ``None`` if no session exists for this phone.
        """
        session = await self.get_by_phone(phone_number)
        if session is None:
            return None

        session.step = next_step
        if data_updates:
            current = dict(session.data or {})
            current.update(data_updates)
            session.data = current
        session.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        return session

    async def delete(self, phone_number: str) -> bool:
        """Delete the onboarding session for *phone_number*.

        Returns ``True`` if a row was removed.
        """
        session = await self.get_by_phone(phone_number)
        if session is None:
            return False
        await self._session.delete(session)
        await self._session.flush()
        return True