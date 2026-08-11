"""Pydantic schemas for the EAV trait store.

Traits are keyed to ``couple_id`` (not per-user) and carry independent
``weight``, ``source``, and ``updated_at`` per row.  The schema layer
validates inputs and shapes outputs for the ``TraitStore`` service and
downstream agent nodes (notably ``load_traits``).
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import TraitSource


class TraitCreate(BaseModel):
    """Input schema for creating or upserting a single trait.

    All fields are required; the service layer handles the
    create-vs-update logic.
    """

    trait_key: str = Field(
        ...,
        max_length=64,
        description="Open-ended trait key, e.g. 'activity_type_pref', 'energy_pref'",
    )
    value: str = Field(
        ...,
        max_length=255,
        description="Trait value, e.g. 'outdoor', 'low_energy', 'cooking'",
    )
    weight: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Implicit weight of this trait (0.0–1.0). Updated by EMA on feedback signals.",
    )
    explicit_weight: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Explicit weight track; set by ratings. NULL until first rating.",
    )
    source: TraitSource = Field(
        default=TraitSource.explicit,
        description="Origin of the trait: 'explicit' (user-declared) or 'implicit' (inferred from feedback).",
    )


class TraitUpdate(BaseModel):
    """Input schema for updating an existing trait's mutable fields.

    All fields are optional; only provided fields are patched.
    """

    value: str | None = Field(
        default=None,
        max_length=255,
        description="New trait value",
    )
    weight: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="New implicit weight for this trait",
    )
    explicit_weight: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="New explicit weight for this trait",
    )
    source: TraitSource | None = Field(
        default=None,
        description="New source for this trait",
    )


class TraitRead(BaseModel):
    """Output schema representing a single trait row."""

    id: int
    couple_id: int
    trait_key: str
    value: str
    weight: float
    explicit_weight: float | None
    source: TraitSource
    updated_at: datetime

    model_config = {"from_attributes": True}


class TraitSet(BaseModel):
    """A couple's full trait set — the collection of all known traits.

    Returned by ``TraitStore.get_trait_set()`` for the ``load_traits``
    agent node.  The ``traits`` dict maps ``trait_key`` → its value/weight
    so the LLM can consume it directly without iterating rows.
    """

    couple_id: int
    traits: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        description=(
            "Mapping of trait_key -> {value, weight, source, updated_at}. "
            "Example: {'energy_pref': {'value': 'low_energy', 'weight': 0.8, "
            "'source': 'explicit', 'updated_at': '2025-01-15T...'}}"
        ),
    )
    count: int = Field(default=0, description="Number of traits in the set")