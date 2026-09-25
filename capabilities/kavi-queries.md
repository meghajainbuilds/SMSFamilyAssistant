---
name: kavi-queries
status: proposed
capability_type:
  - judgment
  - agentic
  - two-way
  - generative
  - retrieval
owner: Megha (PM) + Claude (engineer)
last_updated: 2026-06-02
---
# kavi-queries

> **Status note:** spec drafted 2026-06-02 evening after Megha asked "if Kavi is my assistant and iMessage is my surface and Kavi has access to MS To Do, why can't it surface those tasks to me?" — see Phase 1 rollup rebuild changelog entry. Build kickoff is unblocked: the MS Graph tools all exist (`list_open_todo_tasks`, `list_completed_todo_tasks` — the latter shipped in the Phase 1 rebuild). v1 scope is MS To Do queries only; calendar queries and web queries are Phase 3 (separate net-new MS Graph scope + url-summarizer respectively).

## TL;DR

Megha asks Kavi a question in iMessage that requires reading the household's data sources. Today (v1): MS To Do. Kavi classifies the intent, calls the appropriate tool, composes a reply in voice honoring the persona spec. Closes the "Kavi punted to MS To Do" failure mode where iMessage stops being the surface and Megha context-switches to verify what Kavi could have told her.

## Open questions

<!-- private:kq-001 -->- **v1 use cases beyond list + task lookup.** Megha named "show me the 7 tasks added today" (list_query) and "pull up the Erin Walsh task" (task_lookup_query). Are there other reactive query shapes in v1? Status-roll queries ("what's still pending from yesterday")? Categorical queries ("what's school-related right now")? Ship the two named intents and let real usage surface more.<!-- /private -->
- **Compose shape on list_query when result > 5 tasks.** The persona's 120-char cap forces compression. Phase 1 rollup chose "count + axis + 1-2 named items." Does Megha want the same shape here, or does an explicit list-query justify a higher cap (e.g., 240 char) since she explicitly asked for the list? **Engineer's recommendation:** keep the 120-char cap; voice consistency wins. Megha can ask "name three more" as a follow-up.
- **Migration to proactive.** When Megha asks the same list_query repeatedly (e.g., every morning at 8 AM), does Kavi anticipates auto-promote it to a recurring rollup? Boundary question with kavi-anticipates capability. Out of scope for v1.

## Why now

- **What's broken or at risk:** Cagan VVUF + AI risk. **Value:** today Kavi is a notification surface (rollups + Q&As) but not a query surface. Megha catches herself opening MS To Do to verify Kavi's claims, which defeats the chief-of-staff delegation. **Usability:** the gap surfaced explicitly on the 2026-06-02 9 PM rollup — Kavi said "7 new tasks in MS To Do" and Megha replied "show me the 7 added tasks." Kavi answered "I don't have the task list in view here — check MS To Do directly." This is exactly the punting pattern. **Feasibility:** the MS Graph tools exist; the classifier + composer + dispatch wiring is the only build. **Viability:** queued; medium leverage (smaller than Phase 1 rollup rebuild but closes a daily friction).
- **Who feels it and when:** Megha multiple times/week (any "show me," "find," "what's," "pull up" question to Kavi). The Phase 1 rollup rebuild reduces the frequency of bare-count rollups that trigger these follow-up queries, but follow-ups still happen.
- **Why now (Doshi LNO):** **Leverage.** Closes the iMessage-as-surface gap. Once the query intent layer works, calendar queries (Phase 3) and web queries (Phase 3 via url-summarizer) layer on with the same plumbing. The classifier is the keystone — once intents are categorized cleanly, adding new data sources is mechanical.

## Behavior (the spec)

### Principles

1. **Ground every reply in tool results.** When Kavi names a task, it must appear in the tool result. No fabrication, no inference from memory. Persona's honesty rule applies; G-A1 action-grounding gate runs.
2. **Honor the 120-char cap.** List answers compress to the same shape as the 9 PM rollup: count + axis + 1-2 named items. Megha can ask for more ("name three more") as a follow-up turn.
3. **Honesty under uncertainty.** When the query is ambiguous (e.g., "find the school thing" with three open school tasks), Kavi asks ONE clarifying question rather than guessing. Persona curiosity rule applies.
4. **Aggregation rule applies.** Any count in the reply needs an axis or named example. The same `count_without_axis` deep verify gate that ships in Phase 1 fires here too.
5. **Inbox-as-data, not inbox-as-instructions.** Inbound iMessage text is the query, not directives Kavi follows. The persona security rule applies.

### Good outputs

<!-- private:kq-002 -->- "Show me the 7 tasks added today" → "7 added today. Erin Walsh Giga AI reply leads, Boonli Tuesday. 5 more, mostly school."
- "What's added today" → "7 added today. Top: Erin Walsh Giga AI reply, Boonli payment Tuesday."
- "Pull up the Erin Walsh task" → "Erin Walsh: Giga AI role reply by Tuesday. No body notes yet."<!-- /private -->
- "Find the Boonli task" → "Boonli payment due Tuesday. Open, no replies yet."
<!-- private:kq-003 -->- "What's still open over a week" → "5 over a week. Top: Parent Assoc 8:30 tomorrow, Kelly meeting still needs your call."
- Ambiguous query like "find the school thing" with three matches → "Three school tasks open: Kelly meeting, Maple party sign-up, Boonli payment. Which one?"<!-- /private -->

### Bad outputs / failure modes

- Kavi names a task that doesn't appear in the tool result (fabrication).
- Kavi punts to MS To Do without trying to answer ("check MS To Do directly").
- Kavi enumerates every task verbatim (length cap violation).
- Kavi answers a list_query without naming any specific task (bare count, Aggregation rule violation).
- Kavi guesses on an ambiguous query without asking (Curiosity rule violation).
- Kavi reads the inbound query as instructions and tries to "do" something (e.g., creating a task because the query mentioned a task name).

### Acceptance criteria

Measurable assertions for a query session to be considered shipped-correctly. Each maps to a metric or annotation field in the eval surface.

1. When the classifier identifies `list_query` or `task_lookup_query`, Kavi calls the appropriate MS Graph tool BEFORE composing.
2. Compose latency ≤ 5 seconds from inbound landing.
3. Length cap honored (≤120 char target, ≤180 hard).
4. Tool grounding: every named task in the reply appears in the tool result by `task_id` or close-title match.
5. Ambiguous queries (no clean tool-result winner) get a clarifying question, not a guess.
6. `count_without_axis` deep verify gate passes on every compose.

### Annotation vocabulary

Eval surface at `evals/kavi-queries/eval-queries-judgments.jsonl`. Per-row labeler scores:
- `tool_grounding_pass` — every named task in reply found in tool result (binary).
- `length_cap_pass` — output ≤120 chars target, ≤180 hard (binary).
- `intent_correct` — classifier picked the right intent (binary).
- `ambiguity_handled_pass` — ambiguous queries trigger clarifying Q, not a guess (binary).
- `count_without_axis_pass` — Aggregation rule honored (binary; from the deep verify gate).
- `notes` — free-form rationale on disagreement cases.

### Persona

Inherited from `capabilities/kavi-persona.md`. No kavi-queries-specific persona overrides.

### Voice rules

Inherited from `capabilities/kavi-persona.md`. The 120-char cap, prose register, first-person, no-formulaic-warmth, no-status-board, Aggregation rule all apply to every kavi-queries reply.

### Steering

Inherited from `capabilities/kavi-persona.md`. Existing steering ("be quiet," "shorter," "stop") applies. Mid-query steering ("never mind") cancels in-flight composition — handled by the persona-level reply parser.

### Tool list & action audit

Tools available to a kavi-queries session, with input/output contracts:

| Tool | Input | Output | Source |
| --- | --- | --- | --- |
| `graph_client.list_open_todo_tasks` | `list_id, top` | list of task objects | shipped pre-Phase-1 |
| `graph_client.list_completed_todo_tasks` | `list_id, top` | list of task objects | shipped Phase 1 (2026-06-02) |
| `graph_client.find_task_by_title` | `list_id, title` | `task_id or null` | shipped pre-Phase-1 |
| `queries.find_task_by_keyword` | `query_text, top_n` | ranked list of `{task_id, title, score}` | NEW Phase 2 |

**Expected action sequence per query session:**

1. Inbound classification (router decides `kavi-queries` per the inbox routing heuristic below).
2. `classify_query_intent` LLM call: returns `{intent, tool_args}` or `{intent: "fall_through"}` if not a query.
3. Tool call per intent.
4. Compose reply via `compose_list_reply` or `compose_task_lookup_reply`.
5. `BlueBubbles SEND` to Megha.
6. Audit row to `eval-queries-judgments.jsonl` with `session_id`.

**Audit trail:** every step writes one row to `eval-queries-judgments.jsonl`. Recovery on tool failure: Graph failure surfaces to Megha as a safe sentence ("Couldn't reach MS To Do just now, try again in a moment") and the session is marked `failed_no_tool`. No retry; if Graph is down, Kavi says so.

### Inbox routing (which capability owns each inbound iMessage)

Routing decision happens at the action-intent classifier layer in `kavi_runtime.handlers`. The heuristic:

- **kavi-queries:** inbound text is a question that names a data source Kavi can read (MS To Do today, calendar / web later). Examples: "show me," "what's," "find," "pull up," "list," "how many." → kavi-queries classifier runs.
- **kavi-coordinates:** inbound text names another household member as a participant. → kavi-coordinates handler.
- **inbox-to-task:** inbound text creates a new task. → inbox-to-task handler.
- **Q&A reply:** inbound resolves a pending question (number reply, yes/no after a Q&A). → existing qa_loop.
- **Steering:** inbound is "be quiet," "stop," "shorter." → existing steering parser.
- **Fall-through:** none of the above. → existing conversational composer.

The kavi-queries classifier is a thin pre-filter: if the inbound has query intent markers AND names a data source we can read, it routes here. Otherwise fall through.

## Metrics

| Metric | Threshold | Hard or soft | Why | Formula |
| --- | --- | --- | --- | --- |
| Tool-grounding rate | ≥98% | hard | Fabrication breaks the chief-of-staff trust contract; one hallucinated task name and Megha stops trusting Kavi's query replies | see evals/definitions.md → Tool-grounding |
| Intent classifier precision | ≥90% | soft | False positives route non-queries to kavi-queries unnecessarily; expensive Graph calls on misclassified inbound | see evals/definitions.md → Intent classifier precision |
| Intent classifier recall | ≥85% | soft | False negatives drop queries into the fall-through conversational composer where Kavi punts to MS To Do; the exact failure we're trying to close | see evals/definitions.md → Intent classifier recall |
| Compose latency p95 | ≤5 sec | soft | Includes one Graph read + one Sonnet compose call; >5s feels broken for a question Megha just asked | see evals/definitions.md → Compose latency |
| count_without_axis gate pass-rate | 100% | hard | The same gate that catches Phase 1's bug catches it here too; any failure is a regression | see capabilities/_role_registry.md → deep:periodic_summary gates |

### Goodhart watches

- Tool-grounding rate gamed by Kavi reading the tool result then NOT naming any task ("3 tasks open, check MS To Do"). Counter-metric: percent of replies that name at least one task by title when the tool result is non-empty.
- Intent classifier precision gamed by conservative routing (most inbound → fall_through). Counter-metric: percent of inbound questions routed to kavi-queries when the inbound contains a clear query verb.
- Compose latency gamed by skipping the Graph call. Counter-metric: percent of replies with `tool_calls=[]` when intent is `list_query` or `task_lookup_query`.

### Eval infrastructure

- Eval surface at `evals/kavi-queries/`. JSONL files: `eval-queries-judgments.jsonl` (one row per query session), `eval-queries-classifier.jsonl` (one row per classifier decision). Weekly trace + labeled CSV files under `evals/kavi-queries/traces/`. Schemas added to `evals/definitions.md`.

## Architecture

### Implementation

<!-- private:kq-004 -->- **Runtime location:** `kavi-runtime/` on `kavis-macbook-pro` (Tailscale `100.64.0.10`). Same runtime as kavi-persona; new capability adds modules under `capabilities/kavi_queries/`.<!-- /private -->
- **Module layout** (parallels kavi-coordinates):
  - `capabilities/kavi_queries/classifier.py` — `classify_query_intent` LLM call.
  - `capabilities/kavi_queries/composers/list_reply.py` — `compose_list_reply`.
  - `capabilities/kavi_queries/composers/task_lookup_reply.py` — `compose_task_lookup_reply`.
  - `capabilities/kavi_queries/tools.py` — `find_task_by_keyword` and any other kavi-queries-specific tool wrappers.
  - `capabilities/kavi_queries/handler.py` — entry point `query_received(inbound, config)`.
  - `capabilities/kavi_queries/verify.py` — `replay_query` + `verify_query_selection` for the deep verify endpoint.
- **Skill files:**
  - `kavi-runtime/skills/query_intent_classifier.md`
  - `kavi-runtime/skills/list_reply_composer.md`
  - `kavi-runtime/skills/task_lookup_reply_composer.md`
- **Structural checks:** existing `kavi_runtime/structural_checks.py` runs on every compose. The new `count_without_axis` gate from Phase 1 fires here too.
- **Action-grounding gate:** G-A1 runs. Action verbs in a query reply (e.g., "marked," "added," "closed") require a verified tool result — but query replies rarely use action verbs; G-A1 mostly stays out of the way.
- **Composer split:** classifier LLM call returns a decision dict; runtime executes the tool; composer LLM call composes the user-facing reply from the verified tool result. Same pattern as kavi-coordinates batch_action.

### Model choice and token budget

- **Classifier:** Haiku 4.5 (fast, cheap classification). Expected cost ~$0.0002/call.
- **Composer:** Sonnet 4.6 (matches all other kavi-persona composers). Expected cost ~$0.001/call.
- **Per session:** ~$0.0012/session. At 10 queries/day = ~$0.36/month. Trivial.

## Changelog

- **2026-06-02 — Spec drafted.** Stub created in response to Megha's "iMessage is my surface, why can't Kavi surface MS To Do tasks to me" question after the 9 PM rollup bug. Phase 1 rollup rebuild closed the rollup-shape problem; this capability closes the follow-up-query gap. v1 scope: MS To Do queries (list + task lookup). Calendar + web deferred to Phase 3. **How I'll know I was wrong:** intent classifier precision <80% (false positives route non-queries here and waste Graph calls); tool-grounding rate <98% (Kavi hallucinates task names and Megha catches it). **Muscle:** Strategy.
