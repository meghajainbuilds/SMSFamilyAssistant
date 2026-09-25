# kavi-persona composers

Every LLM call site Megha sees output from. Each composer has:

- A method on `kavi_runtime/claude_client.ClaudeClient` (compose layer).
- A BEHAVIOR-only skill file under `../skills/` (skill layer).
- One or more cross-cutting tests under `kavi-runtime/tests/`.

## Composer inventory

| Composer | Method | Skill | Key tests |
|---|---|---|---|
| Periodic summary | `compose_periodic_summary` | `skills/periodic_summary_composer.md` | `test_periodic_summary_debounce.py`, `test_periodic_summary_open_status_filter.py`, `test_summary_anchor_then_sweep.py`, `test_synthetic_verify_periodic_summary.py` |
| Q&A question composer | `compose_qa_question` | `skills/qa_question_composer.md` | (covered by `test_pending_clarification.py`, `test_handlers_imports.py`) |
| Post-action reply | `compose_post_action_reply` | `skills/post_action_reply_composer.md` | `test_action_layer_llm_first_2026_05_08.py`, `test_mark_done_verb.py` |
| Action-clarifying reply | `compose_action_clarifying_reply` | `skills/action_clarifying_reply_composer.md` | `test_action_clarifying_composer_fix2.py` |
| Weekly self-check | `compose_weekly_self_check` | `skills/weekly_self_check_composer.md` | (driven from `kavi_runtime/weekly_self_check.py`) |
| Action intent classifier | `compose_action_intent_classifier` | `skills/action_intent_classifier.md` | `test_action_layer_llm_first_2026_05_08.py` |
| Action target matcher | `compose_action_target_matcher` | `skills/action_target_matcher.md` | (covered alongside action intent) |
| Pending clarification resolver | `compose_pending_clarification_resolver` | `skills/pending_clarification_resolver.md` | `test_pending_clarification.py` |
| Pause intent classifier | `compose_pause_intent_classifier` | `skills/pause_intent_classifier.md` | (covered by handler tests) |
| Q&A reply classifier | `compose_qa_reply_classifier` | `skills/qa_reply_classifier.md` | (covered by inbound-reply tests) |
| Kavi conversation | `compose_kavi_conversation` | `skills/kavi_conversation.md` | (covered by inbound-handler tests) |
| Correction classifier | `compose_correction_classifier` | `skills/correction_classifier.md` | `test_pattern_audit.py` |
| Close-suggestion judge (2026-06-10) | `judge_close_suggestion` | `skills/close_suggestion_judge.md` | `test_close_suggestions_selection.py`, `test_verify_close_claim_theme_gates.py` |
| Morning theme clusterer (2026-06-10) | `cluster_morning_theme` | `skills/morning_theme_clusterer.md` | `test_morning_theme_selection.py`, `test_verify_close_claim_theme_gates.py` |
| Weekly self-check classifier | `compose_weekly_self_check_classifier` | `skills/weekly_self_check_classifier.md` | (driven from `weekly_self_check.py`) |
