# Phase 3 concept inventory — state-file-per-concept refactor

_Filed 2026-06-02 as step 4.1 of the Phase 3 architectural refactor (EM-critique Phase 3 of 3)._

## Background

Today `imessage-state.json` on Kavi holds 15+ keys spanning 7 conceptually-distinct domains. Every Investigator rebuilds "which file holds what" from scratch, and the 2026-05-29 parent-assoc investigation paid the full tax of that ambiguity. The role-registry's `live_state_source` column is a partial band-aid; the structural fix is one file per concept.

## Live state shape (read from Kavi 2026-06-02)

`ssh kavi@100.64.0.10` → `cat /Users/kavi/HomeOS/state/imessage-state.json` → keys:

```
questions, summary_queue, last_send_at, last_summary_send_at,
auto_runs_paused, paused_since, paused_reason, paused_email_queue,
pending_alerts, spend_cap_bypass_month, pending_action_clarifications,
last_summary_anchors, alert_dedupe, last_failure_rate_alert_at,
pending_self_check
```

Plus three other state-dir files already separate:
- `backfill_state_2026-05-14_to_2026-05-26.json` (one-off backfill artifact; out of scope)
- `last_startup.json` (already its own file — startup probe marker)
- `periodic_summary_last_hash.json` (already its own file — debounce hash)

## Concept grouping rule

Fields that are written together inside a single atomic write are grouped into the same concept. The grouping is functional, not lexical — `last_send_at` lives with `questions` because every `add_pending_question` writes both.

## Inventory

| concept | current keys in imessage-state.json | new file | read sites | write sites |
|---|---|---|---|---|
| **questions** | `questions`, `last_send_at` | `/Users/kavi/HomeOS/state/questions.json` | `state.list_pending_questions`, `state.pop_pending_question`, `handlers.py` periodic_summary read, periodic_summary anchor sweep | `state.add_pending_question`, `state.pop_pending_question`, periodic_summary anchor sweep |
| **summary_queue** | `summary_queue`, `last_summary_send_at`, `last_summary_anchors` | `/Users/kavi/HomeOS/state/summary_queue.json` | `state.drain_summary_queue`, `state.load_summary_anchor` | `state.enqueue_summary_item`, `state.drain_summary_queue`, `state.save_summary_anchor`, `state.clear_summary_anchor` |
| **pause_state** | `auto_runs_paused`, `paused_since`, `paused_reason`, `paused_email_queue`, `paused_spend_usd`, `spend_cap_bypass_month` | `/Users/kavi/HomeOS/state/pause_state.json` | `guardrails.get_pause_state`, `guardrails.is_paused`, `guardrails.is_spend_cap_bypassed`, `handlers.py paused-spend lookup` | `guardrails.set_paused`, `guardrails.clear_paused`, `guardrails.enqueue_paused_email` |
| **pending_alerts** | `pending_alerts` | `/Users/kavi/HomeOS/state/pending_alerts.json` | `guardrails.drain_pending_alerts`, `scheduler.drain_pending_alerts_morning` | `guardrails.enqueue_pending_alert`, `guardrails.drain_pending_alerts` |
| **action_clarifications** | `pending_action_clarifications` | `/Users/kavi/HomeOS/state/action_clarifications.json` | `state.load_pending_clarification` | `state.save_pending_clarification`, `state.clear_pending_clarification` |
| **alert_dedupe** | `alert_dedupe`, `last_failure_rate_alert_at` | `/Users/kavi/HomeOS/state/alert_dedupe.json` | `handler_alerts._should_fire_per_sender_alert`, `handler_alerts._should_fire_rate_alert` (read leg) | `handler_alerts._should_fire_per_sender_alert`, `handler_alerts._should_fire_rate_alert` (write leg) |
| **self_check** | `pending_self_check` | `/Users/kavi/HomeOS/state/self_check.json` | `weekly_self_check.py` (multiple) | `weekly_self_check.py` (multiple) |

## Cross-concept transactions

These are the writes that touch multiple keys today. The per-concept split must split each into multiple writes (one per concept), accepting that the two writes are no longer atomic relative to each other:

- `state.add_pending_question` → questions + summary_queue read (skipped; only `questions`+`last_send_at` written, both in **questions** concept). OK.
- `guardrails.set_paused` → all pause-state fields. **pause_state** concept. OK.
- `guardrails.clear_paused` → pause fields + `paused_email_queue` + `spend_cap_bypass_month`. **pause_state** concept. OK.
- `handler_alerts._should_fire_per_sender_alert` → only `alert_dedupe`. OK.

No cross-concept writes survive once the grouping above is in place. The grouping was chosen specifically to avoid them.

## Files already per-concept (no migration needed)

- `pending_facts.jsonl` — durable cross-day facts (durable_facts.py)
- `subscriptions/<account>.json` — MS Graph webhook subscription state
- `learned_facts.jsonl` — cross-day facts confirmed by Megha
- `corrections.jsonl`, `promoted_patterns.jsonl`, `rejected_patterns.jsonl` — eval/correction trail
- `last_startup.json` — startup probe marker
- `periodic_summary_last_hash.json` — periodic_summary input-hash debounce
- `runtime-events.jsonl` — operational event stream

These are referenced by the live-state column already; Phase 3 doesn't touch them.

## Migration plan

1. New module `kavi_runtime/state_per_concept.py` exposes `load_<concept>` / `save_<concept>` for the 7 concepts above. Atomicity via existing `state_io.atomic_write_json` (per-writer-unique tmp filename guaranteed by `tempfile.mkstemp`).
2. Migration script `scripts/migrate_state_to_per_concept.py` reads legacy `imessage-state.json`, writes each concept's slice to its new file, renames the legacy file to `imessage-state.json.pre-phase3-backup`. Idempotent + dry-run flag.
3. Boot-time migration in `kavi_runtime/main.py` runs the script in-process if (legacy exists) AND (any per-concept file is missing).
4. Every read/write site in `kavi_runtime/` is updated to call the new concept-specific helpers. Tests update to match.
5. Role registry's `live_state_source` column updates to point at the new per-concept files.

## Out of scope for Phase 3

- The legacy `state_invariants.py` migration from `.claude/imessage-state.json` to `HomeOS/state/imessage-state.json` stays as-is (the new layout supersedes it; we leave the old check in place as a belt-and-suspenders).
- `pending_facts.jsonl` and other already-separate files are not touched.
- The `backfill_state_2026-05-14_to_2026-05-26.json` artifact stays as a sibling; it is not a runtime state file.
