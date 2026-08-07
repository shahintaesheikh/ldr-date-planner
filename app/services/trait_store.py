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
        """
        rows = await self._get_all(couple_id)
        traits: dict[str, dict[str, Any]] = {}
        for t in rows:
            traits[t.trait_key] = {
                "value": t.value,
                "weight": t.weight,
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
            existing.updated_at = datetime.now(timezone.utc)
            trait = existing
        else:
            trait = Trait(
                couple_id=couple_id,
                trait_key=data.trait_key,
                value=data.value,
                weight=data.weight,
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
        for field_name in ("value", "weight", "source"):
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
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_all(self, couple_id: int) -> list[Trait]:
        """Raw ORM query — returns ``Trait`` ORM objects, not schemas."""
        result = await self._session.execute(
            select(Trait)
            .where(Trait.couple_id == couple_id)
            .order_by(Trait.trait_key)
        )
        return list(result.scalars().all())