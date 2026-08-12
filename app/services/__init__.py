"""Services layer — deterministic CRUD and business logic.

Each service wraps a single database table or domain concept and is the
only entry point for agent nodes to read/write that data.  Services are
scoped to a single ``AsyncSession`` and are instantiated per-request::

    async with db.session() as session:
        store = TraitStore(session)
        traits = await store.get_trait_set(couple_id=42)

``CatalogService`` is a stateless module singleton whose methods take the
session as their first argument (it needs no per-session state).  The
session-scoped stores are constructed per request.
"""

from app.services.calendar_connector import connect_apple, connect_google
from app.services.calendar_resolver import CalendarResolver, ResolvedCalendar
from app.services.catalog import CatalogService, catalog_service
from app.services.couple_store import CoupleStore
from app.services.feedback_attribution import FeedbackAttribution
from app.services.feedback_store import FeedbackStore
from app.services.onboarding_store import OnboardingStore
from app.services.proposal_store import ProposalStore
from app.services.sms_thread_store import SMSThreadStore
from app.services.trait_store import TraitStore

__all__ = [
    "CalendarResolver",
    "CatalogService",
    "CoupleStore",
    "FeedbackAttribution",
    "FeedbackStore",
    "OnboardingStore",
    "ProposalStore",
    "ResolvedCalendar",
    "SMSThreadStore",
    "TraitStore",
    "catalog_service",
    "connect_apple",
    "connect_google",
]