"""LangGraph agent core for the LDR Date Planner.

Two compiled graphs, exportable for import by the FastAPI layer:

- ``ideation_graph`` — fetch_availability → find_overlap_windows → load_traits
  → ideate_activity → estimate_duration → compose_proposal → deliver_sms.
- ``sms_graph`` — classify_intent → route (YES/NO/RERUN/STOP) or
  edit_proposal → validate_edit → compose_proposal → deliver_sms.

Both graphs are dependency-injected per invocation via
``config["configurable"]["deps"]`` (a ``GraphDeps``).  The FastAPI routers
construct a ``GraphDeps(db=session)``, call ``.resolved()``, and pass it in the
config when invoking.

Example
-------
.. code-block:: python

    from app.agent import ideation_graph, GraphDeps

    async with db.session() as session:
        deps = GraphDeps(db=session)
        result = await ideation_graph.ainvoke(
            {
                "couple_id": 1,
                "window_start": now,
                "window_end": now + timedelta(days=7),
                "on_demand": True,
                "min_duration_min": 60,
            },
            {"configurable": {"deps": deps}},
        )
        await session.commit()
"""

from app.agent.deps import GraphDeps, WebSearchFn
from app.agent.ideation_graph import ideation_graph
from app.agent.ideation_graph import build_ideation_graph
from app.agent.sms_graph import build_sms_graph, sms_graph
from app.agent.state import IdeationState, OverlapWindow, ProposalDraft, SMSState
from app.agent.tools import ProposalEdit, build_ideate_tools

__all__ = [
    "GraphDeps",
    "IdeationState",
    "OverlapWindow",
    "ProposalDraft",
    "ProposalEdit",
    "SMSState",
    "WebSearchFn",
    "build_ideate_tools",
    "build_ideation_graph",
    "build_sms_graph",
    "ideation_graph",
    "sms_graph",
]