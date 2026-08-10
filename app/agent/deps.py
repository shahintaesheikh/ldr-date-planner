"""Dependency injection container for the LangGraph agent.

Every node that needs a database session, a service, an LLM, or an SMS gateway
reads it from ``config["configurable"]["deps"]``, which the caller populates
before invoking.  This keeps the graph stateless and testable: tests inject
mocks for everything.

Usage
-----
    deps = GraphDeps(db=session)
    deps = deps.resolved()         # fill default services
    result = await graph.invoke(
        input_state,
        {"configurable": {"deps": deps}},
    )
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable

from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.sms import SMSGateway, build_sms_gateway
from app.services import (
    CalendarResolver,
    CatalogService,
    CoupleStore,
    FeedbackStore,
    ProposalStore,
    SMSThreadStore,
    TraitStore,
    catalog_service,
)

logger = logging.getLogger(__name__)

# Type alias for the web_search callable injected into ideate_activity.
# Takes a plain-text query, returns formatted search results.
WebSearchFn = Callable[[str], Awaitable[str]]


def _dev_sms_gateway() -> SMSGateway:
    """Default: a LoggingSMSGateway that logs instead of sending."""
    from app.adapters.sms import LoggingSMSGateway

    return LoggingSMSGateway()


@dataclass
class GraphDeps:
    """Everything the agent nodes need to do their job.

    ``db`` is the only required field.  All service fields auto-resolve to
    default implementations when ``.resolved()`` is called, so callers can
    pass a minimal ``GraphDeps(db=...)`` without constructing every service.

    ``llm`` and ``web_search`` are ``None`` by default — the agentic nodes
    (``ideate_activity``, ``edit_proposal``) raise an informative error if
    they run without one.  Tests provide mocks.

    ``sms_gateway`` defaults to a dev ``LoggingSMSGateway`` that logs instead
    of sending, so the graph is runnable end-to-end without Twilio creds.
    """

    # --- Required ---
    db: AsyncSession

    # --- Services (auto-resolved by .resolved()) ---
    trait_store: TraitStore | None = None
    proposal_store: ProposalStore | None = None
    sms_thread_store: SMSThreadStore | None = None
    couple_store: CoupleStore | None = None
    feedback_store: FeedbackStore | None = None
    calendar_resolver: CalendarResolver | None = None
    catalog: CatalogService = catalog_service  # stateless singleton

    # --- Agentic dependencies (must be provided by caller) ---
    llm: BaseChatModel | None = None
    web_search: WebSearchFn | None = None

    # --- Infrastructure (defaults provided) ---
    sms_gateway: SMSGateway = field(default_factory=_dev_sms_gateway)

    def resolved(self) -> GraphDeps:
        """Fill in default service instances for any ``None`` fields.

        Safe to call multiple times — already-resolved fields are not
        overwritten.
        """
        if self.trait_store is None:
            self.trait_store = TraitStore(self.db)
        if self.proposal_store is None:
            self.proposal_store = ProposalStore(self.db)
        if self.sms_thread_store is None:
            self.sms_thread_store = SMSThreadStore(self.db)
        if self.couple_store is None:
            self.couple_store = CoupleStore(self.db)
        if self.feedback_store is None:
            self.feedback_store = FeedbackStore(self.db)
        if self.calendar_resolver is None:
            self.calendar_resolver = CalendarResolver()
        return self


def _deps(config: dict) -> GraphDeps:
    """Convenience: extract ``GraphDeps`` from ``config["configurable"]``.

    Callers:
        deps = _deps(config).resolved()
    """
    return config["configurable"]["deps"]