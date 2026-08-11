"""Proposal store service — CRUD for the ``proposals`` table.

The agent's ``compose_proposal`` node writes pending proposals here, and the
inbound SMS graph's ``route_yes`` / ``route_no`` / ``validate_edit`` nodes
update status and schedule. Services are scoped to a single ``AsyncSession``
and instantiated per-request; nodes never touch the ORM session directly.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Proposal, ProposalStatus
from app.schemas.proposal import ProposalCreate, ProposalUpdate


class ProposalStore:
    """Deterministic proposal store, scoped to a single database session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, proposal_id: int) -> Proposal | None:
        """Fetch a single proposal by id."""
        result = await self._session.execute(
            select(Proposal).where(Proposal.id == proposal_id)
        )
        return result.scalar_one_or_none()

    async def get_latest_pending(self, couple_id: int) -> Proposal | None:
        """Return the most recent pending proposal for a couple.

        Used by the inbound SMS graph to resolve which proposal a reply
        amends, and by on-demand cadence to avoid stacking proposals.
        """
        result = await self._session.execute(
            select(Proposal)
            .where(Proposal.couple_id == couple_id, Proposal.status == ProposalStatus.pending)
            .order_by(Proposal.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest(self, couple_id: int) -> Proposal | None:
        """Return the most recent proposal for a couple regardless of status.

        Used to resolve a reply when no proposal is *pending* anymore (e.g. a
        follow-up reply arriving after the proposal was confirmed or expired).
        """
        result = await self._session.execute(
            select(Proposal)
            .where(Proposal.couple_id == couple_id)
            .order_by(Proposal.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_for_couple(
        self, couple_id: int, limit: int = 20
    ) -> list[Proposal]:
        """List recent proposals for a couple, newest first."""
        result = await self._session.execute(
            select(Proposal)
            .where(Proposal.couple_id == couple_id)
            .order_by(Proposal.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_awaiting_rating(self, couple_id: int) -> Proposal | None:
        """Return the most recent confirmed proposal whose end time has passed
        and that has no associated feedback rating yet.

        This is the query the rating-trigger job runs per couple: it finds
        proposals that need a follow-up SMS asking "How was it?"

        A ``SKIP`` reply writes a feedback row (with ``rating=None``), so
        the proposal is still marked as "asked" and will not be re-prompted.

        Returns ``None`` if no proposal matches (all up-to-date).
        """
        now = datetime.now(timezone.utc)
        result = await self._session.execute(
            select(Proposal)
            .where(
                Proposal.couple_id == couple_id,
                Proposal.status == ProposalStatus.confirmed,
                Proposal.proposed_end < now,
                # No feedback row exists at all for this proposal — i.e. the
                # rating prompt hasn't been sent yet, or was sent but the
                # user hasn't replied.  Once any feedback row exists (even
                # with rating=None for SKIP), the proposal is considered
                # "asked" and excluded from future prompts.
                ~Proposal.feedback.any(),
            )
            .order_by(Proposal.proposed_end.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def create_pending(
        self,
        couple_id: int,
        activity_id: int,
        proposed_start: datetime,
        proposed_end: datetime,
    ) -> Proposal:
        """Insert a new proposal with status=pending.

        This is the write performed by ``compose_proposal`` in the ideation
        graph. Fields are flushed (not committed) so the caller owns the
        transaction.
        """
        proposal = Proposal(
            couple_id=couple_id,
            activity_id=activity_id,
            proposed_start=proposed_start,
            proposed_end=proposed_end,
            status=ProposalStatus.pending,
        )
        self._session.add(proposal)
        await self._session.flush()
        return proposal

    async def update(self, proposal_id: int, data: ProposalUpdate) -> Proposal | None:
        """Patch a proposal. Only the fields set on ``data`` are changed.

        Returns ``None`` if the proposal does not exist.
        """
        proposal = await self.get(proposal_id)
        if proposal is None:
            return None

        if data.activity_id is not None:
            proposal.activity_id = data.activity_id
        if data.proposed_start is not None:
            proposal.proposed_start = data.proposed_start
        if data.proposed_end is not None:
            proposal.proposed_end = data.proposed_end
        if data.status is not None:
            proposal.status = data.status
        if data.confirmed_by is not None:
            proposal.confirmed_by = data.confirmed_by

        await self._session.flush()
        return proposal

    async def set_status(
        self,
        proposal_id: int,
        status: ProposalStatus,
        confirmed_by: int | None = None,
    ) -> Proposal | None:
        """Shorthand to set a proposal's status (and optional confirmations)."""
        return await self.update(
            proposal_id,
            ProposalUpdate(
                status=status,
                confirmed_by=confirmed_by if confirmed_by is not None else None,
            ),
        )