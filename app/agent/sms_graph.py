"""Inbound SMS graph — single Twilio webhook entry, keyword and NL paths.

Pipeline::

    classify_intent → route (keyword path: YES/NO/RERUN/STOP)
                   └→ edit_proposal → validate_edit → compose_proposal → deliver_sms
                                              └→ send_clarification (fallback)

``classify_intent`` fast-paths exact keywords via regex and routes everything
else to the NL edit path. Only ``edit_proposal`` (ProposalEdit tool) is
agentic. ``compose_proposal`` and ``deliver_sms`` are imported from
``app.agent.common`` so both graphs share the exact same serialisation and
delivery logic. See ldr-date-agent-devplan.md §2/§4/§5.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agent.common import (
    EDIT_SYSTEM_PROMPT,
    _proposal_to_dict,
    compose_proposal,
    deliver_sms,
    format_confirmation_sms,
    send_clarification,
)
from app.agent.deps import _deps
from app.agent.state import ProposalDraft, SMSState
from app.agent.tools import ProposalEdit
from app.models import FeedbackSignal, ProposalStatus
from app.schemas.proposal import ProposalUpdate
from app.services.feedback_attribution import FeedbackAttribution

logger = logging.getLogger(__name__)

# =========================================================================
# Keyword fast-path patterns (deterministic-first intent classification)
# =========================================================================

_RERUN_RE = re.compile(
    r"\b(rerun|try again|something else|another one|different idea|"
    r"new idea|again)\b",
    re.IGNORECASE,
)
_STOP_RE = re.compile(
    r"\b(stop|mute|quiet|enough|unsubscribe|turn off)\b", re.IGNORECASE
)
_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|confirm|sounds good|ok|okay|do it|"
    r"go for it|approved?)\b",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"\b(no|nope|nah|pass|decline|not that|not this|not now)\b", re.IGNORECASE
)

# Rating fast-path (Phase 6 — reply to the "How was it?" prompt).
# Matches a bare 1-5 digit or the SKIP keyword, so a rating reply arriving
# after a proposal was confirmed (and thus no longer pending) routes to
# ``route_rating`` instead of falling through to the edit path.
_RATING_RE = re.compile(r"^(?:\s*([1-5])\s*)$", re.IGNORECASE)
_SKIP_RE = re.compile(r"^\s*(skip|skip it|skip this one)\s*$", re.IGNORECASE)


def parse_rating_reply(raw_body: str) -> int | str | None:
    """Parse a reply as a rating (1-5) or SKIP.

    Returns:
        - ``int`` 1-5 for a rating reply
        - ``"SKIP"`` for a skip reply
        - ``None`` if the reply is not a rating/skip
    """
    body = raw_body.strip()
    if not body:
        return None
    rating_match = _RATING_RE.match(body)
    if rating_match:
        return int(rating_match.group(1))
    if _SKIP_RE.match(body):
        return "SKIP"
    return None


def classify_reply(raw_body: str) -> str:
    """Fast-path intent classification.

    Priority: RERUN > STOP > YES > NO > EDIT. A reply like "no, try again"
    routes to RERUN, not NO.
    """
    body = raw_body.strip().lower()
    if not body:
        return "EDIT"
    if _RERUN_RE.search(body):
        return "RERUN"
    if _STOP_RE.search(body):
        return "STOP"
    if _YES_RE.search(body):
        return "YES"
    if _NO_RE.search(body):
        return "NO"
    return "EDIT"


# =========================================================================
# Node implementations
# =========================================================================


async def classify_intent(state: dict, config: RunnableConfig) -> dict:
    """Resolve sender + proposal, append to the sms_thread, and classify intent."""
    deps = _deps(config).resolved()

    user = await deps.couple_store.get_user_by_phone(state["from_phone"])
    if user is None:
        return {
            "intent": "UNKNOWN",
            "needs_clarification": True,
            "clarification_msg": "I don't recognise that phone number.",
            "errors": [f"No user found for phone {state['from_phone']}"],
        }

    couple = await deps.couple_store.get_couple_for_user(user.id)
    if couple is None:
        return {
            "intent": "UNKNOWN",
            "needs_clarification": True,
            "clarification_msg": "I can't find your couple account.",
            "errors": [f"No couple found for user {user.id}"],
        }

    # Resolve which proposal this reply amends (multi-turn state via sms_thread).
    proposal = await deps.proposal_store.get_latest_pending(couple.id)
    proposal_id = proposal.id if proposal else None

    # Phase 6 rating branch: if no pending proposal exists, check whether
    # the reply is a rating (1-5) or SKIP directed at a past confirmed
    # proposal.  If so, route to ``route_rating`` instead of the edit path.
    if proposal_id is None:
        rating = parse_rating_reply(state["raw_body"])
        if rating is not None:
            awaiting = await deps.proposal_store.get_awaiting_rating(couple.id)
            if awaiting is not None:
                proposal_id = awaiting.id
                # Append the rating reply to the awaiting proposal's thread.
                await deps.sms_thread_store.append(
                    proposal_id=proposal_id, user_id=user.id, raw_body=state["raw_body"]
                )
                return {
                    "couple_id": couple.id,
                    "user_id": user.id,
                    "proposal_id": proposal_id,
                    "intent": "RATING",
                    "rating_parsed": rating,
                }

    # Append the raw reply to the proposal's thread for conversation context.
    if proposal_id is not None:
        await deps.sms_thread_store.append(
            proposal_id=proposal_id, user_id=user.id, raw_body=state["raw_body"]
        )

    return {
        "couple_id": couple.id,
        "user_id": user.id,
        "proposal_id": proposal_id,
        "intent": classify_reply(state["raw_body"]),
    }


def _route_on_intent(
    state: dict,
) -> Literal[
    "route_yes", "route_no", "route_rerun", "route_stop", "route_rating", "edit_proposal", "send_clarification"
]:
    """Conditional router after classify_intent."""
    return {
        "YES": "route_yes",
        "NO": "route_no",
        "RERUN": "route_rerun",
        "STOP": "route_stop",
        "RATING": "route_rating",
        "EDIT": "edit_proposal",
        "UNKNOWN": "send_clarification",
    }.get(state.get("intent"), "send_clarification")


async def route_yes(state: dict, config: RunnableConfig) -> dict:
    """Lock the proposal, write calendar events to both calendars, notify the
    other partner, and log implicit feedback (accept)."""
    deps = _deps(config).resolved()
    proposal_id = state.get("proposal_id")
    user_id = state.get("user_id")

    proposal = (
        await deps.proposal_store.get(proposal_id) if proposal_id else None
    )
    if proposal is None:
        return {"errors": ["route_yes: no pending proposal to confirm"]}

    await deps.proposal_store.set_status(
        proposal_id, ProposalStatus.confirmed, confirmed_by=user_id
    )
    await deps.feedback_store.log(proposal_id, FeedbackSignal.accept)

    # Attribute the accept signal to the activity's tags (implicit track).
    attribution = FeedbackAttribution(deps.db)
    await attribution.attribute(proposal_id, FeedbackSignal.accept)

    activity = await deps.catalog.get_by_id(deps.db, proposal.activity_id)
    title = f"Date night: {activity.name}" if activity else "Date night"

    # Write the event to both partners' calendars.
    for rc in await deps.calendar_resolver.get_active_adapters(
        deps.db, state["couple_id"]
    ):
        await rc.adapter.create_event(
            start=proposal.proposed_start,
            end=proposal.proposed_end,
            title=title,
            description="Long-distance date (confirmed via SMS)",
        )

    # Notify the other partner.
    delivery_results: list[dict] = []
    couple = await deps.couple_store.get_couple(state["couple_id"])
    if couple is not None:
        other = await deps.couple_store.get_other_partner(couple, user_id)
        if other is not None:
            confirmer = await deps.couple_store.get_user(user_id)
            confirmer_name = (
                confirmer.name.split()[0] if confirmer and confirmer.name else "Your partner"
            )
            body = format_confirmation_sms(
                activity_name=activity.name if activity else "your date",
                local_start=proposal.proposed_start,
                local_end=proposal.proposed_end,
                local_tz_name=other.timezone or "UTC",
                partner_name=confirmer_name,
            )
            try:
                sid = await deps.sms_gateway.send(other.phone_number, body)
                delivery_results.append(
                    {"user_id": other.id, "to": other.phone_number, "sid": sid, "body": body}
                )
            except Exception as exc:
                delivery_results.append(
                    {"user_id": other.id, "to": other.phone_number, "error": str(exc)}
                )

    return {
        "proposal": _proposal_to_dict(proposal),
        "delivery_results": delivery_results,
    }


async def route_no(state: dict, config: RunnableConfig) -> dict:
    """Reject the proposal and log implicit feedback (reject)."""
    deps = _deps(config).resolved()
    proposal_id = state.get("proposal_id")
    if proposal_id:
        await deps.proposal_store.set_status(proposal_id, ProposalStatus.rejected)
        await deps.feedback_store.log(proposal_id, FeedbackSignal.reject)
        # Attribute the reject signal to the activity's tags (implicit track).
        attribution = FeedbackAttribution(deps.db)
        await attribution.attribute(proposal_id, FeedbackSignal.reject)
    return {}


async def route_rerun(state: dict, config: RunnableConfig) -> dict:
    """Reject the prior proposal, then re-invoke the ideation graph with the
    prior activity excluded."""
    deps = _deps(config).resolved()
    proposal_id = state.get("proposal_id")

    prior_activity_id: int | None = None
    if proposal_id:
        proposal = await deps.proposal_store.get(proposal_id)
        if proposal is not None:
            prior_activity_id = proposal.activity_id
            await deps.proposal_store.set_status(proposal_id, ProposalStatus.rejected)
            await deps.feedback_store.log(proposal_id, FeedbackSignal.reject)
            # Attribute the rerun signal to the activity's tags (implicit
            # track, dampened strength — "rerun ≠ reject").
            attribution = FeedbackAttribution(deps.db)
            await attribution.attribute(proposal_id, FeedbackSignal.rerun)

    # Re-invoke the ideation graph (imported lazily to avoid import cycles).
    from app.agent.ideation_graph import ideation_graph

    now = datetime.now(timezone.utc)
    ideation_input: dict = {
        "couple_id": state["couple_id"],
        "window_start": now,
        "window_end": now + timedelta(days=7),
        "on_demand": True,
        "min_duration_min": 60,
        "exclude_activity_id": prior_activity_id,
    }
    result = await ideation_graph.ainvoke(
        ideation_input,
        {"configurable": {"deps": deps}},
    )

    return {
        "proposal": result.get("proposal"),
        "draft": result.get("draft"),
        "sms_copy": result.get("sms_copy"),
        "delivery_results": result.get("delivery_results"),
    }


async def route_stop(state: dict, config: RunnableConfig) -> dict:
    """Mute suggestions for the couple and log implicit feedback (mute)."""
    deps = _deps(config).resolved()
    if state.get("couple_id"):
        await deps.couple_store.set_muted(state["couple_id"], muted=True)
    if state.get("proposal_id"):
        await deps.feedback_store.log(state["proposal_id"], FeedbackSignal.mute)
    return {}


async def route_rating(state: dict, config: RunnableConfig) -> dict:
    """Handle a rating reply (1-5 or SKIP) to a past confirmed proposal.

    Parses the rating digit, logs it via ``feedback_store.log_rating``,
    and attributes the signal to the activity's tags via
    ``feedback_attribution.attribute_rating``.

    A ``SKIP`` reply records a ``None`` rating so the scheduler does not
    re-prompt for this proposal.
    """
    deps = _deps(config).resolved()
    proposal_id = state.get("proposal_id")
    rating = state.get("rating_parsed")

    if proposal_id is None:
        return {"errors": ["route_rating: no proposal_id in state"]}

    # Convert SKIP to None rating for persistence.
    numeric_rating: int | None = rating if isinstance(rating, int) else None

    await deps.feedback_store.log_rating(proposal_id, numeric_rating)

    # Attribute the rating signal to the activity's tags (explicit track).
    attribution = FeedbackAttribution(deps.db)
    updated_tags = await attribution.attribute_rating(proposal_id, numeric_rating)

    logger.info(
        "route_rating: proposal %d rating=%s tags_updated=%s",
        proposal_id,
        numeric_rating,
        updated_tags,
    )

    return {}


async def edit_proposal(state: dict, config: RunnableConfig) -> dict:
    """Agentic node: interpret a freeform reply into a constrained ProposalEdit.

    If the LLM does not call the tool (nothing maps to a supported field), the
    node falls back to clarification rather than letting a bogus edit through.
    """
    deps = _deps(config).resolved()
    if deps.llm is None:
        raise RuntimeError(
            "edit_proposal requires deps.llm (an LLM). Set ANTHROPIC_API_KEY "
            "and inject a model, or pass a mock in tests."
        )

    proposal_id = state.get("proposal_id")
    proposal = (
        await deps.proposal_store.get(proposal_id) if proposal_id else None
    )
    if proposal is None:
        return {
            "edit": None,
            "needs_clarification": True,
            "clarification_msg": "There's no pending proposal to edit.",
        }

    proposal_json = json.dumps(_proposal_to_dict(proposal), indent=2)
    llm_with_tool = deps.llm.bind_tools([ProposalEdit])

    resp = await llm_with_tool.ainvoke(
        [
            SystemMessage(content=EDIT_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"Current proposal:\n{proposal_json}\n\n"
                    f"User's SMS reply:\n{state['raw_body']}"
                )
            ),
        ]
    )

    tool_calls = getattr(resp, "tool_calls", None) or []
    if not tool_calls:
        return {
            "edit": None,
            "needs_clarification": True,
            "clarification_msg": (
                "I can only change the time, the activity, or the duration. "
                "Try something like '9pm instead' or 'something shorter'."
            ),
        }

    try:
        edit = ProposalEdit(**tool_calls[0]["args"])
    except Exception as exc:
        logger.warning("Invalid ProposalEdit args: %s", exc)
        return {
            "edit": None,
            "needs_clarification": True,
            "clarification_msg": "I didn't quite understand that — could you rephrase?",
        }

    return {"edit": edit}


async def validate_edit(state: dict, config: RunnableConfig) -> dict:
    """Re-run the overlap check for any new start time before persisting.

    Not optional: a freeform "let's do 9pm instead" can name a time neither
    calendar actually has free.  On success, applies the edit to the proposal
    row and builds a draft for the shared compose_proposal/deliver_sms nodes.
    """
    deps = _deps(config).resolved()

    edit = state.get("edit")
    if edit is None:
        return {
            "edit_valid": False,
            "needs_clarification": True,
            "clarification_msg": state.get("clarification_msg")
            or "I didn't catch that — please rephrase.",
        }
    if edit.is_noop():
        return {
            "edit_valid": False,
            "needs_clarification": True,
            "clarification_msg": (
                "I understood your message but couldn't map it to a change. "
                "I can change the time, activity, or duration."
            ),
        }

    proposal = (
        await deps.proposal_store.get(state["proposal_id"])
        if state.get("proposal_id")
        else None
    )
    if proposal is None:
        return {
            "edit_valid": False,
            "needs_clarification": True,
            "clarification_msg": "There's no pending proposal to edit.",
        }

    # Resolve the new schedule.
    new_start = edit.new_start_time or proposal.proposed_start
    duration = edit.duration_override_min or _proposal_duration(proposal)
    duration = max(duration, 60)
    new_end = new_start + timedelta(minutes=duration)

    # Validate the new start time against both calendars (only when changed).
    if edit.new_start_time is not None:
        ok, reason = await _slot_free(
            deps, state["couple_id"], new_start, duration
        )
        if not ok:
            return {
                "edit_valid": False,
                "needs_clarification": True,
                "clarification_msg": (
                    f"That time isn't free for both of you ({reason}). "
                    "Try another time."
                ),
            }

    updated = await deps.proposal_store.update(
        proposal.id,
        ProposalUpdate(
            activity_id=edit.new_activity_id,
            proposed_start=new_start,
            proposed_end=new_end,
        ),
    )
    if updated is None:
        return {
            "edit_valid": False,
            "needs_clarification": True,
            "clarification_msg": "I couldn't update the proposal — try again.",
        }

    # Build a draft for the shared compose_proposal / deliver_sms nodes.
    activity = await deps.catalog.get_by_id(deps.db, updated.activity_id)
    if activity is None:
        return {
            "edit_valid": False,
            "needs_clarification": True,
            "clarification_msg": "The requested activity no longer exists.",
        }

    draft = ProposalDraft(
        activity_id=updated.activity_id,
        activity_name=activity.name,
        description=activity.description or "",
        start=new_start,
        end=new_end,
        duration_min=duration,
    )

    return {
        "edit_valid": True,
        "proposal": _proposal_to_dict(updated),
        "draft": draft,
    }


def _route_after_validate(
    state: dict,
) -> Literal["compose_proposal", "send_clarification"]:
    """Route: valid edit → recompose + deliver; invalid → clarify."""
    return "compose_proposal" if state.get("edit_valid") else "send_clarification"


# =========================================================================
# Graph builder
# =========================================================================


def build_sms_graph():
    """Build and compile the inbound SMS graph."""
    graph = StateGraph(SMSState)

    graph.add_node("classify_intent", classify_intent)
    graph.add_node("route_yes", route_yes)
    graph.add_node("route_no", route_no)
    graph.add_node("route_rerun", route_rerun)
    graph.add_node("route_stop", route_stop)
    graph.add_node("route_rating", route_rating)
    graph.add_node("edit_proposal", edit_proposal)
    graph.add_node("validate_edit", validate_edit)
    graph.add_node("send_clarification", send_clarification)
    graph.add_node("compose_proposal", compose_proposal)
    graph.add_node("deliver_sms", deliver_sms)

    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges(
        "classify_intent",
        _route_on_intent,
        {
            "route_yes": "route_yes",
            "route_no": "route_no",
            "route_rerun": "route_rerun",
            "route_stop": "route_stop",
            "route_rating": "route_rating",
            "edit_proposal": "edit_proposal",
            "send_clarification": "send_clarification",
        },
    )
    graph.add_edge("route_yes", END)
    graph.add_edge("route_no", END)
    graph.add_edge("route_rerun", END)
    graph.add_edge("route_stop", END)
    graph.add_edge("route_rating", END)
    graph.add_edge("edit_proposal", "validate_edit")
    graph.add_conditional_edges(
        "validate_edit",
        _route_after_validate,
        {
            "compose_proposal": "compose_proposal",
            "send_clarification": "send_clarification",
        },
    )
    graph.add_edge("compose_proposal", "deliver_sms")
    graph.add_edge("deliver_sms", END)
    graph.add_edge("send_clarification", END)

    return graph.compile()


sms_graph = build_sms_graph()


# =========================================================================
# Internal helpers
# =========================================================================


def _proposal_duration(proposal) -> int:
    """Duration of a proposal row in minutes."""
    return int((proposal.proposed_end - proposal.proposed_start).total_seconds() // 60)


async def _slot_free(
    deps, couple_id: int, start: datetime, duration_min: int
) -> tuple[bool, str]:
    """Check that [start, start+duration_min) is free on both calendars."""
    end = start + timedelta(minutes=duration_min)
    # Widen the probe range to catch adjacent busy blocks that overlap.
    probe_start = start - timedelta(hours=2)
    probe_end = end + timedelta(hours=2)

    for rc in await deps.calendar_resolver.get_active_adapters(deps.db, couple_id):
        blocks = await rc.adapter.get_busy_blocks(probe_start, probe_end)
        for b in blocks:
            if b.start < end and b.end > start:
                return False, (
                    f"your calendar is busy {b.start.isoformat()}–{b.end.isoformat()}"
                )
    return True, ""