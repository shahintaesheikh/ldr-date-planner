# SMS-Native Onboarding Flow

Onboard users entirely via SMS (plus the existing Google OAuth browser link).
No static HTML pages, no React frontend — the Google link is the only browser
detour.

---

## Table: `onboarding_sessions`

```python
class OnboardingStep(str, Enum):
    await_name              # 1. what's your name?
    await_partner_phone     # 2. partner's phone number?
    await_partner_name      # 3. partner's name?
    await_partner_confirm   # 4. waiting for partner to reply YES
    await_calendar_choice   # 5. google / apple / skip?
    await_google_done       # 6a. waiting for DONE after OAuth link
    await_apple_email       # 6b. waiting for Apple ID email
    await_apple_password    # 6c. waiting for app-specific password
    await_traits_activity   # 7. activity preferences?
    await_traits_energy     # 8. energy level?
    complete                # 9. done


class OnboardingSession(Base):
    __tablename__ = "onboarding_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phone_number: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False
    )
    step: Mapped[OnboardingStep] = mapped_column(
        Enum(OnboardingStep, name="onboarding_step"),
        nullable=False,
        server_default=OnboardingStep.await_name.name,
    )
    # Accumulated data (JSONB) — name, partner_phone, apple_email, traits, etc.
    data: Mapped[dict] = mapped_column(
        JSON, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False
    )
```

---

## Full Conversation

### Phase 1 — Identity

```
[user]  JOIN
[app]   Welcome to LDR Date Planner! What's your name?
[user]  Alice
[app]   Nice to meet you, Alice! What's your partner's phone number?
[user]  +15551234567
[app]   And what's their name?
[user]  Bob
[app]   📨 We've texted Bob to confirm. You're on hold until they reply.
```

Bob receives:

```
[app]   Alice wants to connect with you on LDR Date Planner!
        Reply YES to confirm.
[bob]   YES
[app]   You're connected with Alice! Do you use Google Calendar or Apple
        Calendar? Reply "google", "apple", or "skip" to do it later.
```

When Bob replies YES, the system:
1. Creates the `couples` row (partner_a = Alice's user_id, partner_b = Bob's user_id)
2. Creates Bob's `onboarding_session` (if he doesn't have one already) with `step = await_calendar_choice`
3. Advances Alice's session to `await_calendar_choice` as well (both partners independently pick their calendar)

### Phase 2 — Calendar

**Google path:**

```
[user]  google
[app]   📱 Open this link in your browser to connect Google Calendar:
        https://example.com/auth/google?user_id=42
        Text DONE when you're finished.
```

The user taps the link, completes Google OAuth in their phone browser, and
returns to SMS. The OAuth callback redirects to a simple "Connected!" page
(not raw JSON).

```
[user]  DONE
[app]   Connected! 🎉 What kind of dates do you like? ...
```

`DONE` is only recognized as a keyword when the session is at
`await_google_done` — checked inside `onboarding_node`, not in the global
`classify_reply` classifier.

**Apple path:**

```
[user]  apple
[app]   Apple Calendar needs an app-specific password (not your normal
        Apple password). To create one: sign in at appleid.apple.com →
        Security → App-Specific Passwords → generate one. Then text me
        your Apple ID email.
[user]  alice@icloud.com
[app]   Got it. Now text me the app-specific password (format:
        xxxx-xxxx-xxxx-xxxx).
[user]  abcd-efgh-ijkl-mnop
[app]   ✅ Connected to Apple Calendar! ...
```

At `await_apple_password`, the system:
1. Constructs a `CalDAVAdapter(url="https://caldav.icloud.com/", username=..., password=...)`
2. Attempts a real `get_busy_blocks(now, now+1day)` PROPFIND to validate the password
3. If it fails: `[app] That password didn't work. Double-check it at appleid.apple.com and try again.`
4. If it succeeds: stores the `CalendarConnection` row and advances the step

**Skip path:**

```
[user]  skip
[app]   No problem — you can connect later. Now, what kind of dates...
```

No `CalendarConnection` row is created. The agent already treats a partner
with no calendar as "fully free."

### Phase 3 — Traits

```
[app]   What kind of dates do you like? Reply numbers:
        1 = Virtual tours, 2 = Games, 3 = Cooking, 4 = Movies, 5 = Outdoors
[user]  1, 3, 4
[app]   How much energy for a date? 1 = Low (chill), 2 = Medium, 3 = High
[user]  2
```

At `await_traits_energy`, the system writes traits via `TraitStore`
(`apply_signal_update` with default weight), then marks the session as
`complete`.

```
[app]   You're all set! We'll send you a date idea soon. 🎉
```

---

## Code Changes

### New files

| File | Purpose |
|------|---------|
| `app/services/onboarding_store.py` | CRUD for `onboarding_sessions` — `get_by_phone()`, `create()`, `advance_step(phone, next_step, data_updates)`, `delete()` |
| `app/services/calendar_connector.py` | `connect_google(user_id, code)` — validates Google OAuth token, stores `CalendarConnection`. `connect_apple(user_id, email, password)` — validates via real PROPFIND, stores `CalendarConnection` |

### Modified files

| File | Change |
|------|--------|
| `app/models/couple.py` | Add `OnboardingStep` enum + `OnboardingSession` model |
| alembic migration | New `onboarding_sessions` table |
| `app/agent/sms_graph.py` | Add `onboarding_node`; `classify_intent` routes unknown phones with an active session → `onboarding_node` instead of "I don't recognise that phone number" |
| `app/agent/sms_graph.py` | `classify_reply` gets a `JOIN` keyword match → returns `ONBOARDING` intent |
| `app/agent/sms_graph.py` | `_route_on_intent` gains `ONBOARDING` → `onboarding_node` entry |
| `app/agent/sms_graph.py` | State machine logic in `onboarding_node` — reads session step, parses reply, sends next SMS, advances step |
| `app/routers/google_auth.py` | Change callback to return a `RedirectResponse` to a simple "Connected" message instead of JSON |
| `app/services/couple_store.py` | Add `create_couple(partner_a_id, partner_b_id)` if needed |
| `app/adapters/sms.py` | No change — gateway already exists |

### New graph node: `onboarding_node`

A single async function (not a sub-graph) that:

1. Reads the session by `from_phone`
2. Switch-case on `session.step`:
   - `await_name` → store `data.name`, send partner phone prompt, advance to `await_partner_phone`
   - `await_partner_phone` → store `data.partner_phone`, send partner name prompt, advance to `await_partner_name`
   - `await_partner_name` → store `data.partner_name`, text partner with confirmation request, create partner's session (`await_partner_confirm`), advance to `await_partner_confirm`
   - `await_partner_confirm` → if "YES": create `couples` row, advance both sessions to `await_calendar_choice`. If "NO": send "That's okay, text JOIN when you're ready."
   - `await_calendar_choice` → branch on "google"/"apple"/"skip"
   - `await_google_done` → if "DONE": advance to `await_traits_activity`. Otherwise: "Text DONE when you've finished in the browser."
   - `await_apple_email` → store `data.apple_email`, advance to `await_apple_password`
   - `await_apple_password` → validate via real PROPFIND, on success store `CalendarConnection` + advance to `await_traits_activity`, on failure send "That password didn't work..."
   - `await_traits_activity` → parse comma-separated numbers, store as traits, advance to `await_traits_energy`
   - `await_traits_energy` → parse 1/2/3, write all traits via `TraitStore`, mark `complete`, send "You're all set!"
   - `complete` → no-op, route to existing `deliver_sms` flow

### `classify_intent` changes

Before the "I don't recognise that phone number" return, add:

```python
if user is None:
    session = await deps.onboarding_store.get_by_phone(state["from_phone"])
    if session is not None:
        return {"intent": "ONBOARDING", ...}
    return {"intent": "UNKNOWN", ...}
```

And `classify_reply` gets a pre-check for `JOIN`:

```python
def classify_reply(raw_body: str) -> str:
    body = raw_body.strip().lower()
    if body in ("join", "start", "sign up"):
        return "ONBOARDING"
    ...
```

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Apple password validated at entry** | A real PROPFIND catches typos immediately. The user re-texts a corrected password in the same turn, rather than finding out on the first date-availability check days later. |
| **`DONE` scoped to `await_google_done`** | Prevents `classify_reply` from misrouting freeform messages containing "done" (e.g., "we're done planning"). Only checked inside `onboarding_node`, not in the global keyword classifier. |
| **Single `onboarding_node` function, not a sub-graph** | The state machine is deterministic (switch-case), not agentic. A single node with a step switch is simpler and easier to debug than a sub-graph with conditional edges. |
| **Each partner onboards independently** | Alice and Bob each provide their own calendar and traits. The couple row is created once Bob confirms, then both independently advance through calendar and traits. |
| **No `onboarding_sessions` cleanup** | `complete` sessions remain in the DB for audit. A background job can purge `updated_at < now - 30 days` rows. |

---

## What's deliberately out of scope

- **Calendar disconnect/reconnect** — handled by the existing `calendar_connections` table status column. No SMS flow for this; user reconnects via the OAuth link again.
- **Unmute** — the dev plan says unmute happens via web app. If SMS unmute is needed later, add a `START` keyword to `classify_reply`.
- **Multi-couple users** — v1 assumes one couple per user. The RSVP confirmation flow breaks if a user belongs to multiple couples.
- **Onboarding timeout** — no expiry on sessions. User can text days later and pick up where they left off.