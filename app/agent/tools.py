"""Tools exposed to the agentic nodes.

Scoped per-node, not global:

- ``catalog_search`` / ``web_search`` / ``add_to_catalog`` — exposed to
  ``ideate_activity``.
- ``ProposalEdit`` — the constrained structured-output schema exposed to
  ``edit_proposal``.

The ideate tools are built by ``build_ideate_tools(deps)`` as closures that
capture the resolved ``GraphDeps``, so they can reach the database session and
services without global state.  This is what lets the same compiled graph be
invoked with different sessions/configs.
"""

from __future__ import annotations

from datetime import datetime

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.deps import GraphDeps
from app.schemas.catalog import DateActivityCreate


# =========================================================================
# ProposalEdit — constrained structured-output schema for edit_proposal
# =========================================================================


class ProposalEdit(BaseModel):
    """The only shape an LLM may use to edit a proposal.

    Deliberately narrow (start time / activity / duration only) to prevent
    schema drift and hallucinated fields.  ``reasoning`` is audit-only and is
    never shown to the user.  See ldr-date-agent-devplan.md §4.
    """

    new_start_time: datetime | None = Field(
        default=None,
        description="New proposed start time (RFC 3339, timezone-aware).",
    )
    new_activity_id: int | None = Field(
        default=None,
        description="Replacement activity catalog id, if the user wants a different activity.",
    )
    duration_override_min: int | None = Field(
        default=None,
        ge=15,
        description="Override the activity duration in minutes (floor 60 enforced upstream).",
    )
    reasoning: str = Field(
        description="Explain why these changes were chosen (audit/debug only, not shown to user).",
    )

    def is_noop(self) -> bool:
        """True if the edit changes nothing (all optional fields None)."""
        return (
            self.new_start_time is None
            and self.new_activity_id is None
            and self.duration_override_min is None
        )


# =========================================================================
# ideate_activity tools
# =========================================================================


def build_ideate_tools(deps: GraphDeps) -> list:
    """Build the three tools for ``ideate_activity`` bound to *deps*."""
    deps = deps.resolved()

    @tool
    async def catalog_search(
        query_text: str,
        max_results: int = 10,
        min_similarity: float | None = None,
        duration_max_min: int | None = None,
    ) -> str:
        """Semantic search over the curated long-distance date catalog.

        Search by *meaning*, not exact tags. Build ``query_text`` from the
        couple's top-weighted traits and the target duration. Results include
        a ``similarity`` score (0-1). Use results with similarity >= 0.75; if
        none reach that threshold, call web_search instead.
        """
        results = await deps.catalog.search_semantic(
            deps.db,
            query_text,
            max_results=max_results,
            min_similarity=min_similarity,
            duration_max_min=duration_max_min,
        )
        if not results:
            return "No catalog results found."
        lines = [
            (
                f"- id={r.id} | {r.name} | ~{r.est_duration_min}min | "
                f"similarity={r.similarity:.3f} | {r.description}"
            )
            for r in results
        ]
        return "Catalog results:\n" + "\n".join(lines)

    @tool
    async def add_to_catalog(
        name: str,
        description: str,
        est_duration_min: int,
        tags: list[str],
    ) -> str:
        """Add a genuinely novel activity to the catalog.

        The entry is embedded immediately and dedup-checked: if a near-duplicate
        already exists, the existing id is returned and nothing is inserted.
        Call ONLY when you found a novel activity and will select it.
        """
        activity_in = DateActivityCreate(
            name=name,
            description=description,
            est_duration_min=est_duration_min,
            source="llm",
            tags=tags,
        )
        activity, was_created = await deps.catalog.create_with_dedup(
            deps.db, activity_in
        )
        if was_created:
            return f"Added to catalog: id={activity.id} name={activity.name} (source=llm)"
        return (
            f"Near-duplicate already exists — using existing entry: "
            f"id={activity.id} name={activity.name}"
        )

    @tool
    async def web_search(query: str) -> str:
        """Search the web for fresh long-distance date ideas.

        Use ONLY when catalog_search returned no result with similarity >= 0.75
        or nothing that fits the available duration. Build the query from the
        couple's trait tags + window length (e.g. \"low-energy 90-minute long
        distance date ideas\") so results respect their preferences.
        """
        if deps.web_search is None:
            return (
                "Web search is unavailable (no web_search function injected). "
                "Re-run catalog_search with a different query instead."
            )
        return await deps.web_search(query)

    return [catalog_search, web_search, add_to_catalog]