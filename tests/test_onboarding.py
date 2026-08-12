"""Tests for the SMS-native onboarding flow (see .pi/sms-auth.md).

Covers:
- JOIN keyword routing to onboarding_node
- The full identity phase (name → partner phone → partner name → confirm)
- Partner confirmation creating the couple row
- Calendar choice (google/apple/skip)
- Trait capture and session completion

Uses mocked deps — no real DB, no real SMS.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import GraphDeps
from app.agent.sms_graph import classify_reply, sms_graph
from app.models import OnboardingStep, ProposalStatus, TraitSource

ALICE_PHONE = "+15551111111"
BOB_PHONE = "+15552222222"


@pytest.fixture
def mock_deps():
    """GraphDeps with mocked stores and a recording SMS gateway."""
    db = AsyncMock()

    deps = GraphDeps(db=db)
    deps = deps.resolved()

    # --- Onboarding store: in-memory sessions keyed by phone ---
    sessions: dict[str, MagicMock] = {}
    next_session_id = 1

    def _make_session(phone: str, step: OnboardingStep, data: dict | None = None):
        nonlocal next_session_id
        s = MagicMock()
        s.id = next_session_id
        s.phone_number = phone
        s.step = step
        s.data = data or {}
        next_session_id += 1
        sessions[phone] = s
        return s

    deps.onboarding_store.get_by_phone = AsyncMock(
        side_effect=lambda phone: sessions.get(phone)
    )
    deps.onboarding_store.get_by_id = AsyncMock(
        side_effect=lambda sid: next(
            (s for s in sessions.values() if s.id == sid), None
        )
    )

    async def _create(phone_number, step=OnboardingStep.await_name, data=None):
        if phone_number in sessions:
            return sessions[phone_number]
        return _make_session(phone_number, step, data)

    deps.onboarding_store.create = AsyncMock(side_effect=_create)

    async def _advance(phone_number, next_step, data_updates=None):
        s = sessions.get(phone_number)
        if s is None:
            return None
        s.step = next_step
        current = dict(s.data or {})
        if data_updates:
            current.update(data_updates)
        s.data = current
        return s

    deps.onboarding_store.advance_step = AsyncMock(side_effect=_advance)

    deps.onboarding_store.delete = AsyncMock(
        side_effect=lambda phone: bool(sessions.pop(phone, None))
    )

    # --- User store: in-memory users ---
    users: dict[int, MagicMock] = {}
    next_user_id = 1
    user_by_phone: dict[str, MagicMock] = {}

    def _make_user(name: str, phone_number: str):
        nonlocal next_user_id
        u = MagicMock()
        u.id = next_user_id
        u.name = name
        u.phone_number = phone_number
        u.timezone = "UTC"
        next_user_id += 1
        users[u.id] = u
        user_by_phone[phone_number] = u
        return u

    deps.couple_store.create_user = AsyncMock(side_effect=_make_user)
    deps.couple_store.get_user = AsyncMock(side_effect=lambda uid: users.get(uid))
    deps.couple_store.get_user_by_phone = AsyncMock(
        side_effect=lambda phone: user_by_phone.get(phone)
    )

    # --- Couple store ---
    couples: list[MagicMock] = []
    deps.couple_store.create_couple = AsyncMock(
        side_effect=lambda partner_a_user_id, partner_b_user_id: (
            couples.append(
                MagicMock(
                    id=len(couples) + 1,
                    partner_a_user_id=partner_a_user_id,
                    partner_b_user_id=partner_b_user_id,
                )
            )
            or couples[-1]
        )
    )
    deps.couple_store.get_couple_for_user = AsyncMock(
        side_effect=lambda uid: next(
            (
                c for c in couples
                if c.partner_a_user_id == uid or c.partner_b_user_id == uid
            ),
            None,
        )
    )
    deps.couple_store.get_couple = AsyncMock(
        side_effect=lambda cid: next(
            (c for c in couples if c.id == cid), None
        )
    )

    # --- Trait store ---
    written_traits: list[dict] = []
    deps.trait_store.upsert_trait = AsyncMock(
        side_effect=lambda couple_id, data: (
            written_traits.append(
                {
                    "couple_id": couple_id,
                    "trait_key": data.trait_key,
                    "value": data.value,
                    "weight": data.weight,
                    "source": data.source,
                }
            )
            or MagicMock()
        )
    )

    # --- Proposal store stubs (unused by onboarding) ---
    deps.proposal_store.get_latest_pending = AsyncMock(return_value=None)
    deps.proposal_store.get_awaiting_rating = AsyncMock(return_value=None)
    deps.sms_thread_store.append = AsyncMock(return_value=None)

    # --- SMS gateway: record sent messages ---
    sent: list[tuple[str, str]] = []
    deps.sms_gateway.send = AsyncMock(
        side_effect=lambda to_phone, body: (
            sent.append((to_phone, body)) or f"dev-{len(sent)}"
        )
    )

    # Store references for assertions.
    deps._sessions = sessions
    deps._sent = sent
    deps._users = users
    deps._couples = couples
    deps._written_traits = written_traits
    return deps


def _sms_state(from_phone: str, raw_body: str) -> dict:
    """Build a minimal SMSState input."""
    return {
        "from_phone": from_phone,
        "raw_body": raw_body,
        "couple_id": None,
        "user_id": None,
        "proposal_id": None,
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
    }


async def _run(mock_deps, from_phone: str, raw_body: str) -> dict:
    """Invoke the full sms_graph for a single message."""
    return await sms_graph.ainvoke(
        _sms_state(from_phone, raw_body),
        {"configurable": {"deps": mock_deps}},
    )


# =========================================================================
# classify_reply JOIN keywords
# =========================================================================


class TestClassifyReplyOnboarding:
    def test_join_keyword(self):
        assert classify_reply("join") == "ONBOARDING"

    def test_start_keyword(self):
        assert classify_reply("start") == "ONBOARDING"

    def test_sign_up_keyword(self):
        assert classify_reply("sign up") == "ONBOARDING"

    def test_case_insensitive(self):
        assert classify_reply("  JOIN ") == "ONBOARDING"

    def test_other_keywords_unchanged(self):
        assert classify_reply("yes") == "YES"
        assert classify_reply("stop") == "STOP"
        assert classify_reply("rerun") == "RERUN"


# =========================================================================
# Full onboarding conversation (mocked end-to-end)
# =========================================================================


class TestOnboardingFlow:
    @pytest.mark.asyncio
    async def test_full_flow(self, mock_deps):
        """Run the entire onboarding conversation and assert final state."""

        # ── 1. Alice texts JOIN ──
        await _run(mock_deps, ALICE_PHONE, "join")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_name
        assert mock_deps._sent[-1][1].startswith("Welcome to LDR Date Planner!")

        # ── 2. Alice gives her name ──
        await _run(mock_deps, ALICE_PHONE, "Alice")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_partner_phone
        assert alice_session.data["name"] == "Alice"
        assert "user_id" in alice_session.data
        assert "partner's phone" in mock_deps._sent[-1][1].lower()

        # ── 3. Alice gives Bob's phone ──
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_partner_name
        assert alice_session.data["partner_phone"] == BOB_PHONE

        # ── 4. Alice gives Bob's name ──
        await _run(mock_deps, ALICE_PHONE, "Bob")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_partner_confirm
        assert alice_session.data["partner_name"] == "Bob"

        # Bob's session was created and Bob was texted.
        bob_session = mock_deps._sessions[BOB_PHONE]
        assert bob_session.step == OnboardingStep.await_partner_confirm
        bob_sms = [body for to, body in mock_deps._sent if to == BOB_PHONE]
        assert len(bob_sms) == 1
        assert "Reply YES to confirm" in bob_sms[0]

        # ── 5. Bob texts YES ──
        await _run(mock_deps, BOB_PHONE, "yes")
        bob_session = mock_deps._sessions[BOB_PHONE]
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert bob_session.step == OnboardingStep.await_calendar_choice
        assert alice_session.step == OnboardingStep.await_calendar_choice
        assert len(mock_deps._couples) == 1
        assert mock_deps._couples[0].partner_b_user_id == mock_deps._users[2].id

        # ── 6. Alice chooses google ──
        await _run(mock_deps, ALICE_PHONE, "google")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_google_done
        assert "auth/google?user_id=" in mock_deps._sent[-1][1]

        # ── 7. Alice texts DONE ──
        await _run(mock_deps, ALICE_PHONE, "done")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_traits_activity

        # ── 8. Alice picks activity preferences ──
        await _run(mock_deps, ALICE_PHONE, "1, 3, 4")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_traits_energy
        assert alice_session.data["activity_prefs"] == [
            "virtual_tours", "cooking", "movies"
        ]

        # ── 9. Alice picks energy ──
        await _run(mock_deps, ALICE_PHONE, "2")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.complete
        assert "all set" in mock_deps._sent[-1][1].lower()

        # Traits were written to the couple.
        keys = {t["trait_key"] for t in mock_deps._written_traits}
        assert "activity_type_pref" in keys
        assert "energy_pref" in keys
        energy = next(
            t for t in mock_deps._written_traits if t["trait_key"] == "energy_pref"
        )
        assert energy["value"] == "medium"
        assert energy["source"] == TraitSource.explicit

    @pytest.mark.asyncio
    async def test_partner_declines(self, mock_deps):
        """Bob declining should not create a couple."""
        # Alice goes through identity phase.
        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        await _run(mock_deps, ALICE_PHONE, "Bob")

        await _run(mock_deps, BOB_PHONE, "no")
        assert len(mock_deps._couples) == 0
        assert "text JOIN" in mock_deps._sent[-1][1]

    @pytest.mark.asyncio
    async def test_apple_path(self, mock_deps):
        """Apple calendar path with a valid PROPFIND."""
        from app.services import calendar_connector

        # Patch connect_apple to succeed.
        calendar_connector.connect_apple = AsyncMock(return_value=(True, ""))

        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        await _run(mock_deps, ALICE_PHONE, "Bob")
        await _run(mock_deps, BOB_PHONE, "yes")

        # Alice chooses apple.
        await _run(mock_deps, ALICE_PHONE, "apple")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_apple_email

        await _run(mock_deps, ALICE_PHONE, "alice@icloud.com")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_apple_password
        assert alice_session.data["apple_email"] == "alice@icloud.com"

        await _run(mock_deps, ALICE_PHONE, "abcd-efgh-ijkl-mnop")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_traits_activity
        assert "Apple Calendar" in mock_deps._sent[-1][1]

        calendar_connector.connect_apple.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_apple_password_failure_retries(self, mock_deps):
        """A bad apple password should keep the session at await_apple_password."""
        from app.services import calendar_connector

        calendar_connector.connect_apple = AsyncMock(return_value=(False, "That password didn't work."))

        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        await _run(mock_deps, ALICE_PHONE, "Bob")
        await _run(mock_deps, BOB_PHONE, "yes")

        await _run(mock_deps, ALICE_PHONE, "apple")
        await _run(mock_deps, ALICE_PHONE, "alice@icloud.com")
        await _run(mock_deps, ALICE_PHONE, "wrong-pass")

        alice_session = mock_deps._sessions[ALICE_PHONE]
        # Still waiting for the correct password.
        assert alice_session.step == OnboardingStep.await_apple_password

    @pytest.mark.asyncio
    async def test_skip_path(self, mock_deps):
        """skip should advance directly to traits."""
        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        await _run(mock_deps, ALICE_PHONE, "Bob")
        await _run(mock_deps, BOB_PHONE, "yes")

        await _run(mock_deps, ALICE_PHONE, "skip")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_traits_activity

    @pytest.mark.asyncio
    async def test_google_done_only_recognized_at_right_step(self, mock_deps):
        """'done' should not advance when session is not at await_google_done."""
        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        await _run(mock_deps, ALICE_PHONE, "Bob")
        await _run(mock_deps, BOB_PHONE, "yes")

        # Alice texts 'done' at await_calendar_choice — should be treated as
        # an invalid calendar choice, not a DONE acknowledgement.
        await _run(mock_deps, ALICE_PHONE, "done")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_calendar_choice

    @pytest.mark.asyncio
    async def test_complete_session_routes_normal(self, mock_deps):
        """A complete session routes through the normal SMS flow.

        The user has a couple and a pending proposal — "hello" classifies
        as EDIT and goes to the edit_proposal / clarification path (not
        back to the onboarding_node).
        """
        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        await _run(mock_deps, ALICE_PHONE, "Bob")
        await _run(mock_deps, BOB_PHONE, "yes")
        await _run(mock_deps, ALICE_PHONE, "skip")
        await _run(mock_deps, ALICE_PHONE, "1")
        await _run(mock_deps, ALICE_PHONE, "2")

        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.complete

        # With a complete session, the user routes through the normal flow.
        # "hello" classifies as EDIT → edit_proposal (no LLM) → clarification.
        result = await _run(mock_deps, ALICE_PHONE, "hello")
        # The graph should not crash; errors may be present since deps.llm is
        # None, but the graph should still complete gracefully.
        assert "errors" in result

    @pytest.mark.asyncio
    async def test_invalid_traits_inputs(self, mock_deps):
        """Invalid trait replies should prompt again without advancing."""
        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        await _run(mock_deps, ALICE_PHONE, "Bob")
        await _run(mock_deps, BOB_PHONE, "yes")
        await _run(mock_deps, ALICE_PHONE, "skip")

        # Invalid activity selections.
        await _run(mock_deps, ALICE_PHONE, "9, 10")
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_traits_activity

        # Valid activities.
        await _run(mock_deps, ALICE_PHONE, "1, 2")
        assert mock_deps._sessions[ALICE_PHONE].step == OnboardingStep.await_traits_energy

        # Invalid energy.
        await _run(mock_deps, ALICE_PHONE, "7")
        assert mock_deps._sessions[ALICE_PHONE].step == OnboardingStep.await_traits_energy

        # Valid energy.
        await _run(mock_deps, ALICE_PHONE, "3")
        assert mock_deps._sessions[ALICE_PHONE].step == OnboardingStep.complete

    @pytest.mark.asyncio
    async def test_unknown_phone_number(self, mock_deps):
        """An unknown phone (no user, no session) should get a clarification."""
        result = await _run(mock_deps, "+15559999999", "hello")
        assert result.get("intent") == "UNKNOWN"
        assert "don't recognise" in result.get("clarification_msg", "")

        # The graph should still complete gracefully (send_clarification
        # falls back to from_phone when user_id is None).
        assert "clarification_sent" in result

    @pytest.mark.asyncio
    async def test_unknown_phone_join_creates_session(self, mock_deps):
        """An unknown phone texting JOIN should create a new session."""
        await _run(mock_deps, "+15559999999", "join")

        session = mock_deps._sessions.get("+15559999999")
        assert session is not None
        assert session.step == OnboardingStep.await_name

        # Should have received the welcome message.
        assert len(mock_deps._sent) == 1
        assert "Welcome to LDR Date Planner" in mock_deps._sent[0][1]

    @pytest.mark.asyncio
    async def test_interrupted_session_resumes(self, mock_deps):
        """A user who starts onboarding, gets interrupted, and texts JOIN
        again should resume from where they left off.

        The session persists in the DB. When the user texts JOIN a second
        time, the onboarding_node finds the existing session instead of
        creating a new one, and advances from the current step.
        """
        # ── Start onboarding, get to partner_phone step ──
        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")

        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_partner_phone
        assert "partner's phone" in mock_deps._sent[-1][1].lower()

        # ── User gets interrupted, texts JOIN again in a "new chat" ──
        # The JOIN keyword should route to onboarding_node, which finds the
        # existing session and resumes from the current step.
        await _run(mock_deps, ALICE_PHONE, "join")

        alice_session = mock_deps._sessions[ALICE_PHONE]
        # Session should still be at await_partner_phone (not reset to await_name).
        assert alice_session.step == OnboardingStep.await_partner_phone

        # The system should re-prompt for the current step, not start over.
        assert len(mock_deps._sent) >= 3  # welcome + name prompt + re-prompt
        # The last message should be the partner phone prompt (resumed).
        assert "partner's phone" in mock_deps._sent[-1][1].lower()

        # ── User can now continue from where they left off ──
        await _run(mock_deps, ALICE_PHONE, BOB_PHONE)
        alice_session = mock_deps._sessions[ALICE_PHONE]
        assert alice_session.step == OnboardingStep.await_partner_name
        assert alice_session.data["partner_phone"] == BOB_PHONE

    @pytest.mark.asyncio
    async def test_interrupted_session_does_not_duplicate_user(self, mock_deps):
        """If a user is interrupted at await_name after creating their user
        account, resuming with JOIN should not create a duplicate user.

        The user account already exists from the first attempt; the
        onboarding_node should skip user creation.
        """
        # ── Start onboarding, complete the name step ──
        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")

        assert len(mock_deps._users) == 1  # Alice's user created
        alice_user_id = mock_deps._users[1].id

        # ── User texts JOIN again ──
        await _run(mock_deps, ALICE_PHONE, "join")

        # Should still only have one user — no duplicate.
        assert len(mock_deps._users) == 1
        assert mock_deps._users[1].id == alice_user_id
        assert mock_deps._users[1].name == "Alice"

    @pytest.mark.asyncio
    async def test_session_id_is_persistent_stable_key(self, mock_deps):
        """Onboarding sessions have a stable primary key (id) that persists
        across interruptions. The session is keyed by phone_number, and the
        id is assigned once on creation.

        This test verifies that the same session object is returned after
        interruption, not a new one with a different id.
        """
        # ── Start onboarding ──
        await _run(mock_deps, ALICE_PHONE, "join")
        first_session = mock_deps._sessions[ALICE_PHONE]
        first_id = first_session.id

        # ── Interrupt and resume ──
        await _run(mock_deps, ALICE_PHONE, "join")
        resumed_session = mock_deps._sessions[ALICE_PHONE]

        # Same session object (same id, same phone).
        assert resumed_session.id == first_id
        assert resumed_session.phone_number == ALICE_PHONE

    @pytest.mark.asyncio
    async def test_known_user_during_onboarding(self, mock_deps):
        """A user who has partially completed onboarding (has a user row
        but no couple yet) should be routed to onboarding_node, not the
        normal SMS flow.

        This covers the case where Alice created her user account (step 2)
        but hasn't finished onboarding. Any message from her should go to
        onboarding_node, not to the couple/proposal flow.
        """
        # ── Alice starts onboarding and creates her user account ──
        await _run(mock_deps, ALICE_PHONE, "join")
        await _run(mock_deps, ALICE_PHONE, "Alice")

        assert len(mock_deps._users) == 1  # Alice has a user account

        # ── Alice sends a non-JOIN message mid-onboarding ──
        # This should route to onboarding_node, not to the normal flow.
        result = await _run(mock_deps, ALICE_PHONE, "+15553333333")

        alice_session = mock_deps._sessions[ALICE_PHONE]
        # Alice should have advanced to await_partner_name.
        assert alice_session.step == OnboardingStep.await_partner_name
        assert alice_session.data["partner_phone"] == "+15553333333"

        # No errors — the onboarding_node handled it correctly.
        assert len(result.get("errors", [])) == 0