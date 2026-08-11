"""Feedback store service — persistence for the ``feedback`` table.

The feedback loop (Phase 6) will consume these rows to update trait weights.
In v1 the agent only *writes* implicit signals (accept/reject/rerun/mute) from
the inbound SMS graph's keyword routes; nothing reads them yet. This service
is the single entry point for writing feedback; nodes never touch the ORM
directly.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Feedback, FeedbackSignal


class FeedbackStore:
    """Deterministic feedback store, scoped to a single database session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        proposal_id: int,
        signal: FeedbackSignal,
        rating: int | None = None,
    ) -> Feedback:
        """Write a feedback row for a proposal (implicit-signal path).

        Args:
            proposal_id: The proposal the feedback refers to.
            signal: The implicit signal (accept/reject/rerun/mute).
            rating: Optional explicit rating (unused in v1).
        """
        feedback = Feedback(
            proposal_id=proposal_id,
            rating=rating,
            implicit_signal=signal,
        )
        self._session.add(feedback)
        await self._session.flush()
        return feedback

    async def log_rating(self, proposal_id: int, rating: int | None) -> Feedback:
        """Write an explicit-rating feedback row for a proposal.

        Distinct from :meth:`log`: the row is created with ``rating`` set and
        ``implicit_signal`` left ``None``, keeping the two write paths
        explicit. A ``None`` rating (SKIP reply) still writes a row so the
        rating-trigger job treats the proposal as "asked" and does not
        re-prompt on the next scheduled run.

        Args:
            proposal_id: The proposal the rating refers to.
            rating: The numeric rating (1-5), or ``None`` for SKIP.
        """
        feedback = Feedback(
            proposal_id=proposal_id,
            rating=rating,
            implicit_signal=None,
        )
        self._session.add(feedback)
        await self._session.flush()
        return feedback