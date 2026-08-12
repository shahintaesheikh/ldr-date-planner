"""Cadence scheduler — a single AsyncIOScheduler managing both the rating
prompt and the weekly ideation-trigger jobs.

The rating check runs every 6 hours (same as the old standalone scheduler).
The cadence (ideation) check runs weekly by default.  Both share the same
scheduler instance, started/stopped by the FastAPI lifespan.

The cadence job iterates all couples, skips muted ones, checks for an
existing pending proposal via ``ProposalStore.get_latest_pending``, and
only invokes the ideation graph if no pending proposal exists.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from langchain_anthropic import ChatAnthropic
from sqlalchemy import select

from app import db, settings
from app.adapters.sms import build_sms_gateway
from app.agent import GraphDeps, ideation_graph
from app.models import Couple

logger = logging.getLogger(__name__)

# How often to check for proposals awaiting rating.
_RATING_CHECK_INTERVAL_HOURS = 6

# How often to run the cadence (ideation) sweep.
_CADENCE_INTERVAL_DAYS = 7

# Default look-ahead window for the ideation graph when triggered by the
# scheduled job.  On-demand requests use a shorter window (set in the router).
_CADENCE_WINDOW_DAYS = 14

# Module-level scheduler (started/stopped by the FastAPI lifespan).
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Return the module-level scheduler, creating it if needed."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


def _build_llm() -> ChatAnthropic | None:
    """Build a ChatAnthropic instance if ``ANTHROPIC_API_KEY`` is set.

    Returns ``None`` if the key is missing, in which case the cadence job
    will skip couples that require an LLM (logged as a warning).
    """
    if not settings.anthropic_api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set — ideation graph will not be run "
            "by the cadence scheduler"
        )
        return None
    return ChatAnthropic(
        model="claude-sonnet-4-20250514",
        temperature=0.7,
        api_key=settings.anthropic_api_key,
    )


def _build_sms_gateway():
    """Build the SMS gateway from settings."""
    return build_sms_gateway(
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        from_phone=settings.twilio_phone_number,
        status_callback_url=settings.twilio_status_callback_url,
    )


# =========================================================================
# Rating check job (moved from rating_scheduler.py)
# =========================================================================


async def check_awaiting_ratings() -> None:
    """Iterate couples, check for proposals awaiting rating, and send SMS.

    Runs as an APScheduler job.  Each couple gets its own database session
    so that errors for one couple do not affect others.
    """
    logger.info("Rating check: starting sweep for proposals awaiting rating")

    try:
        async with db.session() as session:
            result = await session.execute(select(Couple))
            couples = list(result.scalars().all())
    except Exception:
        logger.exception("Rating check: failed to fetch couples list")
        return

    checked = 0
    prompted = 0

    for couple in couples:
        if couple.suggestions_muted:
            continue

        try:
            async with db.session() as session:
                from app.services.proposal_store import ProposalStore
                store = ProposalStore(session)
                proposal = await store.get_awaiting_rating(couple.id)

                if proposal is None:
                    continue

                deps = GraphDeps(db=session).resolved()
                couple_obj = await deps.couple_store.get_couple(couple.id)
                if couple_obj is None:
                    continue

                users = await deps.couple_store.partner_users(couple_obj)
                if not users:
                    continue

                gateway = _build_sms_gateway()

                sent = False
                for user in users:
                    try:
                        from app.services.rating_scheduler import RATING_PROMPT_TEMPLATE
                        sid = await gateway.send(
                            to_phone=user.phone_number,
                            body=RATING_PROMPT_TEMPLATE,
                        )
                        logger.info(
                            "Rating prompt sent to user %d (couple %d, proposal %d): sid=%s",
                            user.id,
                            couple.id,
                            proposal.id,
                            sid,
                        )
                        sent = True
                    except Exception:
                        logger.exception(
                            "Failed to send rating prompt to user %d (couple %d)",
                            user.id,
                            couple.id,
                        )

                if sent:
                    prompted += 1

                await session.commit()
        except Exception:
            logger.exception(
                "Rating check: error processing couple %d", couple.id
            )

        checked += 1

    logger.info(
        "Rating check: complete — checked %d couples, sent %d prompts",
        checked,
        prompted,
    )


# =========================================================================
# Cadence (ideation) job
# =========================================================================


async def check_cadence() -> None:
    """Weekly ideation sweep: for each unmuted couple without a pending
    proposal, run the full ideation graph and deliver the result via SMS.

    Each couple gets its own database session so that errors for one couple
    do not affect others.
    """
    logger.info("Cadence check: starting weekly ideation sweep")

    llm = _build_llm()
    if llm is None:
        logger.warning(
            "Cadence check: no LLM configured — skipping ideation sweep"
        )
        return

    try:
        async with db.session() as session:
            result = await session.execute(select(Couple))
            couples = list(result.scalars().all())
    except Exception:
        logger.exception("Cadence check: failed to fetch couples list")
        return

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(days=_CADENCE_WINDOW_DAYS)

    processed = 0
    succeeded = 0
    skipped_existing = 0
    skipped_muted = 0

    for couple in couples:
        if couple.suggestions_muted:
            skipped_muted += 1
            continue

        # Each couple gets its own session to isolate errors.
        try:
            async with db.session() as session:
                from app.services.proposal_store import ProposalStore
                store = ProposalStore(session)

                # Guard: skip if a pending proposal already exists.
                existing = await store.get_latest_pending(couple.id)
                if existing is not None:
                    logger.info(
                        "Cadence check: couple %d already has a pending "
                        "proposal %d — skipping",
                        couple.id,
                        existing.id,
                    )
                    skipped_existing += 1
                    continue

                # Build deps and run the ideation graph.
                deps = GraphDeps(
                    db=session,
                    llm=llm,
                    sms_gateway=_build_sms_gateway(),
                ).resolved()

                # Use scheduled mode (not on-demand) for the weekly job.
                state = {
                    "couple_id": couple.id,
                    "window_start": now,
                    "window_end": window_end,
                    "on_demand": False,
                    "min_duration_min": 60,
                    "exclude_activity_id": None,
                }

                result = await ideation_graph.ainvoke(
                    state,
                    {"configurable": {"deps": deps}},
                )

                errors = result.get("errors") or []
                if errors:
                    logger.warning(
                        "Cadence check: ideation graph for couple %d "
                        "returned errors: %s",
                        couple.id,
                        errors,
                    )
                else:
                    succeeded += 1

                await session.commit()
        except Exception:
            logger.exception(
                "Cadence check: error processing couple %d", couple.id
            )

        processed += 1

    logger.info(
        "Cadence check: complete — processed %d couples, %d succeeded, "
        "%d skipped (muted), %d skipped (existing pending)",
        processed,
        succeeded,
        skipped_muted,
        skipped_existing,
    )


# =========================================================================
# Scheduler lifecycle
# =========================================================================


def start_scheduler() -> None:
    """Start the cadence scheduler with both the rating and ideation jobs.

    Called during FastAPI startup (lifespan).  Safe to call multiple times.
    """
    scheduler = get_scheduler()
    if scheduler.running:
        return

    # Rating check job (every 6 hours)
    scheduler.add_job(
        check_awaiting_ratings,
        "interval",
        hours=_RATING_CHECK_INTERVAL_HOURS,
        id="check_awaiting_ratings",
        name="Check for proposals awaiting rating",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),  # run once on startup
    )

    # Cadence (ideation) job (weekly)
    scheduler.add_job(
        check_cadence,
        "interval",
        days=_CADENCE_INTERVAL_DAYS,
        id="check_cadence",
        name="Weekly ideation sweep",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),  # run once on startup
    )

    scheduler.start()
    logger.info(
        "Cadence scheduler started — rating check every %d h, "
        "ideation sweep every %d d",
        _RATING_CHECK_INTERVAL_HOURS,
        _CADENCE_INTERVAL_DAYS,
    )


def stop_scheduler() -> None:
    """Stop the cadence scheduler.

    Called during FastAPI shutdown (lifespan).  Safe to call multiple times.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Cadence scheduler stopped")