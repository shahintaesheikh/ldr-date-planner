# LDR Date Planner — Agent Dev Plan

Companion to the main dev plan. Scoped to the LangGraph agent only: node behavior, tool exposure per node, agent-relevant data models, and risks specific to the agentic layer (not calendar/SMS infra, which is covered in the main doc).

## 1. Framing

Most of this system is deterministic (calendar fetch, overlap solving, SMS delivery) and doesn't need agent judgment. Two nodes are actually agentic — `ideate_activity` and `edit_proposal` — everything else is plumbing around them. Tools are scoped per-node, not global, so each node only has access to what its job requires.

## 2. Graph architecture (agent-relevant nodes only)

```
Ideation graph:
fetch_availability → find_overlap_windows → load_traits → ideate_activity → estimate_duration → compose_proposal → deliver_sms
                                                                  │                    │
                                                             [tools: web_search,   [tools: query_activity_history
                                                              catalog_search,       — deferred to Phase 6]
                                                              add_to_catalog]

Inbound SMS graph:
classify_intent → route(keyword path) OR edit_proposal → validate_edit → compose_proposal → deliver_sms
                                              │
                                        [tool: ProposalEdit
                                         structured schema]
```

## 3. Data models relevant to the agent

(Full schema in the main dev plan — this is the subset the agent nodes actually read/write.)

- **traits**: `id`, `couple_id`, `trait_key`, `value`, `weight`, `source` (implicit|explicit), `updated_at` — read by `load_traits`, indirectly informs `ideate_activity`.
- **date_activities** (catalog): `id`, `name`, `description`, `est_duration_min`, `cost_tag`, `source` (seed|llm|user), `tags[]`, `embedding` (vector, pgvector) — read via semantic search by `catalog_search`, written by `add_to_catalog` (embedded on insert, dedup-checked via cosine similarity before write).
- **proposals**: `id`, `couple_id`, `activity_id`, `proposed_start`, `proposed_end`, `status`, `confirmed_by`, `created_at` — written by `compose_proposal`, read/amended by `edit_proposal`.
- **feedback**: `id`, `proposal_id`, `rating`, `implicit_signal`, `created_at` — not read by the agent in v1; becomes the source for `query_activity_history` once that tool is added in Phase 6.
- **sms_thread**: `id`, `proposal_id`, `user_id`, `raw_body`, `created_at` — read by `classify_intent` to resolve which proposal a reply amends.

## 4. Node-by-node

### fetch_availability
Deterministic — pulls busy/free blocks from both calendar adapters, normalizes to UTC. No tools; direct adapter calls, not agent reasoning.

### find_overlap_windows
Deterministic constraint solve — intersects free blocks, filters windows ≥1hr, ranks by proximity. No tools.

### load_traits
Deterministic DB read — pulls current trait vector for the couple by `couple_id`. **Not a RAG node** — this is a bounded key-value lookup (typically 10-30 rows), not semantic retrieval. No tools, no embeddings involved.

### ideate_activity — agentic, tool-bearing
The core reasoning node. Given the trait vector and available window lengths, picks a catalog activity or proposes a novel one.

**Tools exposed:**
- **`catalog_search`** — semantic (RAG) search over `date_activities`, not flat tag filtering. Activities are embedded (`name + description + tags`) via `pgvector` on insert; the query is built from the couple's top-weighted traits + target duration rather than exact tag matches, so fuzzy trait signals ("prefers low-key over high-energy") can actually surface a relevant fit. Called first, always, with a similarity threshold (start ~0.75) gating whether results are good enough to use.
- **`web_search`** (Anthropic server-side tool, `web_search_20250305`) — fallback, only triggered when `catalog_search` results fall below the similarity threshold or don't fit the duration window. Sequential, not parallel with catalog_search — this ordering is what keeps the two tools complementary instead of redundant.
- **`add_to_catalog`** — write tool, called only when the LLM lands on a genuinely novel activity via web search. Sets `source=llm`, embeds the new entry immediately so it's searchable by `catalog_search` on future runs — this is the loop-closing step: web search results become catalog coverage over time, so web search usage should trend down as the catalog matures. **Dedup check before insert**: compute the candidate's embedding, compare cosine similarity against existing catalog entries; if similarity exceeds a threshold (e.g. 0.92), skip the insert and return the existing entry's id instead of creating a near-duplicate row (e.g. "virtual cooking class" vs "cook together over video call").

### estimate_duration — agentic, no tools in v1
Reasons about actual time needed for the chosen activity (floor 1hr), refines which overlap window to use. No tools in v1 — pure reasoning over the activity description and catalog's `est_duration_min` as a prior.

**Deferred tool**: `query_activity_history` — pulls actual logged durations from past `feedback` rows for that activity type, grounding estimates in real data instead of the LLM guessing from the description alone. Explicitly deferred to Phase 6 (feedback-loop phase) — with no history in the early weeks, this tool would just return empty results, adding a call with no payoff.

### compose_proposal
Deterministic — formats the SMS copy and writes the `proposals` row. No tools.

### deliver_sms
Deterministic — Twilio send. No tools.

### classify_intent
Deterministic-first — regex match against YES/NO/RERUN/STOP. Only routes to the agentic path if none match. No tools itself; it's a router.

### edit_proposal — agentic, tool-bearing
Given the current proposal JSON and the raw freeform SMS body, determines what the user wants changed.

**Tool exposed:**
- **`ProposalEdit`** — a constrained structured-output schema (start time, activity id, duration override, reasoning), not open JSON editing. The LLM can only set the fields the schema defines, which prevents schema drift and hallucinated fields creeping into the proposal record.

### validate_edit
Deterministic — re-runs the overlap-window check against both calendars for any proposed `new_start_time` before persisting. No tools; reuses the same calendar adapters as `fetch_availability`.

## 5. Tools not included, and why

- **Weather / maps / location tools** — no consumer in the graph as scoped (virtual dates, no location-dependent activity selection currently).
- **Budget/pricing APIs** — `cost_tag` field exists in the schema but budget was explicitly scoped out of v1; a pricing tool would have nothing to feed.
- **A second, separate web search integration (Tavily/SerpAPI etc.)** — redundant. The Anthropic server-side `web_search` tool covers this with no added infra, since the agent is already running on Claude.

## 6. Agent-specific technical risks

- **`catalog_search` vs `web_search` ordering discipline**: resolved architecturally by the similarity-threshold gate — `catalog_search` runs first and `web_search` only fires below threshold. Still needs enforcement in the node's system prompt (the model shouldn't be able to skip straight to web search "for freshness"), but the ordering is no longer left to model judgment alone.
- **`add_to_catalog` duplicate/quality control**: mitigated by the embedding-similarity dedup check before insert (see section 4). Remaining risk is threshold tuning — too low and genuinely distinct activities get merged into one entry; too high and near-duplicates slip through. Expect to tune the 0.92 starting point after seeing real insert volume.
- **Embedding drift / stale index**: if the embedding model changes (version upgrade, provider switch) after the catalog has real entries, old embeddings become incomparable to new query embeddings without a full re-embed pass. Worth tracking an `embedding_model_version` alongside the vector column so a mismatch is detectable rather than silently degrading match quality.
- **`ProposalEdit` schema narrowness vs user intent mismatch**: the structured schema only covers time/activity/duration. A freeform reply that doesn't map to any of those fields (e.g. "can we invite my sister too") has no defined outcome — the node will either silently drop the request or the LLM will try to force it into an unrelated field. Needs an explicit "no matching field, fall back to a plain reply asking for clarification" path rather than letting the tool call happen with irrelevant reasoning.
- **`estimate_duration` cold-start accuracy**: with no `query_activity_history` tool in v1, early duration estimates are pure LLM inference from activity descriptions — likely fine for common activity types, more error-prone for novel `add_to_catalog` entries with sparse descriptions. Not a blocker, but expect early proposals to occasionally mis-size the window until Phase 6's history tool comes online.
- **Web search relevance drift**: "long distance date ideas" as a query surfaces generic listicle content that may not respect the couple's actual trait vector or available duration. The tool call needs to be constrained by a query built from trait tags + window length, not a static search string, or the results won't actually be usable inputs to the selection step.
