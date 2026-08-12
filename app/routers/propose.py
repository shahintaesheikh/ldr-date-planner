"""Propose router — on-demand invocation of the ideation graph.

The ``POST /propose`` endpoint triggers the same ideation workflow that the
scheduled cadence job runs, but synchronously per request.  It includes the
same guard against stacking proposals: if a pending proposal already exists
for the given couple, it returns ``409 Conflict``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import db
from app.agent import GraphDeps, ideation_graph
from app.agent.state import IdeationState
from app.services.proposal_store import ProposalStore

logger = logging.getLogger(__name__)

router = APIRouter(tags=["propose"])

# Default look-ahead window for on-demand proposals.
_ON_DEMAND_WINDOW_DAYS = 7


class ProposeRequest(BaseModel):
    """Request body for the on-demand /propose endpoint."""

    couple_id: int = Field(..., description="Couple to generate a proposal for")


class ProposeResponse(BaseModel):
    """Response from a successful /propose call."""

    proposal_id: int | None = Field(None, description="The created proposal id")
    activity_name: str | None = Field(None, description="Name of the proposed activity")
    activity_description: str | None = Field(None, description="Description of the proposed activity")
    proposed_start: str | None = Field(None, description="ISO 8601 UTC start time")
    proposed_end: str | None = Field(None, description="ISO 8601 UTC end time")
    sms_copy: str | None = Field(None, description="The SMS text that was sent")
    delivery_results: list[dict] = Field(
        default_factory=list, description="SMS delivery results per partner"
    )
    errors: list[str] = Field(
        default_factory=list, description="Errors encountered during processing"
    )


class ProposeErrorResponse(BaseModel):
    """Error response from /propose."""

    detail: str = Field(..., description="Human-readable error description")
    errors: list[str] = Field(
        default_factory=list, description="Detailed error messages"
    )


@router.post(
    "/propose",
    response_model=ProposeResponse,
    responses={
        409: {"model": ProposeErrorResponse, "description": "Existing pending proposal"},
        500: {"model": ProposeErrorResponse, "description": "Internal server error"},
    },
)
async def propose_on_demand(
    request: ProposeRequest,
) -> ProposeResponse:
    """Trigger the ideation graph on-demand for a couple.

    Runs the full ideation workflow (fetch availability → find overlap →
    load traits → ideate activity → estimate duration → compose proposal →
    deliver SMS).  Returns the resulting proposal and SMS delivery status.

    If a pending proposal already exists for this couple, returns
    ``409 Conflict`` with the existing proposal's id in the error message.
    """
    couple_id = request.couple_id
    logger.info("/propose: on-demand request for couple %d", couple_id)

    # --- Guard: check for existing pending proposal ---
    async with db.session() as session:
        store = ProposalStore(session)
        existing = await store.get_latest_pending(couple_id)
        if existing is not None:
            logger.warning(
                "/propose: couple %d already has a pending proposal %d — rejecting",
                couple_id,
                existing.id,
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Couple {couple_id} already has a pending proposal "
                    f"(id={existing.id}). Confirm or reject it before "
                    "requesting a new one."
                ),
            )

    # --- Build deps ---
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=_ON_DEMAND_WINDOW_DAYS)

    from app.adapters.sms import build_sms_gateway
    from langchain_anthropic import ChatAnthropic
    from app import settings

    llm: ChatAnthropic | None = None
    if settings.anthropic_api_key:
        llm = ChatAnthropic(
            model="claude-sonnet-4-20250514",
            temperature=0.7,
            api_key=settings.anthropic_api_key,
        )
    else:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY is not set — cannot run the ideation graph",
        )

    sms_gateway = build_sms_gateway(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        from_phone=settings.twilio_phone_number,
        status_callback_url=settings.twilio_status_callback_url,
    )

    # --- Run the ideation graph ---
    async with db.session() as session:
        deps = GraphDeps(
            db=session,
            llm=llm,
            sms_gateway=sms_gateway,
        ).resolved()

        state: IdeationState = {
            "couple_id": couple_id,
            "window_start": now,
            "window_end": window_end,
            "on_demand": True,
            "min_duration_min": 60,
            "exclude_activity_id": None,
            "busy_blocks_a": [],
            "busy_blocks_b": [],
            "overlap_windows": [],
            "trait_set": None,
            "selected_activity": None,
            "selected_window": None,
            "draft": None,
            "proposal": None,
            "sms_copy": None,
            "delivery_results": [],
            "errors": [],
        }

        try:
            result = await ideation_graph.ainvoke(
                state,
                {"configurable": {"deps": deps}},
            )
        except Exception as exc:
            logger.exception(
                "/propose: ideation graph failed for couple %d", couple_id
            )
            raise HTTPException(
                status_code=500,
                detail=f"Ideation graph failed: {exc}",
            )

        errors = result.get("errors") or []
        if errors:
            logger.warning(
                "/propose: ideation graph for couple %d returned errors: %s",
                couple_id,
                errors,
            )

        # Extract proposal details for the response.
        proposal = result.get("proposal")
        draft = result.get("draft")
        delivery_results = result.get("delivery_results") or []

        await session.commit()

        response = ProposeResponse(
            proposal_id=proposal.get("id") if proposal else None,
            activity_name=draft.activity_name if draft else None,
            activity_description=draft.description if draft else None,
            proposed_start=draft.start.isoformat() if draft else None,
            proposed_end=draft.end.isoformat() if draft else None,
            sms_copy=result.get("sms_copy"),
            delivery_results=delivery_results,
            errors=errors,
        )

        logger.info(
            "/propose: completed for couple %d — proposal_id=%s",
            couple_id,
            response.proposal_id,
        )

        return response