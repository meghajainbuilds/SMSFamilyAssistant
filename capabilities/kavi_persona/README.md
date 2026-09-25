# kavi-persona — every Megha-facing thing Kavi says or does

This directory is the canonical entry point for any bug, change, or new
behavior tied to Kavi-the-person: voice, identity, refusal, periodic
summaries, the intent-first iMessage reply path (parser + executors +
reply composer, 2026-06-10 rebuild), the Q&A loop's resolution, and the
follow-up replies after an action lands.

`capability_type: [meta, generative, two-way]`. Spec:
`capabilities/kavi-persona.md`.

## Where code lives (axis-by-axis)

| Axis | File in this dir | Source of truth |
|---|---|---|
| Behavior spec | `spec.md` (pointer) | `capabilities/kavi-persona.md` |
| Selection (which inputs reach each composer) | `selection.py` | `_filter_*_by_open_status`, `_read_pending_facts_for_summary`, `_pick_summary_anchor`, `_filter_tasks_by_owner` + `_owner_of_title` (per-person split, 2026-06-10), `_select_due_soon_tasks` + `DEADLINE_RUNWAY_DAYS` (deadline runway, 2026-06-10), `_select_morning_theme` + `THEME_MIN_CLUSTER` / `THEME_MAX_INPUT_TITLES` / `THEME_TITLE_TRUNCATE_CHARS` (morning themes, 2026-06-10), `_select_stale_task_nudge` + `STALE_TASK_MIN_AGE_DAYS` (stale-task nudge on no-theme mornings, 2026-06-11), `_compute_rollup_counts`, `_pick_top_importance_tasks` |
| Selection — evening suggest-to-close (2026-06-10) | `close_suggestions.py` | `select_close_suggestions` + `record_close_suggestions_surfaced`; cost caps `CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN` / `CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED`; per-concept state `close_suggestions.json`; judgments eval-logged to `evals/kavi-persona/eval-persona-close-suggestions.jsonl` |
| Composers (one per LLM call site) | `composers/` | `kavi_runtime/claude_client.compose_*` methods (incl. `composers/reply.py` — the ONE reply composer at the end of the intent-first dispatch) |
| Intent parsing (inbound replies, 2026-06-10) | `reply_intent_parser.py` | `parse_reply_intents` — the single LLM parse every semantic inbound goes through; skill `kavi-runtime/skills/reply_intent_parser.md` |
| Intent executors (deterministic, dry-runnable) | `intent_executors.py` | `execute_intents` — ALL parsed intents execute (close incl. answered-by-close, qa keep/drop by question kind, create/delete, pause/resume, correction, coordination delegate) |
| Deep verify — periodic summary | `verify.py` | `kavi_runtime/synthetic_compose.verify_periodic_summary_selection` |
| Deep verify — inbound reply (`deep:reply_intent`) | `verify_reply.py` | `replay_kavi_reply` / `verify_kavi_reply`; gates intent_dropped, wrong_direction_resolution, banned_template_reply, unresolved_context_claim, ungrounded_action_claim, length_cap, prose_required |
| Skills (BEHAVIOR-only) | `skills/` | `kavi-runtime/skills/*.md` |
| Action handlers (mark done / snooze / create_task) | `actions/` | `kavi_runtime/handlers._try_handle_action_intent`, `_handle_create_task_verb` |
| iMessage Q&A loop (questions + composer) | `qa_loop/` | question composer + `_append_household_example`; reply RESOLUTION moved to `intent_executors.py` (legacy `_handle_q_and_a_reply` / `_apply_qa_resolutions` / `_handle_qa_reply` deleted 2026-06-10) |
| Weekly self-check | `weekly_self_check.py` | `kavi_runtime/weekly_self_check.py` |
| Durable facts (pending_facts.jsonl) | `durable_facts.py` | `kavi_runtime/durable_facts.py` |
| Pause + correction handling | `actions/pause_correction.py` | `kavi_runtime/handlers._classify_pause_intent`, `_handle_correction` |

## Three-axis split (LIVE — kavi-persona is the pilot for the EM-critique refactor)

- **PERSONA** — voice, identity, refusal rules — live ONLY in
  `capabilities/kavi-persona.md`, loaded at compose time via
  `kavi_runtime/persona_loader.py`. Skills do not duplicate persona rules.
- **STRUCTURAL** — length caps, prose-vs-lists, JSON shape — live ONLY in
  `kavi_runtime/structural_checks.py`. The runtime enforces these after
  compose; user prompts surface them by referencing the constants
  (`LENGTH_CAP_TARGET` etc.), never literal values.
- **BEHAVIOR** — input-shape rules, what-to-surface rules, when-to-skip
  rules — live in the per-composer skills under `skills/`. The
  architectural test at `kavi-runtime/tests/test_composer_skill_isolation.py`
  enforces that skills do not re-encode voice or structural rules.

## Deep verify

Two surfaces. `POST /synthetic/verify/kavi-reply` replays an inbound
exchange through the REAL intent parser + DRY-RUN executors + REAL reply
composer and gates it (see `verify_reply.py`; contract at
`evals/kavi-reply/matrix/ENDPOINT_CONTRACT.md`; raw mode at
`POST /synthetic/compose/kavi-reply`).

`POST /synthetic/verify/kavi-persona` runs the periodic_summary composer
plus the selection-behavior gates:

- `done_task_surfaced` — output names a title whose task_id is in
  `closed_task_ids`.
- `past_event_surfaced` — output names a title listed in
  `past_event_titles`.
- `duplicate_phrase` — same 4-word phrase appears twice in output.
- `count_without_axis` — count claim with no axis qualifier or named
  example nearby.
- `owner_leak` (2026-06-10) — output composed for `recipient` names a
  task whose title-prefix owner is the other person.
- `due_soon_dropped` (2026-06-10) — payload carries a `due_soon` item the
  composed morning digest never references.
- `close_claim` (2026-06-10) — payload carries `close_suggestions` but
  the output uses completion-claim phrasing ("closed it", "marked done")
  instead of suggestion language.
- `theme_unsupported` (2026-06-10) — payload theme present but its label
  keywords never appear in the morning digest, OR no payload theme but
  the output uses a theme-introducing shape.
- `stale_task_dropped` (2026-06-11) — payload carries `stale_tasks` but
  the composed morning digest never references one of the items; no-op
  for rollups.

The payload accepts `recipient` ("megha" / "max", default "megha"),
`due_soon` (deadline-runway items), `close_suggestions`
([{title, reason}]), `theme` ({label, task_count} or the clusterer's
raw {label, supporting_task_titles}), and `stale_tasks`
([{title, age_days}], 2026-06-11). Compose-only mode (no gates) is
`POST /synthetic/compose/kavi-persona`.

## Tests

Cross-cutting tests live under `kavi-runtime/tests/` — too many to list;
see the `composers/`, `actions/`, and `qa_loop/` sub-READMEs for the test
files most relevant to each axis.
