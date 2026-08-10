"""Couple / user store service — deterministic access to couples and users.

The agent nodes need to resolve phone numbers (for ``deliver_sms``), map an
inbound SMS sender phone to a user + couple (``classify_intent``), and flip
mute state (``route_stop``). This service is the single entry point for those
reads/writes; nodes never touch the ORM session directly.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Couple, User


class CoupleStore:
    """Deterministic couple/user store, scoped to a single database session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_user(self, user_id: int) -> User | None:
        """Fetch a user by id."""
        result = await self._session.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_user_by_phone(self, phone_number: str) -> User | None:
        """Fetch a user by their E.164 phone number (Twilio From field)."""
        result = await self._session.execute(
            select(User).where(User.phone_number == phone_number)
        )
        return result.scalar_one_or_none()

    async def get_couple(self, couple_id: int) -> Couple | None:
        """Fetch a couple by id."""
        result = await self._session.execute(
            select(Couple).where(Couple.id == couple_id)
        )
        return result.scalar_one_or_none()

    async def get_couple_for_user(self, user_id: int) -> Couple | None:
        """Find the (single) couple a user belongs to.

        Assumes one couple per user in v1. Returns the most recently created
        couple the user is a partner of.
        """
        result = await self._session.execute(
            select(Couple)
            .where(
                (Couple.partner_a_user_id == user_id)
                | (Couple.partner_b_user_id == user_id)
            )
            .order_by(Couple.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_other_partner(
        self, couple: Couple, user_id: int
    ) -> User | None:
        """Given a couple and one partner's id, return the *other* partner.

        Used by ``route_yes`` to notify the confirming user's partner.
        """
        other_id = None
        if couple.partner_a_user_id == user_id:
            other_id = couple.partner_b_user_id
        elif couple.partner_b_user_id == user_id:
            other_id = couple.partner_a_user_id
        if other_id is None:
            return None
        return await self.get_user(other_id)

    async def partner_users(self, couple: Couple) -> list[User]:
        """Return both partners of a couple as a list of User objects."""
        users: list[User] = []
        for uid in (couple.partner_a_user_id, couple.partner_b_user_id):
            user = await self.get_user(uid)
            if user is not None:
                users.append(user)
        return users

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def set_muted(self, couple_id: int, muted: bool) -> Couple | None:
        """Set a couple's ``suggestions_muted`` flag (STOP / re-enable)."""
        couple = await self.get_couple(couple_id)
        if couple is None:
            return None
        couple.suggestions_muted = muted
        await self._session.flush()
        return couple