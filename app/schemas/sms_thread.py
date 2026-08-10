"""Pydantic schemas for the sms_thread table.

Backs multi-turn edit state: a reply like "push it later" followed by
"actually Sunday not Saturday" needs conversation state scoped to the pending
proposal. ``classify_intent`` reads threads to resolve which proposal a reply
amends, and appends each inbound reply so the conversation is preserved even
after a proposal expires.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class SMSThreadCreate(BaseModel):
    """Input schema for appending an inbound SMS reply to a proposal thread."""

    proposal_id: int = Field(..., description="Proposal this reply amends")
    user_id: int = Field(..., description="Sender user id")
    raw_body: str = Field(..., min_length=1, description="Raw SMS body text")


class SMSThreadRead(BaseModel):
    """Output schema representing an sms_thread row."""

    id: int
    proposal_id: int
    user_id: int
    raw_body: str
    created_at: datetime

    model_config = {"from_attributes": True}