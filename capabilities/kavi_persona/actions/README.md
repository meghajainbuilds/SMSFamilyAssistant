# kavi-persona actions

What Kavi does after an inbound iMessage. The flow:

1. **Classify intent** — `action_intent_classifier` skill. Possible
   intents: mark_done, snooze, create_task, ambiguous, pause, resume,
   correction.
2. **Match target** — `action_target_matcher` skill matches the intent to
   one open MS To Do task (or returns ambiguous).
3. **Execute** — per-verb handler hits MS Graph + writes per-concept state.
4. **Reply** — `post_action_reply_composer` if action landed, or
   `action_clarifying_reply_composer` if the classifier needed more info.

Source of truth: `kavi_runtime/handlers.py` (the bulk of this lives there).

## Test coverage (in `kavi-runtime/tests/`)

- `test_action_layer_llm_first_2026_05_08.py`
- `test_mark_done_verb.py`
- `test_action_clarifying_composer_fix2.py`
- `test_create_task_verb.py`
- `test_pending_clarification.py`
- `test_structural_g_a1.py` (action-claim correspondence gate)
