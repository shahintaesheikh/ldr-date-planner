"""Ideation graph — the core LangGraph agent.

Pipeline::

    fetch_availability → find_overlap_windows → load_traits → ideate_activity
        → estimate_duration → compose_proposal → deliver_sms

Only ``ideate_activity`` (catalog_search / web_search / add_to_catalog) and
``estimate_duration`` (LLM reasoning, no tools in v1) are agentic. The rest is
deterministic plumbing. See ldr-date-agent-devplan.md §2/§4.

The graph is compiled once at module level and exported; dependencies are
injected per-invocation via ``config["configurable"]["deps"]``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agent.common import (
    IDEATE_SYSTEM_PROMPT,
    compose_proposal,
    deliver_sms,
    free_blocks,
    intersect_windows,
    rank_windows,
)
from app.agent.deps import _deps
from app.agent.state import IdeationState, OverlapWindow, ProposalDraft
from app.agent.tools import build_ideate_tools

logger = logging.getLogger(__name__)

# Max model/tool iterations in the ideate_activity loop before giving up.
_MAX_IDEATE_STEPS = 8

# =========================================================================
# Node implementations
# =========================================================================


async def fetch_availability(state: dict, config: RunnableConfig) -> dict:
    """Pull busy/free blocks from both partners' calendars (UTC)."""
    try:
        deps = _deps(config).resolved()
        couple = await deps.couple_store.get_couple(state["couple_id"])
        if couple is None:
            return {"errors": state.get("errors", []) + [f"Couple {state['couple_id']} not found"]}

        resolved = await deps.calendar_resolver.get_active_adapters(
            deps.db, state["couple_id"]
        )

        busy_a: list[dict] = []
        busy_b: list[dict] = []
        had_a = False
        had_b = False
        errors: list[str] = state.get("errors", [])[:]

        for rc in resolved:
            blocks = await rc.adapter.get_busy_blocks(
                state["window_start"], state["window_end"]
            )
            serialized = [{"start": b.start, "end": b.end} for b in blocks]
            if rc.user_id == couple.partner_a_user_id:
                busy_a = serialized
                had_a = True
            elif rc.user_id == couple.partner_b_user_id:
                busy_b = serialized
                had_b = True

        if not had_a:
            errors.append(
                "Partner A has no active calendar connection — treated as fully free"
            )
        if not had_b:
            errors.append(
                "Partner B has no active calendar connection — treated as fully free"
            )

        return {"busy_blocks_a": busy_a, "busy_blocks_b": busy_b, "errors": errors}
    except Exception as exc:
        logger.exception("Error in fetch_availability: %s", exc)
        return {"errors": state.get("errors", []) + [f"fetch_availability: {exc}"]}


def find_overlap_windows(state: dict) -> dict:
    """Constraint-solve: intersect free blocks, filter >= min duration, rank."""
    try:
        free_a = free_blocks(
            state["window_start"], state["window_end"], state.get("busy_blocks_a", [])
        )
        free_b = free_blocks(
            state["window_start"], state["window_end"], state.get("busy_blocks_b", [])
        )
        windows = intersect_windows(
            free_a, free_b, min_duration_min=state.get("min_duration_min", 60)
        )
        windows = rank_windows(
            windows, on_demand=state.get("on_demand", True), now=state["window_start"]
        )
        return {"overlap_windows": windows}
    except Exception as exc:
        logger.exception("Error in find_overlap_windows: %s", exc)
        return {"overlap_windows": [], "errors": state.get("errors", []) + [f"find_overlap_windows: {exc}"]}


async def load_traits(state: dict, config: RunnableConfig) -> dict:
    """Deterministic DB read of the couple's current trait vector."""
    try:
        deps = _deps(config).resolved()
        trait_set = await deps.trait_store.get_trait_set(state["couple_id"])
        return {"trait_set": trait_set}
    except Exception as exc:
        logger.exception("Error in load_traits: %s", exc)
        return {"trait_set": None, "errors": state.get("errors", []) + [f"load_traits: {exc}"]}


async def ideate_activity(state: dict, config: RunnableConfig) -> dict:
    """Agentic core: pick a catalog activity or propose a novel one.

    Exposes ``catalog_search`` / ``web_search`` / ``add_to_catalog`` to the LLM
    via an explicit tool-execution loop. The system prompt enforces the ordering
    gate (catalog first, web only below the similarity threshold); the loop
    itself is bounded so a misbehaving model cannot run forever.
    """
    try:
        deps = _deps(config).resolved()
        if deps.llm is None:
            raise RuntimeError(
                "ideate_activity requires deps.llm (an LLM). Set ANTHROPIC_API_KEY "
                "and inject a model, or pass a mock in tests."
            )

        tools = {t.name: t for t in build_ideate_tools(deps)}
        bound = deps.llm.bind_tools(list(tools.values()))

        messages: list = [
            SystemMessage(content=IDEATE_SYSTEM_PROMPT),
            HumanMessage(content=_build_ideate_payload(state)),
        ]

        for _ in range(_MAX_IDEATE_STEPS):
            response = await bound.ainvoke(messages)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                break

            for tc in tool_calls:
                name = tc.get("name")
                the_tool = tools.get(name)
                if the_tool is None:
                    messages.append(
                        ToolMessage(
                            content=f"Unknown tool: {name}", tool_call_id=tc.get("id")
                        )
                    )
                    continue
                result = await the_tool.ainvoke(tc.get("args") or {})
                messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc.get("id"))
                )

        activity = _extract_activity(messages)
        if activity is None:
            return {"errors": state.get("errors", []) + ["ideate_activity produced no selected activity"]}
        return {"selected_activity": activity}
    except Exception as exc:
        logger.exception("Error in ideate_activity: %s", exc)
        return {"selected_activity": None, "errors": state.get("errors", []) + [f"ideate_activity: {exc}"]}


async def estimate_duration(state: dict, config: RunnableConfig) -> dict:
    """Agentic (no tools in v1): reason about the real duration, pick a window.

    Uses the catalog's ``est_duration_min`` as a prior. Floor is 60 minutes. If
    no LLM is injected, falls back to the catalog's estimate.
    """
    try:
        deps = _deps(config).resolved()
        activity = state.get("selected_activity")
        windows = state.get("overlap_windows") or []
        min_duration = state.get("min_duration_min", 60)

        if activity is None or not windows:
            return {"errors": state.get("errors", []) + ["estimate_duration requires selected_activity and windows"]}

        prior = max(activity.get("est_duration_min") or 60, min_duration)
        if deps.llm is not None:
            duration = await _ask_duration_estimate(deps.llm, activity, windows)
        else:
            duration = prior

        duration = max(duration, 60)
        window = _pick_window(windows, duration)

        if window is None:
            return {"errors": state.get("errors", []) + [f"No overlap window fits a {duration}min activity"]}

        # Clamp duration to the chosen window
        duration = min(duration, window.duration_min)
        activity["est_duration_min"] = duration

        return {"selected_activity": activity, "selected_window": window}
    except Exception as exc:
        logger.exception("Error in estimate_duration: %s", exc)
        return {"selected_window": None, "errors": state.get("errors", []) + [f"estimate_duration: {exc}"]}


# =========================================================================
# Graph builder
# =========================================================================


def build_ideation_graph():
    """Build and compile the ideation graph."""
    graph = StateGraph(IdeationState)

    graph.add_node("fetch_availability", fetch_availability)
    graph.add_node("find_overlap_windows", find_overlap_windows)
    graph.add_node("load_traits", load_traits)
    graph.add_node("ideate_activity", ideate_activity)
    graph.add_node("estimate_duration", estimate_duration)
    graph.add_node("compose_proposal", compose_proposal)
    graph.add_node("deliver_sms", deliver_sms)

    graph.add_edge(START, "fetch_availability")
    graph.add_edge("fetch_availability", "find_overlap_windows")
    graph.add_edge("find_overlap_windows", "load_traits")
    graph.add_edge("load_traits", "ideate_activity")
    graph.add_edge("ideate_activity", "estimate_duration")
    graph.add_edge("estimate_duration", "compose_proposal")
    graph.add_edge("compose_proposal", "deliver_sms")
    graph.add_edge("deliver_sms", END)

    return graph.compile()


ideation_graph = build_ideation_graph()


# =========================================================================
# Internal helpers
# =========================================================================


def _build_ideate_payload(state: dict) -> str:
    """Format traits + windows + constraints into a user message for the LLM."""
    parts: list[str] = []

    # Traits
    trait_set = state.get("trait_set")
    if trait_set and trait_set.traits:
        lines = []
        for key, meta in trait_set.traits.items():
            value = meta.get("value")
            weight = meta.get("weight")
            lines.append(f"- {key}={value} (weight {weight})")
        parts.append("Couple traits:\n" + "\n".join(lines))
    else:
        parts.append("Couple traits: (none recorded yet)")

    # Windows
    windows = state.get("overlap_windows") or []
    if windows:
        lines = [
            f"- {w.start.isoformat()} to {w.end.isoformat()} ({w.duration_min} min)"
            for w in windows
        ]
        parts.append("Available windows (UTC):\n" + "\n".join(lines))
    else:
        parts.append("Available windows: (none found)")

    # Constraints
    parts.append(f"Minimum duration: {state.get('min_duration_min', 60)} min")
    if state.get("exclude_activity_id"):
        parts.append(f"Excluded activity id: {state['exclude_activity_id']}")

    return "\n\n".join(parts)


def _extract_activity(messages: list) -> dict | None:
    """Extract the selected-activity JSON from the react agent's final message.

    The model is instructed to emit a JSON object; we scan messages for the
    first parseable JSON that has a ``name`` field.
    """
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if not content:
            continue
        if isinstance(content, list):
            # Some providers return content as a list of blocks
            content = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        if not isinstance(content, str):
            continue
        obj = _try_parse_json(content)
        if obj and isinstance(obj, dict) and obj.get("name"):
            return {
                "activity_id": obj.get("activity_id"),
                "name": obj["name"],
                "description": obj.get("description") or "",
                "est_duration_min": max(int(obj.get("est_duration_min") or 60), 60),
                "source": obj.get("source") or ("llm" if obj.get("novel") else "seed"),
                "novel": bool(obj.get("novel")),
            }
    return None


def _try_parse_json(text: str) -> dict | None:
    """Try to parse a dict out of a JSON object, stripping markdown fences."""
    # Strip ```json ... ``` fences
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    # Try whole-text parse first
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    # Fall back to the first {...} block
    m = re.search(r"\{.*\}", candidate, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


async def _ask_duration_estimate(
    llm, activity: dict, windows: list[OverlapWindow]
) -> int:
    """Consult the LLM for a realistic activity duration (floor 60)."""
    window_text = "; ".join(
        f"{w.duration_min}min" for w in windows[:5]
    ) or "unknown"
    prompt = (
        f"Activity: {activity['name']}. Description: {activity.get('description','')}. "
        f"Catalog estimate: {activity.get('est_duration_min')} min. "
        f"Available window lengths: {window_text}. "
        "Estimate the realistic time needed for this virtual long-distance date, "
        "in minutes. Reply with a single integer only (minimum 60)."
    )
    resp = await llm.ainvoke(prompt)
    content = getattr(resp, "content", "") or ""
    match = re.search(r"\d+", content)
    if match:
        return max(int(match.group(0)), 60)
    return max(activity.get("est_duration_min") or 60, 60)


def _pick_window(
    windows: list[OverlapWindow], duration_min: int
) -> OverlapWindow | None:
    """Pick the best-ranked window that fits *duration_min*.

    Prefers the first window (already ranked) that is long enough; otherwise
    returns the longest window as a fallback.
    """
    if not windows:
        return None
    for w in windows:
        if w.duration_min >= duration_min:
            return w
    return max(windows, key=lambda w: w.duration_min)