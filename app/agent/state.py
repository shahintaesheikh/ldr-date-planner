"""State schemas for both LangGraph agents.

Two graphs, two state schemas:

- ``IdeationState`` for the ideation graph (fetch_availability → … → deliver_sms).
- ``SMSState`` for the inbound SMS graph (classify_intent → route / edit_proposal).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, TypedDict

from app.schemas.trait import TraitSet


# =========================================================================
# Shared value objects
# =========================================================================


@dataclass
class OverlapWindow:
    """A candidate time window where both partners are free."""

    start: datetime  # UTC
    end: datetime  # UTC
    duration_min: int = 0

    def __post_init__(self) -> None:
        if self.duration_min == 0 and self.end > self.start:
            self.duration_min = int((self.end - self.start).total_seconds() // 60)


@dataclass
class ProposalDraft:
    """Canonical proposal data used by ``compose_proposal``.

    Both the ideation path and the edit path populate this before the shared
    compose_proposal / deliver_sms nodes.
    """

    activity_id: int
    activity_name: str
    description: str
    start: datetime
    end: datetime
    duration_min: int


# =========================================================================
# Ideation graph state
# =========================================================================


class IdeationState(TypedDict):
    """State for the ideation graph (fetch_availability → … → deliver_sms)."""

    # --- Input ---
    couple_id: int
    window_start: datetime  # UTC, start of the search window (typically "now")
    window_end: datetime  # UTC, end of the search window
    on_demand: bool  # True = on-demand (favor next 3-5 days), False = scheduled
    min_duration_min: int  # floor overlap, default 60
    exclude_activity_id: int | None  # set by RERUN to skip prior activity

    # --- Populated by fetch_availability ---
    busy_blocks_a: list[dict]  # list of {start, end} UTC
    busy_blocks_b: list[dict]

    # --- Populated by find_overlap_windows ---
    overlap_windows: list[OverlapWindow]

    # --- Populated by load_traits ---
    trait_set: TraitSet | None

    # --- Populated by ideate_activity ---
    selected_activity: dict | None  # {activity_id, name, description, est_duration_min, source, novel}

    # --- Populated by estimate_duration ---
    selected_window: OverlapWindow | None  # refined window matching the activity

    # --- Populated by compose_proposal ---
    draft: ProposalDraft | None
    proposal: dict | None  # serialised Proposal row
    sms_copy: str | None  # the formatted SMS text (base, UTC)

    # --- Populated by deliver_sms ---
    delivery_results: list[dict]  # [{user_id, to, sid, body, local_time}]

    # --- Accumulated ---
    errors: list[str]


# =========================================================================
# Inbound SMS graph state
# =========================================================================


class SMSState(TypedDict):
    """State for the inbound SMS graph (classify_intent → route / edit → …)."""

    # --- Input (from the Twilio webhook) ---
    from_phone: str  # E.164, the sender's phone number
    raw_body: str  # the raw SMS text

    # --- Populated by classify_intent ---
    couple_id: int | None
    user_id: int | None  # resolved sender
    proposal_id: int | None  # resolved pending proposal
    intent: str | None  # "YES" | "NO" | "RERUN" | "STOP" | "EDIT" | "UNKNOWN"

    # --- Populated by edit_proposal (agentic node) ---
    edit: dict | None  # serialised ProposalEdit (or None = no-op)
    needs_clarification: bool | None
    clarification_msg: str | None

    # --- Populated by validate_edit ---
    edit_valid: bool | None

    # --- Populated by send_clarification ---
    clarification_sent: bool | None

    # --- Populated by route_yes / compose_proposal ---
    draft: ProposalDraft | None
    proposal: dict | None  # serialised Proposal row
    sms_copy: str | None

    # --- Populated by deliver_sms ---
    delivery_results: list[dict]

    # --- Accumulated ---
    errors: list[str]