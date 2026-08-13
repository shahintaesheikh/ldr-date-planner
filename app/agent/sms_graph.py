"""Inbound SMS graph — single Twilio webhook entry, keyword and NL paths.

Pipeline::

    classify_intent → route (keyword path: YES/NO/RERUN/STOP/MUTE/UNMUTE)
                   └→ edit_proposal → validate_edit → compose_proposal → deliver_sms
                                              └→ send_clarification (fallback)

``classify_intent`` fast-paths exact keywords via regex and routes everything
else to the NL edit path. MUTE/UNMUTE are SMS keywords (replacing the
deprecated web-app mute/unmute button) that flip the couple's
``suggestions_muted`` flag. Only ``edit_proposal`` (ProposalEdit tool) is
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
from app import settings
from app.models import (
    FeedbackSignal,
    OnboardingStep,
    ProposalStatus,
    TraitSource,
)
from app.schemas.proposal import ProposalUpdate
from app.schemas.trait import TraitCreate
from app.services.feedback_attribution import FeedbackAttribution

logger = logging.getLogger(__name__)

# =========================================================================
# Keyword fast-path patterns (deterministic-first intent classification)
# =========================================================================

_RERUN_RE = re.compile(
    r"\b(rerun|try again|something else|another one|different idea|"
    r"new idea|again|run|ideate|propose|new date|date idea|"
    r"plan a date|suggest a date|give me an idea|give us an idea)\b",
    re.IGNORECASE,
)
_STOP_RE = re.compile(
    r"\b(stop)\b", re.IGNORECASE
)


_MUTE_RE = re.compile(
    r"\b(mute|quiet|enough|unsubscribe|turn off)\b", re.IGNORECASE
)


_UNMUTE_RE = re.compile(
    r"\b(unmute|un-mute|start again|resume|turn on|unquiet)\b", re.IGNORECASE
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

    Priority: ONBOARDING > RERUN > UNMUTE > MUTE > STOP > YES > NO > EDIT.
    A reply like "no, try again" routes to RERUN, not NO.
    RUN-style triggers ("run", "ideate", "new date", …) are aliases for
    RERUN — they all route to ``route_rerun``, which invokes the ideation
    graph: fresh when no proposal is pending, reject-and-rerun otherwise.

    ``ONBOARDING`` is returned for JOIN/START/SIGN UP keywords regardless of
    whether the user is already known — the ``onboarding_node`` handles the
    distinction (see ``.pi/sms-auth.md``).
    """
    body = raw_body.strip().lower()
    if not body:
        return "EDIT"

    # Onboarding keywords (checked before user resolution in classify_intent).
    if body in ("join", "start", "sign up"):
        return "ONBOARDING"

    if _RERUN_RE.search(body):
        return "RERUN"
    if _UNMUTE_RE.search(body):
        return "UNMUTE"
    if _MUTE_RE.search(body):
        return "MUTE"
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
    """Resolve sender + proposal, append to the sms_thread, and classify intent.

    Onboarding flow (``.pi/sms-auth.md``):

    1. If the raw body is a JOIN/START/SIGN UP keyword, always route to
       ``onboarding_node`` regardless of whether the user is known.
    2. If the sender is unknown (no user row), check for an existing
       onboarding session — if one exists, route to ``onboarding_node``
       (this handles partner confirmation replies like "YES" from Bob).
    3. Otherwise, return UNKNOWN (no user, no session).
    """
    try:
        deps = _deps(config).resolved()

        # Step 1: Onboarding keywords always go to onboarding_node.
        body = state["raw_body"].strip().lower()
        if body in ("join", "start", "sign up"):
            return {"intent": "ONBOARDING"}

        user = await deps.couple_store.get_user_by_phone(state["from_phone"])
        if user is None:
            # Step 2: Unknown sender with an active session → onboarding_node.
            session = await deps.onboarding_store.get_by_phone(
                state["from_phone"]
            )
            if session is not None:
                return {"intent": "ONBOARDING"}
            # Step 3: Truly unknown.
            return {
                "intent": "UNKNOWN",
                "needs_clarification": True,
                "clarification_msg": "I don't recognise that phone number.",
                "errors": [f"No user found for phone {state['from_phone']}"],
            }

        # Known user mid-onboarding (has a user row but no couple yet).
        session = await deps.onboarding_store.get_by_phone(
            state["from_phone"]
        )
        if session is not None and session.step != OnboardingStep.complete:
            return {"intent": "ONBOARDING"}

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
    except Exception as exc:
        logger.exception("Error in classify_intent: %s", exc)
        return {
            "intent": "UNKNOWN",
            "needs_clarification": True,
            "clarification_msg": "Sorry, something went wrong processing your message.",
            "errors": state.get("errors", []) + [f"classify_intent: {exc}"],
        }


def _route_on_intent(
    state: dict,
) -> Literal[
    "route_yes", "route_no", "route_rerun", "route_stop", "route_mute",
    "route_unmute", "route_rating", "edit_proposal", "onboarding_node",
    "send_clarification"
]:
    """Conditional router after classify_intent."""
    return {
        "YES": "route_yes",
        "NO": "route_no",
        "RERUN": "route_rerun",
        "STOP": "route_stop",
        "MUTE": "route_mute",
        "UNMUTE": "route_unmute",
        "RATING": "route_rating",
        "EDIT": "edit_proposal",
        "ONBOARDING": "onboarding_node",
        "UNKNOWN": "send_clarification",
    }.get(state.get("intent"), "send_clarification")


async def route_yes(state: dict, config: RunnableConfig) -> dict:
    """Lock the proposal, write calendar events to both calendars, notify the
    other partner, and log implicit feedback (accept)."""
    try:
        deps = _deps(config).resolved()
        proposal_id = state.get("proposal_id")
        user_id = state.get("user_id")

        proposal = (
            await deps.proposal_store.get(proposal_id) if proposal_id else None
        )
        if proposal is None:
            return {"errors": state.get("errors", []) + ["route_yes: no pending proposal to confirm"]}

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
        active_adapters = await deps.calendar_resolver.get_active_adapters(
            deps.db, state["couple_id"]
        )
        for rc in active_adapters:
            await rc.adapter.create_event(
                start=proposal.proposed_start,
                end=proposal.proposed_end,
                title=title,
                description="Long-distance date (confirmed via SMS)",
            )

        # Persist any refreshed OAuth tokens after calendar access.
        await deps.calendar_resolver.persist_tokens(deps.db, active_adapters)

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
    except Exception as exc:
        logger.exception("Error in route_yes: %s", exc)
        return {"errors": state.get("errors", []) + [f"route_yes: {exc}"]}


async def route_no(state: dict, config: RunnableConfig) -> dict:
    """Reject the proposal and log implicit feedback (reject)."""
    try:
        deps = _deps(config).resolved()
        proposal_id = state.get("proposal_id")
        if proposal_id:
            await deps.proposal_store.set_status(proposal_id, ProposalStatus.rejected)
            await deps.feedback_store.log(proposal_id, FeedbackSignal.reject)
            # Attribute the reject signal to the activity's tags (implicit track).
            attribution = FeedbackAttribution(deps.db)
            await attribution.attribute(proposal_id, FeedbackSignal.reject)
        return {}
    except Exception as exc:
        logger.exception("Error in route_no: %s", exc)
        return {"errors": state.get("errors", []) + [f"route_no: {exc}"]}


async def route_rerun(state: dict, config: RunnableConfig) -> dict:
    """Reject the prior proposal, then re-invoke the ideation graph with the
    prior activity excluded."""
    try:
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
    except Exception as exc:
        logger.exception("Error in route_rerun: %s", exc)
        return {"errors": state.get("errors", []) + [f"route_rerun: {exc}"]}


async def route_stop(state: dict, config: RunnableConfig) -> dict:
    """Handle STOP keyword — reject the current proposal (no mute)."""
    try:
        deps = _deps(config).resolved()
        if state.get("proposal_id"):
            await deps.proposal_store.set_status(
                state["proposal_id"], ProposalStatus.rejected
            )
        return {}
    except Exception as exc:
        logger.exception("Error in route_stop: %s", exc)
        return {"errors": state.get("errors", []) + [f"route_stop: {exc}"]}


async def route_mute(state: dict, config: RunnableConfig) -> dict:
    """MUTE keyword — mute suggestions for the couple and log feedback."""
    try:
        deps = _deps(config).resolved()
        if state.get("couple_id"):
            await deps.couple_store.set_muted(state["couple_id"], muted=True)
        if state.get("proposal_id"):
            await deps.feedback_store.log(state["proposal_id"], FeedbackSignal.mute)
        return {}
    except Exception as exc:
        logger.exception("Error in route_mute: %s", exc)
        return {"errors": state.get("errors", []) + [f"route_mute: {exc}"]}


async def route_unmute(state: dict, config: RunnableConfig) -> dict:
    """UNMUTE keyword — unmute suggestions for the couple."""
    try:
        deps = _deps(config).resolved()
        if state.get("couple_id"):
            await deps.couple_store.set_muted(state["couple_id"], muted=False)
        return {}
    except Exception as exc:
        logger.exception("Error in route_unmute: %s", exc)
        return {"errors": state.get("errors", []) + [f"route_unmute: {exc}"]}


async def route_rating(state: dict, config: RunnableConfig) -> dict:
    """Handle a rating reply (1-5 or SKIP) to a past confirmed proposal.

    Parses the rating digit, logs it via ``feedback_store.log_rating``,
    and attributes the signal to the activity's tags via
    ``feedback_attribution.attribute_rating``.

    A ``SKIP`` reply records a ``None`` rating so the scheduler does not
    re-prompt for this proposal.
    """
    try:
        deps = _deps(config).resolved()
        proposal_id = state.get("proposal_id")
        rating = state.get("rating_parsed")

        if proposal_id is None:
            return {"errors": state.get("errors", []) + ["route_rating: no proposal_id in state"]}

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
    except Exception as exc:
        logger.exception("Error in route_rating: %s", exc)
        return {"errors": state.get("errors", []) + [f"route_rating: {exc}"]}


async def edit_proposal(state: dict, config: RunnableConfig) -> dict:
    """Agentic node: interpret a freeform reply into a constrained ProposalEdit.

    If the LLM does not call the tool (nothing maps to a supported field), the
    node falls back to clarification rather than letting a bogus edit through.
    """
    try:
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

        # Invoke agent with sms reply context and current proposal
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
    except Exception as exc:
        logger.exception("Error in edit_proposal: %s", exc)
        return {
            "edit": None,
            "needs_clarification": True,
            "clarification_msg": "Sorry, something went wrong while editing.",
            "errors": state.get("errors", []) + [f"edit_proposal: {exc}"],
        }


async def validate_edit(state: dict, config: RunnableConfig) -> dict:
    """Re-run the overlap check for any new start time before persisting.

    Not optional: a freeform "let's do 9pm instead" can name a time neither
    calendar actually has free.  On success, applies the edit to the proposal
    row and builds a draft for the shared compose_proposal/deliver_sms nodes.
    """
    try:
        deps = _deps(config).resolved()

        # Get proposed edit that will show up in the state; check if it exists and makes sense
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
    except Exception as exc:
        logger.exception("Error in validate_edit: %s", exc)
        return {
            "edit_valid": False,
            "needs_clarification": True,
            "clarification_msg": "Sorry, something went wrong while validating your edit.",
            "errors": state.get("errors", []) + [f"validate_edit: {exc}"],
        }


def _route_after_validate(
    state: dict,
) -> Literal["compose_proposal", "send_clarification"]:
    """Route: valid edit → recompose + deliver; invalid → clarify."""
    return "compose_proposal" if state.get("edit_valid") else "send_clarification"


# =========================================================================
# Onboarding node
# =========================================================================


async def onboarding_node(state: dict, config: RunnableConfig) -> dict:
    """SMS-native onboarding flow (``.pi/sms-auth.md``).

    A single deterministic node (not a sub-graph) that reads the
    ``onboarding_sessions`` row by ``from_phone`` and switch-cases on
    ``session.step``.  Sends SMS replies directly via ``deps.sms_gateway``.

    The node handles all 9 onboarding steps, from welcome (``await_name``)
    through partner confirmation, calendar choice, and traits, to
    ``complete``.
    """
    try:
        deps = _deps(config).resolved()
        gateway = deps.sms_gateway
        phone = state["from_phone"]
        body = state["raw_body"].strip()

        session = await deps.onboarding_store.get_by_phone(phone)

        # ── No session yet → create one and start onboarding ──
        if session is None:
            session = await deps.onboarding_store.create(
                phone_number=phone,
                step=OnboardingStep.await_name,
            )
            await gateway.send(
                phone,
                "Welcome to LDR Date Planner! What's your name?",
            )
            return {}

        # Read the accumulated data.
        data = dict(session.data or {})

        # ── Switch on session step ──
        step = session.step

        # ── Resume: user texts JOIN/START/SIGN UP when session already exists ──
        # Re-prompt for the current step instead of treating the keyword as
        # the answer to the current question.
        resume_keywords = {"join", "start", "sign up"}
        if body.lower().strip() in resume_keywords:
            await gateway.send(
                phone,
                _resume_prompt(step, data),
            )
            return {}

        if step == OnboardingStep.await_name:
            # Store name, create user, ask for partner's phone.
            name = body
            user = await deps.couple_store.create_user(
                name=name, phone_number=phone
            )
            await deps.onboarding_store.advance_step(
                phone,
                OnboardingStep.await_partner_phone,
                data_updates={"name": name, "user_id": user.id},
            )
            await gateway.send(
                phone,
                f"Nice to meet you, {name.split()[0]}! "
                f"What's your partner's phone number?",
            )

        elif step == OnboardingStep.await_partner_phone:
            # Store partner's phone, ask for partner's name.
            partner_phone = body
            await deps.onboarding_store.advance_step(
                phone,
                OnboardingStep.await_partner_name,
                data_updates={"partner_phone": partner_phone},
            )
            await gateway.send(
                phone,
                "And what's their name?",
            )

        elif step == OnboardingStep.await_partner_name:
            # Store partner's name, create partner's session, text partner.
            partner_name = body
            partner_phone = data.get("partner_phone", "")

            await deps.onboarding_store.advance_step(
                phone,
                OnboardingStep.await_partner_confirm,
                data_updates={"partner_name": partner_name},
            )

            # Create partner's onboarding session (await_partner_confirm).
            partner_session = await deps.onboarding_store.create(
                phone_number=partner_phone,
                step=OnboardingStep.await_partner_confirm,
                data={
                    "partner_name": data.get("name", ""),
                    "partner_phone": phone,
                    "partner_user_id": data.get("user_id"),
                },
            )

            # Text the partner.
            await gateway.send(
                partner_phone,
                f"{data.get('name', 'Someone')} wants to connect with you "
                f"on LDR Date Planner! Reply YES to confirm.",
            )

            # Tell the user they're on hold.
            await gateway.send(
                phone,
                f"📨 We've texted {partner_name} to confirm. "
                f"You're on hold until they reply.",
            )

        elif step == OnboardingStep.await_partner_confirm:
            # Partner is replying to the confirmation request.
            if body.lower() in ("yes", "yeah", "yep", "sure", "confirm"):
                # Resolve the initiator's user_id from stored data.
                # ``partner_user_id`` in this session is the initiator's
                # user_id (set when the partner's session was created).
                initiator_user_id = data.get("partner_user_id")
                initiator_phone = data.get("partner_phone", "")
                initiator_name = data.get("partner_name", "")

                # Create the current user (Bob) if they don't have an
                # account yet.
                current_user = await deps.couple_store.get_user_by_phone(phone)
                if current_user is None:
                    current_user = await deps.couple_store.create_user(
                        name=initiator_name or "Partner",
                        phone_number=phone,
                    )
                current_user_id = current_user.id

                if initiator_user_id is None:
                    await gateway.send(
                        phone,
                        "Sorry, something went wrong finding your partner. "
                        "Please try again later.",
                    )
                    return {
                        "errors": [
                            "onboarding_node: initiator_user_id not found"
                        ]
                    }

                # Create the couple (initiator is partner_a).
                await deps.couple_store.create_couple(
                    partner_a_user_id=initiator_user_id,
                    partner_b_user_id=current_user_id,
                )

                # Advance the current user's session to calendar choice.
                await deps.onboarding_store.advance_step(
                    phone,
                    OnboardingStep.await_calendar_choice,
                    data_updates={"user_id": current_user_id},
                )

                # Advance the initiator's session to calendar choice.
                initiator_session = await deps.onboarding_store.get_by_phone(
                    initiator_phone
                )
                if initiator_session is not None:
                    await deps.onboarding_store.advance_step(
                        initiator_phone,
                        OnboardingStep.await_calendar_choice,
                    )

                # Tell the current user they're connected.
                await gateway.send(
                    phone,
                    f"You're connected with {initiator_name or 'your partner'}! "
                    f"Do you use Google Calendar or Apple Calendar? "
                    f"Reply \"google\", \"apple\", or \"skip\" "
                    f"to do it later.",
                )

                # Notify the initiator.
                await gateway.send(
                    initiator_phone,
                    f"{initiator_name or 'Your partner'} confirmed! "
                    f"You're connected. "
                    f"Do you use Google Calendar or Apple Calendar? "
                    f"Reply \"google\", \"apple\", or \"skip\" "
                    f"to do it later.",
                )
            else:
                # Partner declined or sent something else.
                await gateway.send(
                    phone,
                    "That's okay, text JOIN when you're ready.",
                )

        elif step == OnboardingStep.await_calendar_choice:
            choice = body.lower().strip()

            if choice == "google":
                user_id = data.get("user_id")
                await deps.onboarding_store.advance_step(
                    phone,
                    OnboardingStep.await_google_done,
                )
                await gateway.send(
                    phone,
                    f"📱 Open this link in your browser to connect "
                    f"Google Calendar:\n"
                    f"{settings.app_base_url}/auth/google?user_id={user_id}\n"
                    f"Text DONE when you're finished.",
                )
            elif choice == "apple":
                await deps.onboarding_store.advance_step(
                    phone,
                    OnboardingStep.await_apple_email,
                )
                await gateway.send(
                    phone,
                    "Apple Calendar needs an app-specific password "
                    "(not your normal Apple password). To create one: "
                    "sign in at appleid.apple.com → Security → "
                    "App-Specific Passwords → generate one. Then text me "
                    "your Apple ID email.",
                )
            elif choice in ("skip", "none", "later"):
                await deps.onboarding_store.advance_step(
                    phone,
                    OnboardingStep.await_traits_activity,
                )
                await gateway.send(
                    phone,
                    "No problem — you can connect later. Now, what kind "
                    "of dates do you like? Reply numbers:\n"
                    "1 = Virtual tours, 2 = Games, 3 = Cooking, "
                    "4 = Movies, 5 = Outdoors",
                )
            else:
                await gateway.send(
                    phone,
                    "Please reply \"google\", \"apple\", or \"skip\".",
                )

        elif step == OnboardingStep.await_google_done:
            if body.lower() == "done":
                await deps.onboarding_store.advance_step(
                    phone,
                    OnboardingStep.await_traits_activity,
                )
                await gateway.send(
                    phone,
                    "Connected! 🎉 What kind of dates do you like? "
                    "Reply numbers:\n"
                    "1 = Virtual tours, 2 = Games, 3 = Cooking, "
                    "4 = Movies, 5 = Outdoors",
                )
            else:
                await gateway.send(
                    phone,
                    "Text DONE when you've finished in the browser.",
                )

        elif step == OnboardingStep.await_apple_email:
            email = body
            await deps.onboarding_store.advance_step(
                phone,
                OnboardingStep.await_apple_password,
                data_updates={"apple_email": email},
            )
            await gateway.send(
                phone,
                "Got it. Now text me the app-specific password "
                "(format: xxxx-xxxx-xxxx-xxxx).",
            )

        elif step == OnboardingStep.await_apple_password:
            password = body
            user_id = data.get("user_id")
            apple_email = data.get("apple_email", "")

            if user_id is None:
                await gateway.send(
                    phone,
                    "Sorry, something went wrong. Please start over "
                    "by texting JOIN.",
                )
                return {"errors": ["onboarding_node: user_id not found for apple_connect"]}

            from app.services.calendar_connector import connect_apple

            success, msg = await connect_apple(
                deps.db, user_id, apple_email, password
            )

            if success:
                await deps.onboarding_store.advance_step(
                    phone,
                    OnboardingStep.await_traits_activity,
                )
                await gateway.send(
                    phone,
                    "✅ Connected to Apple Calendar! "
                    "What kind of dates do you like? Reply numbers:\n"
                    "1 = Virtual tours, 2 = Games, 3 = Cooking, "
                    "4 = Movies, 5 = Outdoors",
                )
            else:
                await gateway.send(phone, msg)

        elif step == OnboardingStep.await_traits_activity:
            # Parse comma-separated numbers.
            selections = [
                s.strip() for s in body.split(",")
                if s.strip() in ("1", "2", "3", "4", "5")
            ]
            if not selections:
                await gateway.send(
                    phone,
                    "Please reply with numbers like \"1, 3, 4\" "
                    "from the list: 1 = Virtual tours, 2 = Games, "
                    "3 = Cooking, 4 = Movies, 5 = Outdoors",
                )
                return {}

            activity_map = {
                "1": "virtual_tours",
                "2": "games",
                "3": "cooking",
                "4": "movies",
                "5": "outdoors",
            }
            selected_activities = [
                activity_map[s] for s in selections
            ]

            await deps.onboarding_store.advance_step(
                phone,
                OnboardingStep.await_traits_energy,
                data_updates={"activity_prefs": selected_activities},
            )
            await gateway.send(
                phone,
                "How much energy for a date? "
                "1 = Low (chill), 2 = Medium, 3 = High",
            )

        elif step == OnboardingStep.await_traits_energy:
            energy = body.strip()
            if energy not in ("1", "2", "3"):
                await gateway.send(
                    phone,
                    "Please reply 1 (Low), 2 (Medium), or 3 (High).",
                )
                return {}

            energy_map = {"1": "low", "2": "medium", "3": "high"}
            energy_val = energy_map[energy]

            # Resolve the couple_id from the user.
            user_id = data.get("user_id")
            if user_id is None:
                await gateway.send(
                    phone,
                    "Sorry, something went wrong. Please start over "
                    "by texting JOIN.",
                )
                return {"errors": ["onboarding_node: user_id not found"]}

            couple = await deps.couple_store.get_couple_for_user(user_id)
            if couple is None:
                await gateway.send(
                    phone,
                    "Sorry, something went wrong. Please start over "
                    "by texting JOIN.",
                )
                return {"errors": ["onboarding_node: couple not found for user"]}

            couple_id = couple.id

            # Write traits via TraitStore.
            activity_prefs = data.get("activity_prefs", [])
            if activity_prefs:
                await deps.trait_store.upsert_trait(
                    couple_id=couple_id,
                    data=TraitCreate(
                        trait_key="activity_type_pref",
                        value=",".join(activity_prefs),
                        weight=1.0,
                        source=TraitSource.explicit,
                    ),
                )

            await deps.trait_store.upsert_trait(
                couple_id=couple_id,
                data=TraitCreate(
                    trait_key="energy_pref",
                    value=energy_val,
                    weight=1.0,
                    source=TraitSource.explicit,
                ),
            )

            await deps.onboarding_store.advance_step(
                phone,
                OnboardingStep.complete,
                data_updates={"energy_pref": energy_val},
            )
            await gateway.send(
                phone,
                "You're all set! We'll send you a date idea soon. 🎉",
            )

        elif step == OnboardingStep.complete:
            # No-op — session is done. Send a friendly reminder.
            await gateway.send(
                phone,
                "You're already set up! We'll send you a date idea soon. 🎉",
            )

        else:
            logger.warning(
                "onboarding_node: unknown step %s for phone %s",
                step, phone,
            )
            await gateway.send(
                phone,
                "Sorry, something went wrong. Please text JOIN to start over.",
            )

        return {}

    except Exception as exc:
        logger.exception("Error in onboarding_node: %s", exc)
        return {
            "errors": state.get("errors", []) + [f"onboarding_node: {exc}"]
        }


# =========================================================================
# Resume prompt helper
# =========================================================================


def _resume_prompt(step: OnboardingStep, data: dict) -> str:
    """Return an appropriate re-prompt message when a user texts JOIN to
    resume an interrupted onboarding session."""
    prompts = {
        OnboardingStep.await_name: (
            "Welcome back! Let's pick up where you left off. What's your name?"
        ),
        OnboardingStep.await_partner_phone: (
            "Welcome back! What's your partner's phone number?"
        ),
        OnboardingStep.await_partner_name: (
            "Welcome back! And what's their name?"
        ),
        OnboardingStep.await_partner_confirm: (
            "You're waiting for your partner to confirm. "
            "We'll let you know when they reply!"
        ),
        OnboardingStep.await_calendar_choice: (
            "Welcome back! Do you use Google Calendar or Apple Calendar? "
            "Reply \"google\", \"apple\", or \"skip\" to do it later."
        ),
        OnboardingStep.await_google_done: (
            "Welcome back! Did you finish connecting Google Calendar? "
            "Text DONE when you're finished."
        ),
        OnboardingStep.await_apple_email: (
            "Welcome back! Please text me your Apple ID email."
        ),
        OnboardingStep.await_apple_password: (
            "Welcome back! Please text me the app-specific password "
            "(format: xxxx-xxxx-xxxx-xxxx)."
        ),
        OnboardingStep.await_traits_activity: (
            "Welcome back! What kind of dates do you like? Reply numbers:\n"
            "1 = Virtual tours, 2 = Games, 3 = Cooking, "
            "4 = Movies, 5 = Outdoors"
        ),
        OnboardingStep.await_traits_energy: (
            "Welcome back! How much energy for a date? "
            "1 = Low (chill), 2 = Medium, 3 = High"
        ),
        OnboardingStep.complete: (
            "You're already set up! We'll send you a date idea soon. 🎉"
        ),
    }
    return prompts.get(
        step, "Welcome back! Text JOIN to start or continue."
    )


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
    graph.add_node("route_mute", route_mute)
    graph.add_node("route_unmute", route_unmute)
    graph.add_node("route_rating", route_rating)
    graph.add_node("edit_proposal", edit_proposal)
    graph.add_node("validate_edit", validate_edit)
    graph.add_node("onboarding_node", onboarding_node)
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
            "route_mute": "route_mute",
            "route_unmute": "route_unmute",
            "route_rating": "route_rating",
            "edit_proposal": "edit_proposal",
            "onboarding_node": "onboarding_node",
            "send_clarification": "send_clarification",
        },
    )
    graph.add_edge("route_yes", END)
    graph.add_edge("route_no", END)
    graph.add_edge("route_rerun", END)
    graph.add_edge("route_stop", END)
    graph.add_edge("route_mute", END)
    graph.add_edge("route_unmute", END)
    graph.add_edge("route_rating", END)
    graph.add_edge("onboarding_node", END)
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

    active_adapters = await deps.calendar_resolver.get_active_adapters(deps.db, couple_id)
    for rc in active_adapters:
        blocks = await rc.adapter.get_busy_blocks(probe_start, probe_end)
        for b in blocks:
            if b.start < end and b.end > start:
                return False, (
                    f"your calendar is busy {b.start.isoformat()}–{b.end.isoformat()}"
                )

    # Persist any refreshed OAuth tokens after calendar access.
    await deps.calendar_resolver.persist_tokens(deps.db, active_adapters)

    return True, ""