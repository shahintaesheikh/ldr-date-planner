"""Trait store service — deterministic CRUD for the EAV ``traits`` table.

This is the only entry point for reading and writing traits; agent nodes
(``load_traits``, feedback loop) interact with the trait store through this
service, never directly with the ORM session.  The service is intentionally
free of AI/ML inference — it is a pure data-access layer.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Trait, TraitSource
from app.schemas.trait import TraitCreate, TraitRead, TraitSet, TraitUpdate


class TraitStore:
    """Deterministic trait store, scoped to a single database session.

    Usage::

        async with db.session() as session:
            store = TraitStore(session)
            vector = await store.get_trait_vector(couple_id=42)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_trait_set(self, couple_id: int) -> TraitSet:
        """Fetch the full trait set for a couple.

        This is the method called by the ``load_traits`` agent node.
        Returns a ``TraitSet`` with a ``traits`` dict keyed by
        ``trait_key``, making it directly consumable by the LLM.

        **Weight blend**: when ``explicit_weight`` is set, the returned
        ``weight`` is the blended value:
        ``effective_weight = 0.3 * weight + 0.7 * explicit_weight``.
        When ``explicit_weight`` is ``None``, ``weight`` is returned as-is.
        See ldr-phase6-plan.md §1.
        """
        rows = await self._get_all(couple_id)
        traits: dict[str, dict[str, Any]] = {}
        for t in rows:
            effective = self._blend_weight(t.weight, t.explicit_weight)
            traits[t.trait_key] = {
                "value": t.value,
                "weight": effective,
                "explicit_weight": t.explicit_weight,
                "source": t.source.value,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
        return TraitSet(couple_id=couple_id, traits=traits, count=len(traits))

    async def get_trait(
        self, couple_id: int, trait_key: str
    ) -> TraitRead | None:
        """Fetch a single trait by couple_id + trait_key.

        Returns ``None`` if the trait does not exist.
        """
        result = await self._session.execute(
            select(Trait).where(
                Trait.couple_id == couple_id,
                Trait.trait_key == trait_key,
            )
        )
        trait = result.scalar_one_or_none()
        return TraitRead.model_validate(trait) if trait else None

    async def list_traits(self, couple_id: int) -> list[TraitRead]:
        """List all traits for a couple as ``TraitRead`` objects."""
        rows = await self._get_all(couple_id)
        return [TraitRead.model_validate(r) for r in rows]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def upsert_trait(self, couple_id: int, data: TraitCreate) -> TraitRead:
        """Create or update a trait.

        If a trait with the same ``couple_id`` + ``trait_key`` already
        exists, its ``value``, ``weight``, ``source``, and ``updated_at``
        are overwritten.  Otherwise a new row is inserted.

        This is the primary write method used by the onboarding flow
        (explicit traits) and the feedback loop (implicit traits).
        """
        result = await self._session.execute(
            select(Trait).where(
                Trait.couple_id == couple_id,
                Trait.trait_key == data.trait_key,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.value = data.value
            existing.weight = data.weight
            existing.source = data.source
            # explicit_weight is None when not provided — leave an existing
            # explicit weight untouched in that case (None means "no rating
            # recorded yet", not "clear it").
            if data.explicit_weight is not None:
                existing.explicit_weight = data.explicit_weight
            existing.updated_at = datetime.now(timezone.utc)
            trait = existing
        else:
            trait = Trait(
                couple_id=couple_id,
                trait_key=data.trait_key,
                value=data.value,
                weight=data.weight,
                explicit_weight=data.explicit_weight,
                source=data.source,
            )
            self._session.add(trait)

        await self._session.flush()
        return TraitRead.model_validate(trait)

    async def update_trait(
        self, couple_id: int, trait_key: str, data: TraitUpdate
    ) -> TraitRead | None:
        """Patch specific fields on an existing trait.

        Only the fields set on ``data`` are applied.  Returns ``None`` if
        the trait does not exist.

        Used by the feedback loop to adjust trait weights without
        overwriting the entire row.
        """
        # Build a dict of only the non-None fields
        updates: dict[str, Any] = {}
        for field_name in ("value", "weight", "explicit_weight", "source"):
            val = getattr(data, field_name, None)
            if val is not None:
                updates[field_name] = val

        if not updates:
            # Nothing to update — return current state
            return await self.get_trait(couple_id, trait_key)

        updates["updated_at"] = datetime.now(timezone.utc)

        result = await self._session.execute(
            update(Trait)
            .where(
                Trait.couple_id == couple_id,
                Trait.trait_key == trait_key,
            )
            .values(**updates)
            .returning(Trait)
        )
        trait = result.scalar_one_or_none()
        return TraitRead.model_validate(trait) if trait else None

    async def delete_trait(self, couple_id: int, trait_key: str) -> bool:
        """Delete a single trait.  Returns ``True`` if a row was removed."""
        result = await self._session.execute(
            delete(Trait).where(
                Trait.couple_id == couple_id,
                Trait.trait_key == trait_key,
            )
        )
        return result.rowcount > 0

    async def delete_all_traits(self, couple_id: int) -> int:
        """Remove every trait for a couple.  Returns the number of rows deleted."""
        result = await self._session.execute(
            delete(Trait).where(Trait.couple_id == couple_id)
        )
        return result.rowcount

    # ------------------------------------------------------------------
    # Feedback-loop methods
    # ------------------------------------------------------------------

    _LEARNING_RATE: float = 0.3
    """EMA learning rate applied to implicit and explicit weight updates.

    ``weight_new = weight_old + learning_rate * signal_strength``, clamped
    to ``[0.0, 1.0]``.
    """

    async def apply_signal_update(
        self,
        couple_id: int,
        trait_key: str,
        source: TraitSource,
        signal_strength: float,
    ) -> TraitRead | None:
        """Apply an EMA update to a trait's weight track and persist it.

        ``source`` determines which track is updated:

        - ``TraitSource.implicit`` → updates the ``weight`` column (implicit track).
        - ``TraitSource.explicit`` → updates the ``explicit_weight`` column.

        If the trait does not exist yet, it is created with default values
        and the update applied on top.

        Args:
            couple_id: The couple whose trait to update.
            trait_key: The trait key (e.g. ``"energy_pref"``).
            source: Which weight track to update.
            signal_strength: A value in ``[-1.0, +1.0]`` encoding the
                direction and magnitude of the feedback signal.

        Returns:
            The updated ``TraitRead``, or ``None`` if the trait row could
            not be created/updated.
        """
        existing = await self.get_trait(couple_id, trait_key)

        if existing:
            current_weight = (
                existing.explicit_weight
                if source == TraitSource.explicit
                else existing.weight
            )
            old_val = current_weight if current_weight is not None else 1.0
            new_val = max(0.0, min(1.0, old_val + self._LEARNING_RATE * signal_strength))

            update_data = TraitUpdate()
            if source == TraitSource.explicit:
                update_data.explicit_weight = new_val
                update_data.source = TraitSource.explicit
            else:
                update_data.weight = new_val
                update_data.source = TraitSource.implicit

            return await self.update_trait(couple_id, trait_key, update_data)
        else:
            # Trait doesn't exist yet — create it with the updated weight.
            init_weight = max(0.0, min(1.0, 1.0 + self._LEARNING_RATE * signal_strength))
            create_data = TraitCreate(
                trait_key=trait_key,
                value=trait_key,  # Use key as initial value; the caller can
                                  # override via a subsequent update.
                weight=init_weight if source == TraitSource.implicit else 1.0,
                explicit_weight=init_weight if source == TraitSource.explicit else None,
                source=source,
            )
            return await self.upsert_trait(couple_id, create_data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _blend_weight(
        implicit_weight: float, explicit_weight: float | None
    ) -> float:
        """Blend implicit and explicit weight tracks into a single effective weight.

        When ``explicit_weight`` is ``None``, the implicit weight is returned
        as-is.  Otherwise the blend is:

            effective_weight = 0.3 * implicit_weight + 0.7 * explicit_weight

        The coefficients favour explicit (stated) preferences over implicit
        (inferred) signals, so a single rating can meaningfully shift the
        trait vector without erasing implicit history.
        """
        if explicit_weight is None:
            return implicit_weight
        return round(0.3 * implicit_weight + 0.7 * explicit_weight, 4)

    async def _get_all(self, couple_id: int) -> list[Trait]:
        """Raw ORM query — returns ``Trait`` ORM objects, not schemas."""
        result = await self._session.execute(
            select(Trait)
            .where(Trait.couple_id == couple_id)
            .order_by(Trait.trait_key)
        )
        return list(result.scalars().all())