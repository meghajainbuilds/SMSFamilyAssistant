# inbox-to-task — email-to-task creation capability

This directory is the canonical entry point for any bug, change, or new
behavior tied to reading Megha's and Max's Outlook inboxes, classifying
emails, and writing tasks to the McMullen-Jain Shared MS To Do list.

`capability_type: [judgment, agentic]`.

## Where code lives

| Axis | File | Source of truth |
|---|---|---|
| Behavior spec | `spec.md` (pointer) | `capabilities/inbox-to-task.md` |
| Selection (which emails reach the classifier) | `selection.py` | `kavi_runtime/inbox_pre_filter.py`, `kavi_runtime/handlers._email_arrived_impl` (the "should we run the LLM?" gate) |
| Compose (classifier + composer) | `compose.py` | `kavi_runtime/claude_client.run_email_to_tasks` |
| Verify (deep verify procedure) | `verify.py` | `kavi_runtime/synthetic_compose.replay_email_classify` |
| Skill (BEHAVIOR-only composer spec) | `skill.md` | `kavi-runtime/skills/email_to_tasks.md` |
| Task writer (MS To Do API + dedup) | `task_writer.py` | `kavi_runtime/handlers._apply_lifecycle_update`, `kavi_runtime/graph_client` create/update task |

## Three-axis split

- **PERSONA** — n/a (judgment capability with no Megha-facing voice; the LLM
  produces a structured decision dict, not user prose).
- **STRUCTURAL** — JSON-shape gates in `kavi_runtime/claude_client.run_email_to_tasks`
  parsing. Length/format constraints live in the skill via reference.
- **BEHAVIOR** — `skill.md` (BEHAVIOR-only rules: when to skip, when to create,
  when to set confidence=low).

## Deep verify

- **Compose endpoint:** `POST /synthetic/compose/inbox-to-task` (raw replay).
- **Verify endpoint:** TBD — currently the only deep verify available is the
  raw compose; selection-behavior gates for inbox-to-task are
  Phase 6 work (see `verify.py` for the dispatch and gate stubs).

## Tests

Cross-cutting tests live in `kavi-runtime/tests/`:
- `test_email_to_tasks_cache_ttl.py`
- `test_lifecycle.py` (most of the lifecycle/dedup behavior)
- `test_inbox_pre_filter.py`
- `test_synthetic_compose.py` (covers the deep verify endpoint)
- `test_matched_task_eval_fields.py`
- `test_pattern_audit.py`
- `test_open_tasks_fetch_2026_05_08.py`
- `test_backfill_email_range.py`
