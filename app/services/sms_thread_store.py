"""SMS thread store service — persistence for the ``sms_thread`` table.

Every inbound SMS reply is appended here, scoped to the proposal it amends.
This gives ``classify_intent`` the conversation state needed to resolve which
proposal a multi-turn edit refers to, even after the proposal has expired.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SMSThread


class SMSThreadStore:
    """Deterministic sms_thread store, scoped to a single database session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_latest(self, proposal_id: int) -> SMSThread | None:
        """Return the most recent thread entry for a proposal."""
        result = await self._session.execute(
            select(SMSThread)
            .where(SMSThread.proposal_id == proposal_id)
            .order_by(SMSThread.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_proposal(
        self, proposal_id: int, limit: int = 50
    ) -> list[SMSThread]:
        """List thread entries for a proposal, oldest first."""
        result = await self._session.execute(
            select(SMSThread)
            .where(SMSThread.proposal_id == proposal_id)
            .order_by(SMSThread.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def append(
        self, proposal_id: int, user_id: int, raw_body: str
    ) -> SMSThread:
        """Append an inbound reply to a proposal's thread."""
        entry = SMSThread(
            proposal_id=proposal_id,
            user_id=user_id,
            raw_body=raw_body,
        )
        self._session.add(entry)
        await self._session.flush()
        return entry