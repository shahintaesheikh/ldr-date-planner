"""Feedback attribution service — resolves a proposal outcome into trait-weight
updates via the ``TraitStore.apply_signal_update`` method.

Pipeline
--------
1. Resolve ``proposal_id`` → ``activity_id`` → ``date_activities.tags[]``
   (and the proposal's ``couple_id`` for the update).
2. Divide the resolved signal strength across the tag count (so a multi-tag
   activity doesn't get 3× the influence of a single-tag activity).
3. Call ``apply_signal_update`` once per tag with the divided strength.

Signal-strength table (see ldr-phase6-plan.md §2 — feedback_attribution):
    - ``accept`` → ``+1.0`` (implicit track)
    - ``reject`` → ``-1.0`` (implicit track)
    - ``rerun``  → ``-0.25`` (implicit track, dampened — "rerun ≠ reject")
    - ``mute``   → no attribution call (couple-level flag only)
    - explicit rating → ``(rating - 3) / 2``, mapping 1→-1.0, 3→0.0, 5→+1.0
      (explicit track)

This service is the *only* place that knows about signal-strength values.
Nodes and handlers never do EMA math or strength mapping themselves.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DateActivity, FeedbackSignal, Proposal, TraitSource
from app.services.trait_store import TraitStore

logger = logging.getLogger(__name__)


class FeedbackAttribution:
    """Resolves a proposal outcome into per-tag trait-weight updates.

    Usage::

        attribution = FeedbackAttribution(session)
        await attribution.attribute(
            proposal_id=42,
            signal=FeedbackSignal.accept,
        )
        # — or for an explicit rating —
        await attribution.attribute_rating(
            proposal_id=42,
            rating=4,
        )

    ``mute`` has no trait attribution — callers skip the call entirely for
    ``mute`` (it only flips the couple-level ``suggestions_muted`` flag).
    """

    # --- Signal-strength table (implicit track) ---
    _SIGNAL_STRENGTH: dict[FeedbackSignal, float] = {
        FeedbackSignal.accept: 1.0,
        FeedbackSignal.reject: -1.0,
        FeedbackSignal.rerun: -0.25,
        # mute is intentionally absent — callers skip attribution for mute.
    }

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._trait_store = TraitStore(session)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def attribute(
        self,
        proposal_id: int,
        signal: FeedbackSignal,
    ) -> list[str]:
        """Attribute an implicit feedback signal to the proposal's activity tags.

        Resolves the proposal → activity → tags, divides the signal strength
        across the tag count, and calls ``apply_signal_update`` for each tag
        on the implicit weight track.

        Args:
            proposal_id: The proposal the feedback refers to.
            signal: The implicit signal (accept/reject/rerun).

        Returns:
            A list of ``trait_key`` values updated (empty if the proposal or
            activity cannot be resolved, or the signal is unknown).

        Raises:
            ValueError: If ``signal`` is ``mute`` — mute has no trait
                attribution and callers must skip it.
        """
        if signal == FeedbackSignal.mute:
            raise ValueError(
                "attribute() called with mute signal — mute has no trait "
                "attribution. Callers should skip attribution for mute."
            )

        strength = self._SIGNAL_STRENGTH.get(signal)
        if strength is None:
            logger.warning("Unknown signal %s — skipping attribution", signal)
            return []

        resolved = await self._resolve_proposal(proposal_id)
        if resolved is None:
            return []

        couple_id, tags = resolved
        return await self._apply_to_tags(
            couple_id=couple_id,
            tags=tags,
            signal_strength=strength,
            source=TraitSource.implicit,
        )

    async def attribute_rating(
        self,
        proposal_id: int,
        rating: int | None,
    ) -> list[str]:
        """Attribute an explicit rating to the proposal's activity tags.

        A ``None`` rating (SKIP) is logged by the caller but produces no
        trait updates.

        Args:
            proposal_id: The proposal the rating refers to.
            rating: The numeric rating (1-5), or ``None`` for SKIP.

        Returns:
            A list of ``trait_key`` values updated (empty for SKIP or if the
            proposal/activity cannot be resolved).
        """
        if rating is None:
            return []  # SKIP — no trait update

        # Map 1-5 rating to -1.0..+1.0 (1→-1.0, 3→0.0, 5→+1.0).
        strength = (rating - 3) / 2.0

        resolved = await self._resolve_proposal(proposal_id)
        if resolved is None:
            return []

        couple_id, tags = resolved
        return await self._apply_to_tags(
            couple_id=couple_id,
            tags=tags,
            signal_strength=strength,
            source=TraitSource.explicit,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_proposal(
        self, proposal_id: int
    ) -> tuple[int, list[str]] | None:
        """Resolve a proposal_id to ``(couple_id, activity_tags)``.

        Returns ``None`` if the proposal or its activity cannot be found
        (in which case there is nothing to attribute to).
        """
        result = await self._session.execute(
            select(Proposal).where(Proposal.id == proposal_id)
        )
        proposal = result.scalar_one_or_none()
        if proposal is None:
            logger.warning(
                "Proposal %d not found — cannot resolve attribution", proposal_id
            )
            return None

        result = await self._session.execute(
            select(DateActivity).where(DateActivity.id == proposal.activity_id)
        )
        activity = result.scalar_one_or_none()
        if activity is None:
            logger.warning(
                "Activity %d not found (proposal %d) — cannot resolve attribution",
                proposal.activity_id,
                proposal_id,
            )
            return None

        return proposal.couple_id, list(activity.tags or [])

    async def _apply_to_tags(
        self,
        couple_id: int,
        tags: list[str],
        signal_strength: float,
        source: TraitSource,
    ) -> list[str]:
        """Apply a signal strength divided across ``tags`` to each tag.

        Returns:
            A list of ``trait_key`` values that were successfully updated.
        """
        if not tags:
            return []

        divided = signal_strength / len(tags)
        updated_keys: list[str] = []

        for tag in tags:
            try:
                result = await self._trait_store.apply_signal_update(
                    couple_id=couple_id,
                    trait_key=tag,
                    source=source,
                    signal_strength=divided,
                )
                if result is not None:
                    updated_keys.append(tag)
            except Exception:
                logger.exception(
                    "Failed to apply signal update for couple %d, tag %s",
                    couple_id,
                    tag,
                )

        return updated_keys
