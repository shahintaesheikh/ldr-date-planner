"""Pydantic schemas for the proposals table.

Proposals are written by the agent's ``compose_proposal`` node and read /
amended by the ``edit_proposal`` node in the inbound SMS graph.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import ProposalStatus


class ProposalCreate(BaseModel):
    """Input schema for creating a new proposal (status=pending)."""

    couple_id: int = Field(..., description="Couple this proposal is for")
    activity_id: int = Field(..., description="Catalog activity being proposed")
    proposed_start: datetime = Field(..., description="Proposed start (timezone-aware)")
    proposed_end: datetime = Field(..., description="Proposed end (timezone-aware)")


class ProposalUpdate(BaseModel):
    """Input schema for patching a proposal.

    All fields optional; only provided fields are changed. Used by
    ``validate_edit`` to apply a validated ``ProposalEdit`` to an existing row.
    """

    activity_id: int | None = Field(default=None, description="Replacement activity id")
    proposed_start: datetime | None = Field(
        default=None, description="New proposed start (timezone-aware)"
    )
    proposed_end: datetime | None = Field(
        default=None, description="New proposed end (timezone-aware)"
    )
    status: ProposalStatus | None = Field(
        default=None, description="New proposal status"
    )
    confirmed_by: int | None = Field(
        default=None, description="User id who confirmed the proposal"
    )


class ProposalRead(BaseModel):
    """Output schema representing a proposal row."""

    id: int
    couple_id: int
    activity_id: int
    proposed_start: datetime
    proposed_end: datetime
    status: ProposalStatus
    confirmed_by: int | None
    created_at: datetime

    model_config = {"from_attributes": True}