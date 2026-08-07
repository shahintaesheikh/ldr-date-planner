"""Services layer — deterministic CRUD and business logic.

Each service wraps a single database table or domain concept and is the
only entry point for agent nodes to read/write that data.  Services are
scoped to a single ``AsyncSession`` and are instantiated per-request::

    async with db.session() as session:
        store = TraitStore(session)
        traits = await store.get_trait_set(couple_id=42)
"""

from app.services.trait_store import TraitStore

__all__ = ["TraitStore"]