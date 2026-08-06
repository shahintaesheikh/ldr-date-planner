# LDR Date Planner — Dev Plan

## 1. Problem framing

Not a chat app. A constraint-satisfaction + ideation agent that collapses "let's find time to plan something" into "approve or don't." Two calendars in, one specific proposal out, delivered where friction is lowest (SMS).

## 2. System architecture

```
┌─────────────┐      ┌──────────────────┐      ┌─────────────────┐
│  Web App     │      │   FastAPI Core    │      │   Postgres       │
│ (React)      │─────▶│  - REST endpoints │◀────▶│  - couples       │
│ OAuth setup  │      │  - cron trigger   │      │  - calendar_conn │
│ profile setup│      │  - SMS webhook    │      │  - traits        │
└─────────────┘      └────────┬─────────┘      │  - catalog       │
                               │                 │  - proposals     │
                               ▼                 │  - feedback      │
                     ┌──────────────────┐        └─────────────────┘
                     │  LangGraph Agent  │
                     │  (ideation graph) │
                     └────────┬─────────┘
                               │
                   ┌───────────┴───────────┐
                   ▼                       ▼
          ┌─────────────────┐    ┌──────────────────┐
          │ Calendar Adapters│    │  Twilio (SMS)    │
          │ - Google Cal API │    │  outbound + inbound
          │ - CalDAV (iCloud)│    │  webhook parsing
          └─────────────────┘    └──────────────────┘
```

## 3. Data model (Postgres)

- **users**: `id`, `name`, `phone_number` (E.164, for Twilio), `timezone`, `created_at`
- **couples**: `id`, `partner_a_user_id` (fk users), `partner_b_user_id` (fk users), `created_at`
- **calendar_connections**: `id`, `user_id` (fk users), `provider` (google|caldav), `oauth_token` / `caldav_credentials`, `refresh_token`, `status`
- **traits**: `id`, `couple_id`, `trait_key` (e.g. `activity_type_pref`, `energy_pref`), `value`, `weight`, `source` (implicit|explicit), `updated_at` — kept as an EAV table, not columns on `couples`, because the trait set is open-ended: `ideate_activity` can add new trait keys over time, and each trait needs independent `weight`/`source`/`updated_at`, which columns can't express without a shadow table anyway (i.e. EAV in disguise, with migrations on top). Keyed to `couple_id` per the earlier unified-model decision, not per-user.
- **date_activities** (catalog): `id`, `name`, `description`, `est_duration_min`, `cost_tag` (unused v1), `source` (seed|llm|user), `tags[]`, `embedding` (vector, pgvector) — catalog lookups are semantic (RAG), not flat tag filtering; see agent dev plan for tool-level detail
- **proposals**: `id`, `couple_id`, `activity_id`, `proposed_start`, `proposed_end`, `status` (pending|confirmed|rejected|expired), `confirmed_by` (fk users), `created_at`
- **feedback**: `id`, `proposal_id`, `rating` (nullable, explicit), `implicit_signal` (accept|reject|rerun|mute), `created_at`
- **sms_thread**: `id`, `proposal_id` (fk proposals), `user_id` (fk users), `raw_body`, `created_at`

## 4. LangGraph flow (ideation graph, refer to ldr-date-agent-devplan.md)and

Nodes, in order:

1. **fetch_availability** — pulls busy/free blocks from both calendar adapters, normalizes to UTC, converts to each partner's local tz for downstream display.
2. **find_overlap_windows** — constraint solve: intersect free blocks, filter windows ≥ 1hr, rank by proximity to "soon" (weighted toward the next 3-5 days for on-demand, wider for scheduled).
3. **load_traits** — pulls current couple trait vector from DB.
4. **ideate_activity** — LLM node: given trait vector + available window lengths, runs semantic catalog search first, falls back to web search only below a similarity threshold, and writes back to catalog (embedded, dedup-checked) with `source=llm` if novel. Full tool-level breakdown in the agent dev plan.
5. **estimate_duration** — LLM reasons about actual time needed for the chosen activity (floor 1hr), refines which overlap window to use.
6. **compose_proposal** — writes `proposals` row (status=pending), formats SMS copy.
7. **deliver_sms** — sends via Twilio to both partners.

Separate small graph for **inbound SMS handling** (single Twilio webhook, not the ideation graph — one endpoint handles both keyword and freeform replies, the split happens inside the graph, not at the transport layer):

- `classify_intent` — replaces a flat `parse_reply` node. Fast-path regex match against YES/NO/RERUN/STOP first; anything else routes to the NL edit path.
- `route` (keyword path):
  - YES → lock proposal, write calendar events to both calendars, notify other partner, log implicit feedback (accept).
  - NO → log implicit feedback (reject), do nothing further (proposal expires).
  - RERUN → log implicit feedback (reject), re-invoke ideation graph from `ideate_activity` with prior activity excluded.
  - STOP → set couple-level `suggestions_muted=true`, skip scheduled trigger until re-enabled via web app.
- `edit_proposal` (NL path) — LLM with a constrained tool schema (not open JSON editing, to avoid schema drift/hallucinated fields):
  ```python
  class ProposalEdit(BaseModel):
      new_start_time: datetime | None = None
      new_activity_id: str | None = None
      duration_override_min: int | None = None
      reasoning: str  # audit/debug only, not shown to user
  ```
  LLM receives the current proposal JSON + raw `Body` text, calls the tool with only the fields the user actually wants changed.
- `validate_edit` — re-runs the overlap-window check against both calendars for any `new_start_time` before persisting. Not optional: a freeform "let's do 9pm instead" can name a time neither calendar actually has free.
- On successful validation, reuses `compose_proposal` and `deliver_sms` from the ideation graph rather than duplicating them.

**Multi-turn edit state**: a reply like "push it later" followed by "actually Sunday not Saturday" needs conversation state scoped to the *pending proposal*, not just the couple. The `sms_thread` table (schema in section 3) gives `classify_intent` a way to know which proposal a given reply amends — otherwise a reply arriving after a proposal has expired has nothing to attach to.

## 5. Scheduling trigger

Cron job (e.g. APScheduler or a simple cloud scheduler hitting a `/trigger-cadence` endpoint) runs weekly by default, invokes ideation graph per couple unless muted. On-demand endpoint `/propose` triggers the same graph immediately.

## 6. Calendar adapters

- **Google**: `google-api-python-client`, OAuth2 flow via web app, refresh token stored encrypted.
- **Apple/iCloud**: `caldav` Python package, app-specific password stored encrypted (not OAuth — user generates it manually in Apple ID settings, entered once via web app).
- Common interface: `get_busy_blocks(start, end) -> list[TimeRange]` and `create_event(start, end, title) -> event_id`, so the rest of the system never touches provider-specific logic.

## 7. Build phases

| Phase | Scope | Notes |
|---|---|---|
| 0 | Repo scaffold, Postgres schema, FastAPI skeleton | AGENTS.md conventions carried over |
| 1 | Google Calendar adapter + CalDAV adapter for Apple, in parallel | Moved up: CalDAV write reliability (event creation, not just reads) is the highest-risk external dependency in the whole system — validate both adapters against the common interface before building anything downstream of them, rather than discovering iCloud quirks late |
| 2 | Catalog + trait store + seed data | ~15-20 seed activities across types (co-watch, cook-along, game, virtual tour, etc.) |
| 3 | LangGraph ideation graph (nodes 1-6) | Test with mocked calendar data before wiring live adapters |
| 4 | Twilio integration — outbound + inbound webhook, keyword path only (YES/NO/RERUN/STOP) | Ship the deterministic path first; reply parsing is the fragile part — test keyword variants |
| 5 | NL edit path — `classify_intent` routing, `edit_proposal` tool-calling node, `validate_edit`, `sms_thread` state | Builds on the keyword path once it's stable; validate_edit reuses the same calendar adapters from Phase 1 |
| 6 | Feedback loop wiring | Confirm implicit + explicit signals actually update trait weights |
| 7 | Cron trigger + mute/unmute | Last piece — ties scheduled cadence to the on-demand path already built |

## 8. Open technical risks

- **CalDAV write reliability**: iCloud's CalDAV implementation is less forgiving than Google's API. Moved to Phase 1 — test event creation (not just reads) before any downstream node depends on the calendar adapter interface.
- **SMS reply ambiguity**: resolved architecturally — `classify_intent` fast-paths exact keywords and routes everything else to the NL edit path (Phase 5), so ambiguous freeform replies have a defined destination instead of falling through.
- **NL edit scope creep**: the `ProposalEdit` tool schema is intentionally narrow (time/activity/duration only). Resist the urge to let the LLM touch arbitrary proposal fields directly — schema drift here is what makes `validate_edit` unreliable.
- **Trait cold start**: with no history, `ideate_activity` has nothing to weight against for the first few proposals — seed with an initial manual preference form (short) during onboarding rather than starting from zero.
