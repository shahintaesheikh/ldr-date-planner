# LDR Date Planner — Agent Agent Design Decisions

## DI pattern: `GraphDeps` per-invocation

**Decision:** Both graphs compiled once at module level; dependencies injected via `config["configurable"]["deps"]` at invoke time. `GraphDeps(db=session).resolved()` fills default services.

**Why:** Keeps graphs stateless and testable — tests inject mocks for everything without module-level monkey-patching. Matches the existing per-request service pattern (services scoped to `AsyncSession`, instantiated per-request). The FastAPI layer constructs a `GraphDeps` once per request and passes it in config.

## Manual tool-execution loop instead of `create_react_agent`

**Decision:** `ideate_activity` runs a bounded 8-step manual loop: `bound.ainvoke(messages)` → inspect `tool_calls` → execute tools → append `ToolMessage` → repeat. Not `create_react_agent`.

**Why:** `create_react_agent` is deprecated in LangGraph 1.2+ (moved to `langchain.agents`). The manual loop is testable without mocking the entire react-agent message machinery, and it explicitly enforces the tool-calling gate (catalog first, web only below similarity threshold) via the system prompt + loop logic rather than leaving ordering to the LLM's discretion.

## Availability math as pure functions

**Decision:** `free_blocks`, `intersect_windows`, `rank_windows` are pure functions operating on plain dicts and dataclasses. Zero dependencies on LangGraph, LangChain, or the database.

**Why:** Tested in isolation without mocks. The existing calendar adapters return UTC timezone-aware datetimes; these functions work on that representation natively. `free_blocks` computes the inverse of busy blocks within a range, `intersect_windows` merges two partners' free blocks, `rank_windows` scores by "soonness" for on-demand vs chronological for scheduled.

## `OverlapWindow` and `ProposalDraft` as dataclasses

**Decision:** Plain `@dataclass` for `OverlapWindow(start, end, duration_min)` and `ProposalDraft(activity_id, activity_name, description, start, end, duration_min)`. Stored as state values in the TypedDict.

**Why:** These are transient, value-like objects that carry computed data between nodes. Dataclasses give clean repr/equality/construction without Pydantic overhead. The `OverlapWindow.__post_init__` auto-computes duration from start/end for convenience.

## `ProposalEdit` as a narrow Pydantic model (not freeform JSON)

**Decision:** `new_start_time | None`, `new_activity_id | None`, `duration_override_min | None`, `reasoning: str`. Exposed as a bound tool, not via `with_structured_output`.

**Why:** Prevents schema drift and hallucinated fields (dev plan risk #5). Only three mutable fields mirror what an SMS reply can realistically express. `is_noop()` detects when the model called the tool but changed nothing, which routes to clarification instead of silently no-opping.

## Lazy import for Twilio (`TwilioSMSGateway`)

**Decision:** `from twilio.rest import Client` inside `_client_factory()`, guarded by try/except ImportError. `LoggingSMSGateway` is the default fallback when no credentials are configured.

**Why:** The agent graph must be importable without the `twilio` package installed (Phase 4 dependency, not yet added to the environment). Tests never need Twilio. The `build_sms_gateway(settings)` factory switches between production and dev at construction time.

## Separate schemas and services for proposals and SMS threads

**Decision:** Full Pydantic schemas (`ProposalCreate/Read/Update`, `SMSThreadCreate/Read`) and session-scoped stores (`ProposalStore`, `SMSThreadStore`, `CoupleStore`, `FeedbackStore`).

**Why:** Consistent with the existing pattern (`TraitStore`, `CatalogService`). Nodes never touch the ORM directly — they call store methods. This keeps test boundaries clean: mock the store method, not the SQLAlchemy session.

## `classify_intent` priority: RERUN > STOP > YES > NO > EDIT

**Decision:** Deterministic regex matching. RERUN checked first so "no, try again" routes correctly. STOP second to catch unsubscribe intent. YES/NO last among keywords. Everything else → EDIT (NL path) or UNKNOWN (fallback).

**Why:** Keyword routing must be deterministic by design (dev plan: "deterministic-first — regex match against YES/NO/RERUN/STOP"). Priority order resolves ambiguous messages like "no, try again" correctly.

## `validate_edit` always re-checks calendars for time changes

**Decision:** `_slot_free()` re-fetches busy blocks from both partners' calendars with a widened probe range (start-2h to end+2h) to catch adjacent overlaps. Not optional — even a 1-line SMS like "let's do 9pm" could name a time neither calendar actually has free.

**Why:** Dev plan explicitly requires this: "not optional: a freeform 'let's do 9pm instead' can name a time neither calendar actually has free."

## RERUN re-invokes the ideation graph from within the SMS graph

**Decision:** `route_rerun` calls `ideation_graph.ainvoke(...)` with the same deps but excludes the prior activity. The ideation graph's `deliver_sms` sends the new proposal.

**Why:** The RERUN path must produce a full new proposal, not just skip one step. Reusing the ideation graph avoids duplicating the 7-node pipeline. The prior activity id is passed via `exclude_activity_id` in the state.

## `compose_proposal` and `deliver_sms` are shared between both graphs

**Decision:** Both reside in `app/agent/common.py`, imported by both graph modules. `compose_proposal` handles create (ideation) and update (edit path) distinguished by `state["proposal_id"]`.

**Why:** The dev plan states "reuses `compose_proposal` and `deliver_sms` from the ideation graph rather than duplicating them." Shared import avoids code drift.

## Error-resilient design — nodes short-circuit gracefully

**Decision:** Every node checks preconditions and returns `{"errors": [...]}` instead of raising. `compose_proposal` wraps draft-construction in try/except. `deliver_sms` returns early if draft is missing.

**Why:** LangGraph continues executing the next node even when a prior node returns errors. Without checks, downstream nodes would crash on missing data, obscuring the actual failure point.