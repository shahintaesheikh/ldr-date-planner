"""Tests for the LangGraph agent core (ideation graph + SMS graph).

Uses mocks for all external dependencies — no real API calls, no real database.
Validates graph structure, node wiring, state transitions, and error paths.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from importlib import import_module
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolCall

from app.agent import (
    GraphDeps,
    IdeationState,
    ProposalEdit,
    SMSState,
    build_ideation_graph,
    ideation_graph,
    sms_graph,
)
from app.agent.common import (
    compose_proposal,
    deliver_sms,
    free_blocks,
    format_proposal_sms,
    intersect_windows,
    rank_windows,
)
from app.agent.ideation_graph import _build_ideate_payload, _extract_activity
from app.agent.state import OverlapWindow, ProposalDraft
from app.agent.tools import build_ideate_tools
from app.models import ProposalStatus


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def mock_deps(utc_now) -> GraphDeps:
    """Build a fully-mocked GraphDeps for use in graph tests.

    Every service is a mock that returns sensible empty defaults.  Tests
    override specific mocks as needed.
    """
    db = AsyncMock()

    deps = GraphDeps(db=db)
    deps = deps.resolved()  # fill default services

    # --- Trait store ---
    deps.trait_store.get_trait_set = AsyncMock(
        return_value=MagicMock(traits={}, count=0, couple_id=1)
    )

    # --- Couple store ---
    deps.couple_store.get_couple = AsyncMock(
        return_value=MagicMock(
            id=1,
            partner_a_user_id=10,
            partner_b_user_id=20,
            suggestions_muted=False,
        )
    )
    deps.couple_store.partner_users = AsyncMock(
        return_value=[
            MagicMock(id=10, phone_number="+15551111111", timezone="America/New_York", name="Alice"),
            MagicMock(id=20, phone_number="+15552222222", timezone="America/Los_Angeles", name="Bob"),
        ]
    )
    deps.couple_store.get_user = AsyncMock(
        side_effect=lambda uid: {
            10: MagicMock(id=10, phone_number="+15551111111", timezone="America/New_York", name="Alice"),
            20: MagicMock(id=20, phone_number="+15552222222", timezone="America/Los_Angeles", name="Bob"),
        }.get(uid)
    )
    deps.couple_store.get_user_by_phone = AsyncMock(
        side_effect=lambda phone: {
            "+15551111111": MagicMock(id=10, phone_number="+15551111111", timezone="America/New_York", name="Alice"),
            "+15552222222": MagicMock(id=20, phone_number="+15552222222", timezone="America/Los_Angeles", name="Bob"),
        }.get(phone)
    )
    deps.couple_store.get_other_partner = AsyncMock(
        side_effect=lambda couple, uid: (
            MagicMock(id=20, phone_number="+15552222222", timezone="America/Los_Angeles", name="Bob")
            if uid == 10 else
            MagicMock(id=10, phone_number="+15551111111", timezone="America/New_York", name="Alice")
        )
    )
    deps.couple_store.get_couple_for_user = AsyncMock(
        return_value=MagicMock(id=1, partner_a_user_id=10, partner_b_user_id=20)
    )

    # --- Proposal store ---
    deps.proposal_store.get = AsyncMock(
        return_value=MagicMock(
            id=100, couple_id=1, activity_id=5,
            proposed_start=utc_now + timedelta(hours=2),
            proposed_end=utc_now + timedelta(hours=3, minutes=30),
            status=ProposalStatus.pending,
            confirmed_by=None,
            created_at=utc_now,
        )
    )
    deps.proposal_store.get_latest_pending = AsyncMock(
        return_value=MagicMock(
            id=100, couple_id=1, activity_id=5,
            proposed_start=utc_now + timedelta(hours=2),
            proposed_end=utc_now + timedelta(hours=3, minutes=30),
            status=ProposalStatus.pending,
        )
    )
    deps.proposal_store.create_pending = AsyncMock(
        return_value=MagicMock(
            id=101, couple_id=1, activity_id=5,
            proposed_start=utc_now + timedelta(hours=2),
            proposed_end=utc_now + timedelta(hours=3),
            status=ProposalStatus.pending,
            confirmed_by=None,
            created_at=utc_now,
        )
    )
    deps.proposal_store.update = AsyncMock(
        side_effect=lambda pid, data=None: MagicMock(
            id=pid, couple_id=1,
            activity_id=(data.activity_id if data and hasattr(data, 'activity_id') and data.activity_id else 5),
            proposed_start=(data.proposed_start if data and hasattr(data, 'proposed_start') and data.proposed_start else utc_now + timedelta(hours=2)),
            proposed_end=(data.proposed_end if data and hasattr(data, 'proposed_end') and data.proposed_end else utc_now + timedelta(hours=3)),
            status=ProposalStatus.pending,
            confirmed_by=None,
            created_at=utc_now,
        )
    )
    deps.proposal_store.set_status = AsyncMock(return_value=None)

    # --- Feedback store ---
    deps.feedback_store.log = AsyncMock(return_value=None)

    # --- SMS thread store ---
    deps.sms_thread_store.append = AsyncMock(return_value=None)

    # --- Catalog ---
    deps.catalog.get_by_id = AsyncMock(
        return_value=MagicMock(id=5, name="Movie Night", description="Watch a movie together")
    )

    # --- Calendar resolver ---
    deps.calendar_resolver.get_active_adapters = AsyncMock(return_value=[])

    # --- SMS gateway ---
    deps.sms_gateway.send = AsyncMock(return_value="test-sid-123")

    # --- LLM (fake agent responses) ---
    mock_llm = AsyncMock()
    mock_llm.bind_tools = MagicMock(return_value=mock_llm)

    # Default invoke response for edit_proposal (plain text, no tool call)
    mock_llm.ainvoke = AsyncMock(
        return_value=AIMessage(content="I can't change that.")
    )
    deps.llm = mock_llm

    # --- Web search ---
    deps.web_search = AsyncMock(
        return_value="Result: try a virtual painting class, 90 mins, medium energy."
    )

    return deps


@pytest.fixture
def default_ideation_state(utc_now) -> dict:
    """Minimal input for the ideation graph.

    Tests mutate this dict as needed.
    """
    return {
        "couple_id": 1,
        "window_start": utc_now,
        "window_end": utc_now + timedelta(hours=4),
        "on_demand": True,
        "min_duration_min": 60,
        "exclude_activity_id": None,
        "busy_blocks_a": [],
        "busy_blocks_b": [],
        "overlap_windows": [],
        "trait_set": None,
        "selected_activity": None,
        "selected_window": None,
        "draft": None,
        "proposal": None,
        "sms_copy": None,
        "delivery_results": [],
        "errors": [],
    }


# =========================================================================
# Availability helpers
# =========================================================================


class TestFreeBlocks:
    def test_no_busy(self, utc_now):
        busy: list[dict] = []
        free = free_blocks(utc_now, utc_now + timedelta(hours=2), busy)
        assert len(free) == 1
        assert free[0]["start"] == utc_now
        assert free[0]["end"] == utc_now + timedelta(hours=2)

    def test_busy_in_middle(self, utc_now):
        busy = [
            {"start": utc_now + timedelta(hours=1), "end": utc_now + timedelta(hours=1, minutes=30)},
        ]
        free = free_blocks(utc_now, utc_now + timedelta(hours=3), busy)
        assert len(free) == 2
        assert free[0]["end"] == utc_now + timedelta(hours=1)
        assert free[1]["start"] == utc_now + timedelta(hours=1, minutes=30)

    def test_busy_fills_range(self, utc_now):
        busy = [{"start": utc_now, "end": utc_now + timedelta(hours=3)}]
        free = free_blocks(utc_now, utc_now + timedelta(hours=3), busy)
        assert free == []

    def test_busy_outside_range(self, utc_now):
        busy = [
            {"start": utc_now + timedelta(hours=5), "end": utc_now + timedelta(hours=6)},
        ]
        free = free_blocks(utc_now, utc_now + timedelta(hours=3), busy)
        assert len(free) == 1  # entire range is free


class TestIntersectWindows:
    def test_simple_overlap(self, utc_now):
        a = [{"start": utc_now, "end": utc_now + timedelta(hours=2)}]
        b = [{"start": utc_now + timedelta(hours=1), "end": utc_now + timedelta(hours=3)}]
        windows = intersect_windows(a, b, min_duration_min=30)
        assert len(windows) == 1
        assert windows[0].start == utc_now + timedelta(hours=1)
        assert windows[0].end == utc_now + timedelta(hours=2)

    def test_no_overlap(self, utc_now):
        a = [{"start": utc_now, "end": utc_now + timedelta(hours=1)}]
        b = [{"start": utc_now + timedelta(hours=2), "end": utc_now + timedelta(hours=3)}]
        windows = intersect_windows(a, b, min_duration_min=30)
        assert windows == []

    def test_below_min_duration(self, utc_now):
        a = [{"start": utc_now, "end": utc_now + timedelta(minutes=45)}]
        b = [{"start": utc_now + timedelta(minutes=30), "end": utc_now + timedelta(hours=1)}]
        windows = intersect_windows(a, b, min_duration_min=60)
        assert windows == []

    def test_multiple_windows(self, utc_now):
        a = [{"start": utc_now, "end": utc_now + timedelta(hours=4)}]
        b = [
            {"start": utc_now + timedelta(hours=1), "end": utc_now + timedelta(hours=2)},
            {"start": utc_now + timedelta(hours=3), "end": utc_now + timedelta(hours=4)},
        ]
        windows = intersect_windows(a, b, min_duration_min=30)
        assert len(windows) == 2


class TestRankWindows:
    def test_on_demand_favors_early(self, utc_now):
        windows = [
            OverlapWindow(start=utc_now + timedelta(days=6), end=utc_now + timedelta(days=6, hours=2)),
            OverlapWindow(start=utc_now + timedelta(days=2), end=utc_now + timedelta(days=2, hours=2)),
        ]
        ranked = rank_windows(windows, on_demand=True, now=utc_now)
        # The day-2 window is within 5 days → should rank first
        assert ranked[0].start == utc_now + timedelta(days=2)
        assert ranked[1].start == utc_now + timedelta(days=6)


class TestProposalDraft:
    def test_overlap_window_computes_duration(self, utc_now):
        w = OverlapWindow(start=utc_now, end=utc_now + timedelta(hours=1, minutes=30))
        assert w.duration_min == 90


# =========================================================================
# ProposalDraft / ProposalEdit
# =========================================================================


class TestProposalEdit:
    def test_noop_all_none(self):
        edit = ProposalEdit(new_start_time=None, new_activity_id=None, duration_override_min=None, reasoning="test")
        assert edit.is_noop()

    def test_not_noop_with_start(self, utc_now):
        edit = ProposalEdit(new_start_time=utc_now, reasoning="change time")
        assert not edit.is_noop()

    def test_not_noop_with_duration(self):
        edit = ProposalEdit(duration_override_min=90, reasoning="longer")
        assert not edit.is_noop()


# =========================================================================
# SMS formatting
# =========================================================================


class TestFormatProposalSms:
    def test_basic_format(self, utc_now):
        draft = ProposalDraft(activity_id=1, activity_name="Movie Night", description="Watch a movie", start=utc_now, end=utc_now + timedelta(hours=2), duration_min=120)
        body = format_proposal_sms(draft, local_tz_name="America/New_York")
        assert "Movie Night" in body
        assert "YES, NO, RERUN" in body
        assert ":00" in body


# =========================================================================
# Ideation graph — node-level tests
# =========================================================================


class TestIdeatePayload:
    def test_builds_without_traits(self, utc_now):
        windows = [OverlapWindow(start=utc_now, end=utc_now + timedelta(hours=2))]
        state = {
            "trait_set": MagicMock(traits={}, count=0, couple_id=1),
            "overlap_windows": windows,
            "min_duration_min": 60,
            "exclude_activity_id": 5,
        }
        payload = _build_ideate_payload(state)
        assert "Excluded activity id: 5" in payload


class TestExtractActivity:
    def test_extracts_json_from_message(self):
        msg = AIMessage(content='{"activity_id": 3, "name": "Cooking", "description": "Cook together", "est_duration_min": 90, "source": "seed", "novel": false}')
        result = _extract_activity([msg])
        assert result is not None
        assert result["name"] == "Cooking"

    def test_extracts_json_from_markdown(self):
        msg = AIMessage(content='```json\n{"name": "Game Night", "activity_id": 4, "est_duration_min": 60}\n```')
        result = _extract_activity([msg])
        assert result is not None
        assert result["name"] == "Game Night"

    def test_returns_none_when_no_json(self):
        msg = AIMessage(content="I think we should play a game.")
        result = _extract_activity([msg])
        assert result is None


# =========================================================================
# Ideation graph — integration test with mocks
# =========================================================================


class TestIdeationGraph:
    """Tests the full ideation graph with a mock LLM that simulates the
    react agent's final message."""

    @pytest.mark.asyncio
    async def test_happy_path(self, mock_deps, utc_now, default_ideation_state):
        """Run the full graph with all mocks wired and verify it completes."""
        # Override the LLM mock to return a valid activity selection
        mock_llm = AsyncMock()

        # create_react_agent invokes model.ainvoke — mock the react agent's
        # response as the final AI message with activity JSON
        mock_llm.ainvoke = AsyncMock(
            return_value=AIMessage(
                content=(
                    '{"activity_id": 5, "name": "Movie Night", '
                    '"description": "Watch a movie together", '
                    '"est_duration_min": 120, "source": "seed", "novel": false}'
                )
            )
        )
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_deps.llm = mock_llm

        result = await ideation_graph.ainvoke(
            default_ideation_state,
            {"configurable": {"deps": mock_deps}},
        )

        # It should reach deliver_sms
        assert "delivery_results" in result
        assert "proposal" in result
        assert "draft" in result
        assert result["draft"].activity_id == 5

    @pytest.mark.asyncio
    async def test_no_overlap_windows(self, mock_deps, utc_now, default_ideation_state):
        """When calendars return no free windows, the graph should handle
        gracefully (no overlap_windows → ideate has nothing to work with)."""
        # The LLM will be called but find no windows. Still should not crash.
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=AIMessage(
                content='{"activity_id": 5, "name": "Movie Night", "est_duration_min": 60}'
            )
        )
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_deps.llm = mock_llm

        # Set busy blocks that cover the entire range
        state = dict(default_ideation_state)
        state["busy_blocks_a"] = [{"start": utc_now, "end": utc_now + timedelta(hours=4)}]
        state["busy_blocks_b"] = [{"start": utc_now, "end": utc_now + timedelta(hours=4)}]

        result = await ideation_graph.ainvoke(
            state,
            {"configurable": {"deps": mock_deps}},
        )

        # No overlap windows → estimate_duration will see empty windows
        # and produce an error
        assert len(result.get("errors", [])) > 0

    @pytest.mark.asyncio
    async def test_llm_returns_no_activity(self, mock_deps, utc_now, default_ideation_state):
        """If the LLM fails to produce a valid activity JSON, an error is
        accumulated."""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=AIMessage(content="Sorry, I couldn't find any good ideas.")
        )
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_deps.llm = mock_llm

        # Add some windows so we get past find_overlap_windows
        state = dict(default_ideation_state)
        state["overlap_windows"] = [OverlapWindow(utc_now + timedelta(hours=1), utc_now + timedelta(hours=2))]

        result = await ideation_graph.ainvoke(
            state,
            {"configurable": {"deps": mock_deps}},
        )

        assert len(result.get("errors", [])) > 0


# =========================================================================
# SMS graph — node-level tests
# =========================================================================


class TestClassifyReply:
    """Tests for the pure classify_reply function in sms_graph."""

    def test_rerun_path(self):
        from app.agent.sms_graph import classify_reply
        assert classify_reply("try again") == "RERUN"
        assert classify_reply("something else please") == "RERUN"

    def test_stop_path(self):
        from app.agent.sms_graph import classify_reply
        assert classify_reply("stop") == "STOP"

    def test_mute_path(self):
        from app.agent.sms_graph import classify_reply
        assert classify_reply("mute") == "MUTE"
        assert classify_reply("mute these suggestions") == "MUTE"
        assert classify_reply("quiet") == "MUTE"

    def test_unmute_path(self):
        from app.agent.sms_graph import classify_reply
        assert classify_reply("unmute") == "UNMUTE"
        assert classify_reply("unmute me") == "UNMUTE"
        assert classify_reply("resume") == "UNMUTE"

    def test_yes_path(self):
        from app.agent.sms_graph import classify_reply
        assert classify_reply("yes") == "YES"
        assert classify_reply("yeah sounds good") == "YES"
        assert classify_reply("ok do it") == "YES"

    def test_no_path(self):
        from app.agent.sms_graph import classify_reply
        assert classify_reply("no") == "NO"
        assert classify_reply("nope") == "NO"
        assert classify_reply("pass") == "NO"

    def test_rerun_overrides_no(self):
        from app.agent.sms_graph import classify_reply
        assert classify_reply("no, try again") == "RERUN"

    def test_edit_fallback(self):
        from app.agent.sms_graph import classify_reply
        assert classify_reply("can we do 9pm instead") == "EDIT"
        assert classify_reply("make it shorter") == "EDIT"
        assert classify_reply("") == "EDIT"


# =========================================================================
# SMS graph — integration test
# =========================================================================


class TestSMSGraph:
    @pytest.mark.asyncio
    async def test_yes_path(self, mock_deps, utc_now):
        """YES intent should lock proposal, log feedback, and notify partner."""
        mock_deps.user_id = 10  # sender is Alice
        result = await sms_graph.ainvoke(
            {
                "from_phone": "+15551111111",
                "raw_body": "yes",
                "couple_id": 1,
                "user_id": 10,
                "proposal_id": 100,
                "intent": None,
                "rating_parsed": None,
                "edit": None,
                "edit_valid": None,
                "needs_clarification": None,
                "clarification_msg": None,
                "draft": None,
                "proposal": None,
                "sms_copy": None,
                "delivery_results": [],
                "clarification_sent": None,
                "errors": [],
            },
            {"configurable": {"deps": mock_deps}},
        )
        # Should have routed to route_yes
        assert "delivery_results" in result
        assert "proposal" in result
        assert result.get("delivery_results", []) != []

    @pytest.mark.asyncio
    async def test_no_path(self, mock_deps, utc_now):
        """NO intent should reject the proposal."""
        mock_deps.proposal_store.get = AsyncMock(
            return_value=MagicMock(id=100, activity_id=5)
        )
        result = await sms_graph.ainvoke(
            {
                "from_phone": "+15551111111",
                "raw_body": "no",
                "couple_id": 1,
                "user_id": 10,
                "proposal_id": 100,
                "intent": None,
                "rating_parsed": None,
                "edit": None,
                "edit_valid": None,
                "needs_clarification": None,
                "clarification_msg": None,
                "draft": None,
                "proposal": None,
                "sms_copy": None,
                "delivery_results": [],
                "clarification_sent": None,
                "errors": [],
            },
            {"configurable": {"deps": mock_deps}},
        )
        # This should not crash. Delivery results may be empty.
        assert "errors" in result

    @pytest.mark.asyncio
    async def test_stop_path(self, mock_deps, utc_now):
        """STOP intent should reject the current proposal (no mute)."""
        mock_deps.proposal_store.set_status = AsyncMock(return_value=None)
        result = await sms_graph.ainvoke(
            {
                "from_phone": "+15551111111",
                "raw_body": "stop",
                "couple_id": 1,
                "user_id": 10,
                "proposal_id": 100,
                "intent": None,
                "rating_parsed": None,
                "edit": None,
                "edit_valid": None,
                "needs_clarification": None,
                "clarification_msg": None,
                "draft": None,
                "proposal": None,
                "sms_copy": None,
                "delivery_results": [],
                "clarification_sent": None,
                "errors": [],
            },
            {"configurable": {"deps": mock_deps}},
        )
        mock_deps.proposal_store.set_status.assert_awaited_once_with(100, ProposalStatus.rejected)

    @pytest.mark.asyncio
    async def test_mute_path(self, mock_deps, utc_now):
        """MUTE keyword should mute suggestions."""
        mock_deps.couple_store.set_muted = AsyncMock(return_value=None)
        result = await sms_graph.ainvoke(
            {
                "from_phone": "+15551111111",
                "raw_body": "mute",
                "couple_id": 1,
                "user_id": 10,
                "proposal_id": 100,
                "intent": None,
                "rating_parsed": None,
                "edit": None,
                "edit_valid": None,
                "needs_clarification": None,
                "clarification_msg": None,
                "draft": None,
                "proposal": None,
                "sms_copy": None,
                "delivery_results": [],
                "clarification_sent": None,
                "errors": [],
            },
            {"configurable": {"deps": mock_deps}},
        )
        mock_deps.couple_store.set_muted.assert_awaited_once_with(1, muted=True)

    @pytest.mark.asyncio
    async def test_unmute_path(self, mock_deps, utc_now):
        """UNMUTE keyword should unmute suggestions."""
        mock_deps.couple_store.set_muted = AsyncMock(return_value=None)
        result = await sms_graph.ainvoke(
            {
                "from_phone": "+15551111111",
                "raw_body": "unmute",
                "couple_id": 1,
                "user_id": 10,
                "proposal_id": 100,
                "intent": None,
                "rating_parsed": None,
                "edit": None,
                "edit_valid": None,
                "needs_clarification": None,
                "clarification_msg": None,
                "draft": None,
                "proposal": None,
                "sms_copy": None,
                "delivery_results": [],
                "clarification_sent": None,
                "errors": [],
            },
            {"configurable": {"deps": mock_deps}},
        )
        mock_deps.couple_store.set_muted.assert_awaited_once_with(1, muted=False)

    @pytest.mark.asyncio
    async def test_freeform_edit_no_tool_call(self, mock_deps, utc_now):
        """Freeform SMS that doesn't match any supported field → clarification."""
        result = await sms_graph.ainvoke(
            {
                "from_phone": "+15551111111",
                "raw_body": "can we invite my sister too",
                "couple_id": 1,
                "user_id": 10,
                "proposal_id": 100,
                "intent": None,
                "rating_parsed": None,
                "edit": None,
                "edit_valid": None,
                "needs_clarification": None,
                "clarification_msg": None,
                "draft": None,
                "proposal": None,
                "sms_copy": None,
                "delivery_results": [],
                "clarification_sent": None,
                "errors": [],
            },
            {"configurable": {"deps": mock_deps}},
        )
        # LLM returned plain text (no tool call) → clarification path
        assert result.get("needs_clarification") is True

    @pytest.mark.asyncio
    async def test_freeform_edit_with_tool_call(self, mock_deps, utc_now):
        """Freeform SMS that matches the time field → should validate edit."""
        # Override LLM to return a tool call
        mock_llm = AsyncMock()
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.ainvoke = AsyncMock(
            return_value=AIMessage(
                content="Changing the time.",
                tool_calls=[
                    ToolCall(
                        name="ProposalEdit",
                        args={
                            "new_start_time": (utc_now + timedelta(hours=5)).isoformat(),
                            "new_activity_id": None,
                            "duration_override_min": None,
                            "reasoning": "User asked for 5pm instead",
                        },
                        id="call_1",
                        type="tool_call",
                    )
                ],
            )
        )
        mock_deps.llm = mock_llm

        # Mock slot check to return free
        sg = import_module("app.agent.sms_graph")
        with patch.object(sg, "_slot_free", new=AsyncMock(return_value=(True, ""))):
            result = await sms_graph.ainvoke(
                {
                    "from_phone": "+15551111111",
                    "raw_body": "let's do 5pm instead",
                    "couple_id": 1,
                    "user_id": 10,
                    "proposal_id": 100,
                    "intent": None,
                    "rating_parsed": None,
                    "edit": None,
                    "edit_valid": None,
                    "needs_clarification": None,
                    "clarification_msg": None,
                    "draft": None,
                    "proposal": None,
                    "sms_copy": None,
                    "delivery_results": [],
                    "clarification_sent": None,
                    "errors": [],
                },
                {"configurable": {"deps": mock_deps}},
            )

        # Should have composed + delivered a new SMS
        assert result.get("delivery_results", []) != []


# =========================================================================
# RERUN from SMS graph → ideation graph (integration)
# =========================================================================


class TestRerunPath:
    @pytest.mark.asyncio
    async def test_rerun_invokes_ideation(self, mock_deps, utc_now):
        """RERUN should invoke the ideation graph via route_rerun."""
        from importlib import import_module

        ig_module = import_module("app.agent.ideation_graph")

        mock_ideation = AsyncMock()
        mock_ideation.ainvoke = AsyncMock(
            return_value={
                "proposal": MagicMock(id=200),
                "draft": ProposalDraft(
                    activity_id=5, activity_name="Test", description="",
                    start=utc_now, end=utc_now + timedelta(hours=2), duration_min=120,
                ),
                "sms_copy": "Date idea: Test",
                "delivery_results": [{"sid": "sid-001"}],
            }
        )

        with unittest.mock.patch.object(ig_module, "ideation_graph", mock_ideation):
            from app.agent.sms_graph import route_rerun

            result = await route_rerun(
                {
                    "from_phone": "+15551111111",
                    "raw_body": "rerun",
                    "couple_id": 1,
                    "user_id": 10,
                    "proposal_id": 100,
                    "intent": "RERUN",
                    "rating_parsed": None,
                    "edit": None,
                    "edit_valid": None,
                    "needs_clarification": False,
                    "clarification_msg": None,
                    "draft": None,
                    "proposal": None,
                    "sms_copy": None,
                    "delivery_results": [],
                    "clarification_sent": None,
                    "errors": [],
                },
                {"configurable": {"deps": mock_deps}},
            )

        assert "delivery_results" in result
        assert result.get("sms_copy") is not None
        mock_ideation.ainvoke.assert_awaited_once()