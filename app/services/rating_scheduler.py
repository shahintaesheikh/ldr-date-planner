"""Rating-trigger scheduler — minimal APScheduler instance that checks for
past confirmed proposals awaiting a rating and sends a follow-up SMS.

This is a standalone piece that will become one entry in the Phase 7 cadence
scheduler.  It is deliberately scoped to *one job, one query* to avoid
duplicating infra that Phase 7 will build properly.

The job runs every 6 hours by default, iterating all couples, calling
``ProposalStore.get_awaiting_rating()``, and sending a rating-prompt SMS
via the ``SMSGateway`` if a qualifying proposal is found.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app import db, settings
from app.adapters.sms import build_sms_gateway
from app.agent.deps import GraphDeps
from app.models import Couple
from app.services.feedback_attribution import FeedbackAttribution
from app.services.feedback_store import FeedbackStore
from app.services.proposal_store import ProposalStore

logger = logging.getLogger(__name__)

# How often to check for proposals awaiting rating.
_RATING_CHECK_INTERVAL_HOURS = 6

# The SMS sent to prompt a rating.
RATING_PROMPT_TEMPLATE = (
    "How was your date? Reply with a number 1-5 (1=terrible, 5=amazing), "
    "or SKIP to skip."
)

# Module-level scheduler (started/stopped by the FastAPI lifespan).
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """Return the module-level scheduler, creating it if needed."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler()
    return _scheduler


async def check_awaiting_ratings() -> None:
    """Iterate couples, check for proposals awaiting rating, and send SMS.

    Runs as an APScheduler job.  Each couple gets its own database session
    so that errors for one couple do not affect others.
    """
    logger.info("Rating check: starting sweep for proposals awaiting rating")

    try:
        async with db.session() as session:
            # Fetch all couples.
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
                store = ProposalStore(session)
                proposal = await store.get_awaiting_rating(couple.id)

                if proposal is None:
                    continue

                # Resolve the couple's partners to know who to SMS.
                deps = GraphDeps(db=session).resolved()
                couple_obj = await deps.couple_store.get_couple(couple.id)
                if couple_obj is None:
                    continue

                users = await deps.couple_store.partner_users(couple_obj)
                if not users:
                    continue

                # Send the rating prompt to at least one partner.
                gateway = build_sms_gateway(
                    account_sid=settings.twilio_account_sid,
                    auth_token=settings.twilio_auth_token,
                    from_phone=settings.twilio_phone_number,
                    status_callback_url=settings.twilio_status_callback_url,
                )

                sent = False
                for user in users:
                    try:
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


def start_scheduler() -> None:
    """Start the rating-trigger APScheduler job.

    Called during FastAPI startup (lifespan).  Safe to call multiple times.
    """
    scheduler = get_scheduler()
    if scheduler.running:
        return

    scheduler.add_job(
        check_awaiting_ratings,
        "interval",
        hours=_RATING_CHECK_INTERVAL_HOURS,
        id="check_awaiting_ratings",
        name="Check for proposals awaiting rating",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc),  # run once on startup
    )
    scheduler.start()
    logger.info(
        "Rating scheduler started (interval=%d hours)",
        _RATING_CHECK_INTERVAL_HOURS,
    )


def stop_scheduler() -> None:
    """Stop the rating-trigger scheduler.

    Called during FastAPI shutdown (lifespan).  Safe to call multiple times.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Rating scheduler stopped")