"""Shared node functions and helpers for both LangGraph agents.

Contains:
- Availability helpers (free_blocks, intersect_windows, rank_windows)
- ``compose_proposal`` — writes the proposal row + formats SMS
- ``deliver_sms`` — sends via the injected SMS gateway
- ``format_proposal_sms`` — generates the SMS text per recipient timezone

These are imported by both ``ideation_graph.py`` and ``sms_graph.py`` so that
the inbound SMS graph reuses the exact same serialisation and delivery logic
without duplicating nodes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig
from zoneinfo import ZoneInfo

from app.agent.deps import GraphDeps, _deps
from app.agent.state import OverlapWindow, ProposalDraft
from app.models import ProposalStatus
from app.schemas.proposal import ProposalUpdate

logger = logging.getLogger(__name__)

# =========================================================================
# Availability helpers
# =========================================================================


def free_blocks(
    range_start: datetime,
    range_end: datetime,
    busy: list[dict],
) -> list[dict]:
    """Compute free time blocks within [range_start, range_end) given busy periods.

    Busy blocks are expected to have ``start`` / ``end`` keys with UTC
    timezone-aware datetime values.  Returns a list of ``{start, end}`` dicts
    sorted chronologically.
    """
    sorted_busy = sorted(
        [b for b in busy if b.get("start") and b.get("end")],
        key=lambda x: x["start"],
    )
    free: list[dict] = []
    cursor = range_start
    for b in sorted_busy:
        b_start = b["start"]
        b_end = b["end"]

        # Skip busy blocks entirely before the cursor
        if b_end <= cursor:
            continue
        # Gap between cursor and this busy block → free
        if b_start > cursor:
            free.append(
                {"start": cursor, "end": min(b_start, range_end)}
            )
        cursor = max(cursor, b_end)
        if cursor >= range_end:
            break
    # Remaining free after the last busy block
    if cursor < range_end:
        free.append({"start": cursor, "end": range_end})
    return free


def intersect_windows(
    free_a: list[dict],
    free_b: list[dict],
    min_duration_min: int = 60,
) -> list[OverlapWindow]:
    """Intersect two free-block lists and return overlapping windows.

    Each window is ``OverlapWindow`` with ``start``, ``end``, ``duration_min``.
    Only windows >= ``min_duration_min`` are kept.
    """
    windows: list[OverlapWindow] = []
    for a in free_a:
        for b in free_b:
            s = max(a["start"], b["start"])
            e = min(a["end"], b["end"])
            if e > s:
                duration_min = int((e - s).total_seconds() // 60)
                if duration_min >= min_duration_min:
                    windows.append(OverlapWindow(start=s, end=e, duration_min=duration_min))
    return windows


def rank_windows(
    windows: list[OverlapWindow],
    *,
    on_demand: bool,
    now: datetime | None = None,
) -> list[OverlapWindow]:
    """Rank windows, best first.

    For on-demand, windows within the next 5 days are ranked highest, then
    later windows.  For scheduled, simply sort by start time ascending.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    def _score(w: OverlapWindow) -> tuple:
        days_out = (w.start - now).days
        if on_demand:
            # Primary: soonness (lower is better), capped at 5 days
            soon_score = 0 if days_out <= 5 else 1
            # Secondary: start time (earlier is better)
            return (soon_score, w.start)
        # Scheduled: just start time
        return (0, w.start)

    windows.sort(key=_score)
    return windows


# =========================================================================
# Activity selection helpers
# =========================================================================

IDEATE_SYSTEM_PROMPT = """\
You are a long-distance date planner. Your job is to find a date activity that \
a specific couple will enjoy, given their traits and available time windows.

## Workflow (must follow this order)

1. **Call catalog_search** — Build a search query from the couple's top-weighted \
traits and the available window length. Review the results' similarity scores.

2. **Evaluate catalog results** — If the top result has similarity >= 0.75 AND \
fits the available duration, select it. You are done — skip to step 4.

3. **If no catalog result is good enough** (top similarity < 0.75 or none fit the \
duration), call **web_search** for fresh ideas. From the results, identify a \
genuinely novel activity. If it's novel, call **add_to_catalog** to store it \
(embedding + dedup check happens automatically). Use the returned id.

4. **Output your selection** as a JSON object (no markdown, no extra text):

```json
{
  "activity_id": <int or null>,
  "name": "<activity name>",
  "description": "<brief description>",
  "est_duration_min": <estimated duration in minutes, at least 60>,
  "source": "seed" | "llm",
  "novel": <true if added to catalog, false otherwise>
}
```

## Constraints
- Minimum activity duration is 60 minutes.
- The activity must fit within the longest available window.
- If the couple has an excluded activity id, do NOT propose that activity.
- Never propose the same activity twice in a row for the same couple.
"""

EDIT_SYSTEM_PROMPT = """\
You are helping a user edit a pending date proposal via SMS. \
Given the current proposal details and the user's freeform text reply, \
determine what the user wants to change.

Call the ProposalEdit tool with ONLY the fields the user explicitly wants to change. \
If the user's message doesn't map to any supported field (time, activity, duration), \
do NOT call the tool — the system will ask for clarification.

Supported fields:
- new_start_time: if the user mentions a different time or day
- new_activity_id: if the user wants a different activity (use the id from the proposal)
- duration_override_min: if the user wants a shorter or longer duration
- reasoning: required — explain why you chose these changes

If the user's message is ambiguous or doesn't match any field, do not call the tool.
"""


# =========================================================================
# SMS formatting
# =========================================================================


def format_proposal_sms(
    draft: ProposalDraft,
    *,
    local_tz_name: str,
    partner_name: str | None = None,
) -> str:
    """Format the SMS text for a proposal, localised to a recipient's timezone.

    Parameters
    ----------
    draft:
        The proposal draft (activity, start, end, etc.).
    local_tz_name:
        IANA timezone name (e.g. ``"America/New_York"``) for the recipient.
    partner_name:
        Optional sender/partner name for personalisation.

    Returns
    -------
    The formatted SMS body (plain text, ~160 characters).
    """
    tz = ZoneInfo(local_tz_name)
    local_start = draft.start.astimezone(tz)
    local_end = draft.end.astimezone(tz)

    time_str = local_start.strftime("%A %b %d at %I:%M %p")
    end_str = local_end.strftime("%I:%M %p")

    greeting = f"Hey {partner_name}! " if partner_name else ""
    return (
        f"{greeting}Date idea: {draft.activity_name} on {time_str}–{end_str} "
        f"({draft.duration_min} min). Reply YES, NO, RERUN, or STOP."
    )


def format_clarification_sms(msg: str) -> str:
    """Format a clarification/follow-up SMS (edit path fallback)."""
    return f"Sorry, I didn't understand that. {msg} Reply YES, NO, RERUN, or STOP."


def format_confirmation_sms(
    activity_name: str,
    local_start: datetime,
    local_end: datetime,
    local_tz_name: str,
    partner_name: str | None = None,
) -> str:
    """Format the notification sent to the *other* partner when someone confirms."""
    tz = ZoneInfo(local_tz_name)
    start_local = local_start.astimezone(tz)
    time_str = start_local.strftime("%A %b %d at %I:%M %p")
    greeting = f"Hey {partner_name}! " if partner_name else ""
    return (
        f"{greeting}{partner_name or 'Your partner'} confirmed: "
        f"{activity_name} on {time_str}. It's on the calendar! 🎉"
    )


# =========================================================================
# Shared nodes
# =========================================================================


def _proposal_to_dict(proposal: Any) -> dict:
    """Serialise a Proposal ORM instance to a plain dict."""
    return {
        "id": proposal.id,
        "couple_id": proposal.couple_id,
        "activity_id": proposal.activity_id,
        "proposed_start": proposal.proposed_start.isoformat(),
        "proposed_end": proposal.proposed_end.isoformat(),
        "status": proposal.status.value if hasattr(proposal.status, "value") else str(proposal.status),
        "confirmed_by": proposal.confirmed_by,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
    }


async def compose_proposal(state: dict, config: RunnableConfig) -> dict:
    """Write the proposal row and format the SMS copy.

    Reads ``draft`` from state.  If ``proposal_id`` is already set (edit path),
    updates the existing row; otherwise creates a new pending proposal.

    Short-circuits if required data is missing (e.g. upstream node errored).
    """
    deps: GraphDeps = _deps(config).resolved()

    # Build the draft if the caller didn't set one (ideation path).
    draft = state.get("draft")
    if draft is None:
        try:
            draft = _draft_from_ideation(state)
        except ValueError as exc:
            return {"errors": [f"compose_proposal: {exc}"]}

    # Persist
    if state.get("proposal_id"):
        proposal = await deps.proposal_store.update(
            state["proposal_id"],
            ProposalUpdate(
                activity_id=draft.activity_id,
                proposed_start=draft.start,
                proposed_end=draft.end,
            ),
        )
        if proposal is None:
            return {"errors": [f"Proposal {state['proposal_id']} not found for update"]}
    else:
        proposal = await deps.proposal_store.create_pending(
            couple_id=state["couple_id"],
            activity_id=draft.activity_id,
            proposed_start=draft.start,
            proposed_end=draft.end,
        )

    proposal_dict = _proposal_to_dict(proposal)

    # Format SMS (base copy in UTC — deliver_sms will localise per recipient)
    sms_copy = format_proposal_sms(draft, local_tz_name="UTC")

    return {
        "proposal": proposal_dict,
        "sms_copy": sms_copy,
        "draft": draft,
    }


def _draft_from_ideation(state: dict) -> ProposalDraft:
    """Build a ProposalDraft from ideation-path state (activity + window)."""
    activity = state.get("selected_activity")
    window = state.get("selected_window")
    if not activity or not window:
        raise ValueError(
            "compose_proposal needs a draft, or selected_activity + selected_window"
        )
    activity_id = activity.get("activity_id")
    if not activity_id:
        raise ValueError(
            "selected_activity has no activity_id — cannot create a proposal"
        )
    duration = max(activity.get("est_duration_min") or 60, 60)
    return ProposalDraft(
        activity_id=int(activity_id),
        activity_name=activity["name"],
        description=activity.get("description") or "",
        start=window.start,
        end=window.start + timedelta(minutes=duration),
        duration_min=duration,
    )


async def deliver_sms(state: dict, config: RunnableConfig) -> dict:
    """Send the proposal SMS to both partners and record results.

    Localises the time per recipient's timezone.  Uses the ``SMSGateway``
    from deps.  Short-circuits if no draft is available (upstream error).
    """
    deps: GraphDeps = _deps(config).resolved()
    gateway = deps.sms_gateway

    draft = state.get("draft")
    if draft is None:
        return {"errors": ["deliver_sms: no draft in state — upstream node likely failed"]}

    # Resolve both partners
    couple = await deps.couple_store.get_couple(state["couple_id"])
    if couple is None:
        return {"errors": [f"Couple {state['couple_id']} not found"]}

    users = await deps.couple_store.partner_users(couple)
    if not users:
        return {"errors": ["No partners found for couple"]}

    results: list[dict] = []
    for user in users:
        local_body = format_proposal_sms(
            draft,
            local_tz_name=user.timezone or "UTC",
            partner_name=user.name.split()[0] if user.name else None,
        )
        try:
            sid = await gateway.send(to_phone=user.phone_number, body=local_body)
        except Exception as exc:
            err = f"SMS delivery failed to {user.phone_number}: {exc}"
            logger.warning(err)
            results.append({
                "user_id": user.id,
                "to": user.phone_number,
                "error": str(exc),
            })
            continue

        results.append({
            "user_id": user.id,
            "to": user.phone_number,
            "sid": sid,
            "body": local_body,
        })

    return {"delivery_results": results}


async def send_clarification(state: dict, config: RunnableConfig) -> dict:
    """Send a clarification SMS to the sender (edit path fallback)."""
    deps: GraphDeps = _deps(config).resolved()
    gateway = deps.sms_gateway

    msg = state.get("clarification_msg", "I didn't understand that request.")
    body = format_clarification_sms(msg)

    # Resolve the sender's phone number
    user = await deps.couple_store.get_user(state["user_id"])
    if user is None:
        return {"errors": [f"User {state['user_id']} not found for clarification"]}

    try:
        sid = await gateway.send(to_phone=user.phone_number, body=body)
    except Exception as exc:
        logger.warning("Clarification SMS failed to %s: %s", user.phone_number, exc)
        return {"delivery_results": [{"error": str(exc)}], "clarification_sent": False}

    return {
        "delivery_results": [{"user_id": user.id, "to": user.phone_number, "sid": sid, "body": body}],
        "clarification_sent": True,
    }