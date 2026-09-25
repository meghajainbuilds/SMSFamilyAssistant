# Role registry — capability ↔ Investigator + Verifier procedures

One row per capability. The Investigator + Verifier sub-agents read this file at run time to know how to investigate symptoms and how to verify fixes for each capability.

**Required for any capability before it ships** (Phase 0c gate, enforced in `CLAUDE.md`):

- If `capability_type` includes any of `generative`, `judgment`, `agentic`, `two-way`, `retrieval` → `verifier_procedure` MUST be `deep:<procedure>` AND the synthetic compose entry point must ship in `kavi-runtime/kavi_runtime/synthetic_compose.py` + a corresponding endpoint in `kavi-runtime/kavi_runtime/server.py`.
- If `capability_type` is only `runtime` or `meta` → `shallow:<procedure>` is allowed; the `rationale` column must explain why deep doesn't apply.

Shipping a capability without a registry row is a ship-blocker, not a backlog item.

## Registry

| capability | investigator_log_path | live_state_source | verifier_procedure | rationale |
|---|---|---|---|---|
| `inbox-to-task` | `evals/inbox-to-task/traces/eval-inbox-week*.jsonl` | Eval log: curl runtime `/evals/inbox-to-task/recent`. MS To Do task lifecycle (open / closed / dedup state): NOT directly accessible to Investigator sub-agent — main engineer must confirm via `mcp__ms365__list-todo-tasks`. | `deep:email_classify` (raw compose at `POST /synthetic/compose/inbox-to-task`; selection-gate verify at `POST /synthetic/verify/inbox-to-task` added 2026-06-02 Phase 6) | LLM-shaped (`capability_type: [judgment, agentic]`). Synthetic email replay through classifier + composer. Phase 6 selection gates: `expected_decision_mismatch`, `expected_owner_mismatch`, `expected_confidence_mismatch`. |
| `kavi-persona` | `evals/kavi-persona/traces/eval-persona-week*.jsonl` | Per-concept state files on Kavi (one concept = one file = one canonical home, post-Phase-3 2026-06-02): pending Q&A → `/Users/kavi/HomeOS/state/questions.json`; summary queue + anchors → `/Users/kavi/HomeOS/state/summary_queue.json`; pause flags → `/Users/kavi/HomeOS/state/pause_state.json`; quiet-hours alert queue → `/Users/kavi/HomeOS/state/pending_alerts.json`; action-clarification pending → `/Users/kavi/HomeOS/state/action_clarifications.json`; alert dedupe → `/Users/kavi/HomeOS/state/alert_dedupe.json`; weekly self-check → `/Users/kavi/HomeOS/state/self_check.json`; durable cross-day facts → `pending_facts.jsonl`. Pre-Phase-3 monolithic legacy: `/Users/kavi/HomeOS/state/imessage-state.json.pre-phase3-backup` (frozen post-migration; do not read for current state). Currently-queued MS To Do tasks (the periodic_summary anchor source): NOT directly accessible to Investigator sub-agent — main engineer must confirm via `mcp__ms365__list-todo-tasks`. | `deep:periodic_summary + deep:reply_intent` | LLM-shaped (`capability_type: [meta, generative, two-way]`). Two user-visible LLM surfaces: the periodic summary composer (synthetic state snapshot replay at `POST /synthetic/compose/kavi-persona`) and the inbound-reply path (intent parser + reply composer replay at `POST /synthetic/compose/kavi-reply`, gated verify at `POST /synthetic/verify/kavi-reply`, added 2026-06-10 intent-first dispatch rebuild). |
| `realtime-kavi` | `/Users/kavi/Library/Logs/kavi-runtime.err.log` (via SSH) plus `GET /status` | Same as investigator log path. Runtime status, subscription health, recent dependency calls all live on the `/status` HTML response. Directly accessible to Investigator. | `shallow:status_endpoint` | `capability_type: [runtime]`. No LLM composer to replay. The right deep verify for runtime infra is synthetic webhook integration testing (different machinery, build when a runtime bug surfaces). Shallow verify asserts `/status` returns `spec_loaders_ok: true` plus subscription health. Spend-counter gate (added 2026-06-10): after any successful Claude API call, `/status` Today's spend must be > $0.00 and must increase across a synthetic compose call — a $0.00 reading on a day with live Claude calls is a FAIL, not a quiet default. |
| `kavi-coordinates` | `evals/kavi-coordinates/eval-coordinates-judgments.jsonl` (one row per session state transition, joined by `session_id`) | Active coordination sessions → `/Users/kavi/HomeOS/state/coordination_sessions.json` (per-concept file, write-through persisted on every mutation since 2026-06-10; closed/stale sessions pruned at load). Pending intent/commitment facts → `pending_facts.jsonl` (rows whose `inbound_source` starts with `imessage coord-`). MS To Do task created on branch 4b: NOT directly accessible to Investigator sub-agent — main engineer must confirm via `mcp__ms365__list-todo-tasks`. | `deep:coordination_addressee` (raw compose at `POST /synthetic/compose/kavi-coordinates`; selection-gate verify at `POST /synthetic/verify/kavi-coordinates`, added 2026-06-10 Phase 0c closure) | LLM-shaped (`capability_type: [agentic, two-way, generative]`). Synthetic session payload replay through the addressee-message composer (the central user-visible output of a coordination session). Selection gates: `vague_addressee_message` (output must carry at least one content keyword from the injected coordination ask — rejects the "coordinating on something" class), `empty_content_outbound` (an empty-content session must not produce an outbound). Shape gates: `length_cap` (`structural_checks.COORDINATION_ADDRESSEE_LENGTH_CAP`), `prose_required`. |

<!-- private:reg-001 -->**Live state check rule (added 2026-05-29 after the parent-assoc canonical run).** Before forming a present-tense hypothesis, the Investigator must consult the `live_state_source` column and classify the bug input as CONFIRMED PRESENT, CONFIRMED ABSENT, or UNVERIFIED (with the specific source named). The Verifier carries this classification forward into its verdict so the engineer cannot accidentally overclaim a synthetic PASS as a production fix. See `.claude/agents/investigator.md` step 6 and `.claude/agents/verifier.md` verdict shape.<!-- /private -->

## Procedure definitions

### `deep:periodic_summary`

Two endpoints, chosen by the bug shape:

**Output-shape mode — `POST /synthetic/compose/kavi-persona`** (Tailnet direct, port 8080). Returns the raw composed message; the Verifier matches it against a string / regex / optional LLM-as-judge PASS criterion. Use this when the bug is about voice, length, format, or persona drift.

Body shape:
```json
{
  "queued_tasks": [],
  "pending_questions": [],
  "pending_facts": [],
  "is_rollup": false,
  "time_of_day": "morning"
}
```

Returns:
```json
{ "output": "<composed message>", "model": "...", "input_payload": {...} }
```

**Selection-behavior mode — `POST /synthetic/verify/kavi-persona`** (added 2026-05-31). Same body as compose mode plus two verifier-only fields. Returns a structured verdict with per-gate failures. Use this when the bug is about WHAT the composer surfaced (e.g., done tasks, past events, duplicates) rather than HOW it phrased it.

Body shape (compose body + verifier metadata):
```json
{
  "queued_tasks": [],
  "pending_questions": [],
  "pending_facts": [],
  "is_rollup": true,
  "time_of_day": "9pm",
  "closed_task_ids": ["task-id-megha-marked-done-1", "..."],
  "past_event_titles": ["Walk for Kids this Saturday", "..."],
  "tasks_added_today_count": 0,
  "tasks_completed_today_count": 0,
  "tasks_over_7d_count": 0,
  "top_importance_tasks": [{"title": "...", "reason": "..."}],
  "recipient": "megha",
  "due_soon": [{"title": "MJ Pay Boonli invoice", "due_date": "2026-06-11", "days_until": 1}],
  "close_suggestions": [{"title": "MJ Pay Boonli invoice", "reason": "your reply says it was paid"}],
  "theme": {"label": "Summer camp planning", "supporting_task_titles": ["MJ Register for Cascade summer camp", "MJ Pay camp deposit", "MJ Order swim gear for camp"]},
  "stale_tasks": [{"title": "MJ Submit SCT reimbursement ticket", "age_days": 45}]
}
```

`recipient` (added 2026-06-10, per-person split) defaults to `"megha"`; pass `"max"` to replay Max's digest. `due_soon` (added 2026-06-10, deadline runway) carries the items `selection._select_due_soon_tasks` would have produced; `days_until` may be negative (overdue, still open). `close_suggestions` (added 2026-06-10, evening suggest-to-close) carries the items `close_suggestions.select_close_suggestions` would have produced for the 9 PM rollup. `theme` (added 2026-06-10, morning themes) accepts either the composer's `{label, task_count}` shape or the clusterer's raw `{label, supporting_task_titles}` shape (task_count derives from the supporting list); omit or null for the no-theme morning. `stale_tasks` (added 2026-06-11, stale-task nudge) carries the items `selection._select_stale_task_nudge` would have produced on a no-theme morning (each `{title, age_days}`, oldest first); the live selector never produces it alongside a theme, but the replay surface accepts any combination for Investigator convenience.

Returns:
```json
{
  "output": "<composed message>",
  "verdict": "PASS" | "FAIL",
  "failures": [{"gate": "done_task_surfaced|past_event_surfaced|duplicate_phrase|count_without_axis|owner_leak|due_soon_dropped|close_claim|theme_unsupported|stale_task_dropped", "detail": "..."}],
  "model": "...",
  "input_payload": {...}
}
```

Gates (all run; one or more failing → verdict FAIL):

- **done_task_surfaced** — output names a title whose task_id is in `closed_task_ids`. Catches the 2026-05-30 9 PM rollup bug where a pending Q&A whose underlying MS To Do task Megha had marked done leaked into the composed text. Runtime-side filter (`_filter_*_by_open_status` in `handlers.py`) is the upstream defense; this gate is defense in depth.
- **past_event_surfaced** — output names any title listed in `past_event_titles`. Catches same-day-but-past-time anchors (e.g., 9 PM Saturday rollup re-anchoring on a 9am Saturday event). The skill's stale-date rule is the upstream defense.
- **duplicate_phrase** — same 4-word phrase appears twice in output. Catches in-message duplicates.
- **count_without_axis** — output contains a count claim (`\d+ tasks?`, `\d+ emails?`, `\d+ items?`) without an axis qualifier ("today," "done," "over a week") or a named example nearby. Catches the 2026-06-02 9 PM rollup bug where "7 new tasks in MS To Do" shipped without saying which tasks, when, or why they mattered. Pointer phrases ("in MS To Do," "tap the list") count as deferral, not as an axis. Required-content gate type (the inverse of the three above, which are bad-content gates) — added per the Phase 1 deep-verify-asymmetry finding.
- **owner_leak** (added 2026-06-10, per-person split) — the output composed for `recipient` R names a task whose title-prefix owner is the other person (prefix convention: `MJ ` = Megha, `MM ` = Max, unprefixed = Megha; canonical parse in `capabilities/kavi_persona/selection.py:_owner_of_title`). Scans every titled input surface (queued_tasks, pending_questions, top_importance_tasks, due_soon) with the same two-keyword title-match heuristic as the done-task gate. The per-person selection filter (`_filter_tasks_by_owner`) is the upstream defense; this gate is defense in depth.
- **due_soon_dropped** (added 2026-06-10, deadline runway) — the payload carries a `due_soon` item but the composed MORNING digest references none of that item's title keywords. Required-content gate (like count_without_axis): deadline-runway items must surface every morning until the task closes, so a digest that drops one is a selection failure. No-op when `is_rollup` is true (the 9 PM rollup keeps its counts + named-top shape).
- **close_claim** (added 2026-06-10, evening suggest-to-close) — the payload carries `close_suggestions` but the composed output uses completion-claim phrasing ("closed it", "marked done", "completed it", first-person "I closed/marked/completed"). The contract is suggestion language only — the task is still open and Kavi closed nothing (the selection-side cousin of the G-A1 action-claim gate). Phrase-pattern heuristic; canonical patterns in `capabilities/kavi_persona/verify.py:_CLOSE_COMPLETION_CLAIM_RES` — extend there when a new claim phrasing ships. Armed only when the payload carries close suggestions; without them, completion wording is the outbound G-A1 gate's job.
- **theme_unsupported** (added 2026-06-10, morning themes) — two directions, morning shape only: (1) payload theme present → the composed digest must reference at least one keyword from the theme label (required-content direction; the skill says a present theme opens the digest); (2) payload theme absent → the output must not use a theme-introducing shape ("big open thread", "top of mind", "the big theme"). Direction 2 is a phrase heuristic with documented limits (a paraphrased invented theme escapes it); the per-day clustering cache + conservative clusterer skill are the upstream defenses. No-op when `is_rollup` is true.
- **stale_task_dropped** (added 2026-06-11, stale-task nudge) — the payload carries `stale_tasks` but the composed MORNING digest references none of an item's title keywords (at least one keyword per item must appear). Required-content gate, mirror of due_soon_dropped: the nudge is the morning's top-of-mind frame when present (selection computes it only on no-theme mornings), so a digest that drops an item is a selection failure. No-op when `is_rollup` is true or the composer returned None. The owner_leak gate also scans `stale_tasks` titles.

Title-matching heuristic: two distinct keywords (length ≥4, owner prefix and action verbs stripped) from the title appearing in the output count as a match. Tuned conservatively to avoid over-firing on single common words.

**Required-content gate body shape (added 2026-06-02 Phase 1):** when the bug is "what the composer left out" rather than "what the composer surfaced wrongly," the synthetic state snapshot may also carry the new Phase 1 fields used by the rollup composer: `tasks_added_today_count`, `tasks_completed_today_count`, `tasks_over_7d_count`, `top_importance_tasks`. The verify endpoint passes them through to the composer so the replay exercises the same code path as the live 9 PM rollup.

### `deep:reply_intent`

Two endpoints, chosen by the bug shape (mirrors `deep:periodic_summary`):

**Raw replay mode — `POST /synthetic/compose/kavi-reply`** (staging port 8081 by
default; production port 8080 only for a deliberate Verifier run). Body carries a
synthetic inbound exchange: `inbound_text`, `sender` ("megha"|"max"), `recent_outbound`
[{kind, text}] (newest last), `recent_inbound` [string] (the sender's own prior
messages), `pending_questions`, `pending_facts`, `open_tasks` [{id, title}], optional
`open_coordination_sessions`. Runs the REAL intent parser with that context, the
deterministic executors in DRY-RUN (resolution against the payload's `open_tasks` only,
no Graph calls, no sends, no state writes), then the REAL reply composer over the
simulated execution results. Returns `{output, parsed_intents, executed, model,
input_payload}`. Use when the bug is about what the parser extracted or how the reply
was phrased.

**Gated verify mode — `POST /synthetic/verify/kavi-reply`.** Same body plus two
verifier-only fields: `expected_intents` and `forbidden_intents`, each
[{type, target_keyword}] matched against the parsed intents (type equality plus
case-insensitive keyword-in-target-title-or-target-text; null keyword = any of that
type). Returns `{output, verdict, failures[], parsed_intents, executed, model,
input_payload}`.

Gates (all run; any failing → verdict FAIL):

- **intent_dropped** — an `expected_intents` entry absent from the parsed intents.
<!-- private:reg-002 -->  Catches the swallowed-intent class (2026-06-10: the maple street close that never
  happened).
- **wrong_direction_resolution** — a `forbidden_intents` entry present in the parsed
  intents. Catches the wrong-binding and vocabulary-coercion classes (2026-06-10: "Yes"
  bound to the Anita question instead of the Cedar House offer; "close" coerced to keep).<!-- /private -->
- **banned_template_reply** — reply matches `^Got it\.$|^Got it!$|^Kept: |^Dropped: `.
  The always-LLM rule's mechanical enforcement on this surface.
- **unresolved_context_claim** — reply claims to lack context (canonical phrase list in
  the verify module) while the payload's `recent_inbound` is non-empty. Catches the
  "I don't have context on others from this thread" class.
- **ungrounded_action_claim** — affirmative past-tense/progressive action claim
  (G-A1 canonical verb list in `structural_checks.py`) with no matching dry-run
  execution result; negation-aware.
- **length_cap** — reply exceeds `structural_checks.LENGTH_CAP_TARGET`.
- **prose_required** — reply contains list markers.

Canonical home of the gate logic: `capabilities/kavi_persona/verify_reply.py` (the
runtime endpoint imports from it via `kavi_runtime/synthetic_compose.py`, per the
deep-verify-parity shape).

Frozen seed matrix: `evals/kavi-reply/matrix/matrix-kavi-reply.jsonl` (18 cases,
authored 2026-06-10 from the keep/drop-coercion incident transcript plus the
kavi-persona.md Behavior examples). Per BUILD_PIPELINE Rule 5, a Verifier "fixed" claim
on any inbound-reply bug requires the incident repro PASS AND this full matrix green.

### `deep:email_classify`

<!-- private:reg-003 -->Verifier curls `POST http://100.64.0.10:8080/synthetic/compose/inbox-to-task` (Tailnet direct, port 8080). Body is the Investigator's email payload as JSON. Captures the returned decision dict. Compares against the Investigator's PASS criterion (decision value match, task content match, or string presence in reasoning).<!-- /private -->

Body shape:
```json
{
  "email": {
    "subject": "...",
    "from": "...",
    "body": "...",
    "received_at": "..."
  }
}
```

Returns:
```json
{
  "decision": "create | skip",
  "confidence": "high | medium | low",
  "task": { "title": "...", "owner": "...", "body": "..." },
  "reasoning": "<LLM rationale>",
  "tokens": {...}
}
```

### `deep:coordination_addressee`

Two endpoints, chosen by the bug shape (mirrors `deep:periodic_summary`):

**Output-shape mode — `POST /synthetic/compose/kavi-coordinates`** (Tailnet direct, port 8080). Returns the raw composed addressee message; the Verifier matches it against a string / regex PASS criterion. Use when the bug is about voice, length, or attribution phrasing.

Body shape:
```json
{
  "inbound_text": "verbatim requester free-text",
  "requester_name": "Megha",
  "addressee_name": "Max",
  "coordination_ask": "Can Max meet the teachers tomorrow at 3:00 or 3:35?",
  "attribution_judgment": {"should_attribute": true, "reason": "..."}
}
```

Returns:
```json
{ "output": "<composed addressee message>", "model": "...", "input_payload": {...} }
```

**Selection-behavior mode — `POST /synthetic/verify/kavi-coordinates`** (added 2026-06-10). Same body. Returns a structured verdict with per-gate failures. Use when the bug is about WHAT the composer surfaced.

Returns:
```json
{
  "output": "<composed addressee message>",
  "verdict": "PASS" | "FAIL",
  "failures": [{"gate": "vague_addressee_message|empty_content_outbound|length_cap|prose_required", "detail": "..."}],
  "model": "...",
  "input_payload": {...}
}
```

Gates (all run; one or more failing → verdict FAIL):

- **vague_addressee_message** — the composed message carries NO content keyword from the injected session payload's coordination ask (keywords are content-bearing words; household names, generic time words, and filler are excluded). Catches the June 3 class where the recipient got a content-free ping ("coordinating with Max on something") that nobody could act on.
- **empty_content_outbound** — the injected session has a blank `coordination_ask` but the composer produced an outbound. No content → no send; an empty-content session must refuse.
- **length_cap** — output exceeds `structural_checks.COORDINATION_ADDRESSEE_LENGTH_CAP`.
- **prose_required** — output contains list markers.

Canonical home of the gate logic: `capabilities/coordination/verify.py` (the runtime endpoint imports from it via `kavi_runtime/synthetic_compose.py`, per the deep-verify-parity shape).

### Build pipeline (frozen matrix + bounded iteration + staging replay)

How a fix or new capability EARNS the right to call any of the deep
procedures above against production: see `capabilities/BUILD_PIPELINE.md`.
Short form — the per-capability test matrix
(`evals/<slug>/matrix/matrix-<slug>.jsonl`) is frozen via
`kavi-runtime/scripts/matrix_freeze.py` BEFORE fix iteration begins;
`kavi-runtime/scripts/run_matrix.py` replays it against the STAGING
runtime (port 8081, outbound hard-disabled) with a 10-runs-since-freeze
iteration bound; matrix-green-on-staging precedes a production deploy;
the production Verifier sub-agent (procedures above) remains the only
authority for "fixed" / "works" claims. For a NEW capability, the seed
matrix is frozen at Phase 0c time alongside this file's registry row and
the synthetic compose/verify endpoints.

### `shallow:spec_grep`

<!-- private:reg-004 -->Verifier SSHes to Kavi (`kavi@100.64.0.10`) and greps the deployed capability spec file at `/Users/kavi/kavi-runtime/capabilities/<slug>.md` for the rule string named in the Investigator's smoking-gun evidence. PRESENT (grep count > 0) = PASS, ABSENT (count == 0) = FAIL.<!-- /private -->

Use only when the Verifier needs to confirm a spec change reached Kavi, not when behavior verification is needed. For LLM-shaped capabilities, deep is the correct procedure even when the underlying question is "did the deploy land" — deep also covers "is the LLM honoring the deployed rule," which shallow does not.

### `shallow:status_endpoint`

<!-- private:reg-005 -->Verifier curls `GET http://100.64.0.10:8080/status` (Tailscale). The endpoint returns HTML with one labeled field per line (`<label>: <value>` inside `<pre>`).<!-- /private -->

Asserts:
- `spec_loaders_ok: true` (canonical specs loaded into runtime; deploy gap or syntax error in `capabilities/kavi-persona.md` / `capabilities/security-baseline.md` flips this to false)
- `spec_loaders_checked_at: <timestamp within last 60 minutes>` (proves the loader probe ran recently, not stale)
- Any additional fields named in the Investigator's PASS criterion (e.g., `Last successful Claude API call: <recent timestamp>`, `Last successful MS Graph call: <recent>` to confirm dependencies are reachable)

All asserted fields green = PASS, any red or missing = FAIL.

Implementation note: parse via regex/grep on the HTML response. The endpoint is HTML-only today (no `Accept: application/json` shape) because Megha bookmarks `/status` on her phone for at-a-glance health. A JSON variant would be additive work; not needed until a real-time-kavi bug surfaces that requires it.

## When to update this file

- **New capability proposed:** add a row at proposal time with the verifier_procedure already chosen. If `capability_type` is LLM-shaped, building the synthetic compose entry point is part of the capability's Phase 0c shipping work.
- **New procedure type:** add a definition above. Keep procedure names stable (`deep:<name>` / `shallow:<name>`) so the Verifier sub-agent code can dispatch on them.
- **Capability deprecated:** mark the row deprecated (don't delete; preserves history for later sessions).
