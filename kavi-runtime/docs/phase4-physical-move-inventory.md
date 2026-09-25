# Phase 4 — physical move inventory

For each top-level def/class in the four monoliths, this map says:

- Which capability owns it (or "shared" if used by 2+).
- Proposed new home (file path under `capabilities/<name>/` or `kavi-runtime/kavi_runtime/runtime/`).

Inventory built 2026-06-02 as the planning artifact for the physical move.

## Convention

`kp` = kavi_persona · `i2t` = inbox_to_task · `rtk` = realtime_kavi · `coord` = coordination · `shared` = `kavi_runtime/runtime/`.

---

## `kavi_runtime/handlers.py` (4,798 lines, 72 top-level defs)

| Line | Def | Owner | New home |
|---|---|---|---|
| 94 | `_dedup_check` | shared | `kavi_runtime/runtime/dedup.py` |
| 108 | `_dedup_record` | shared | `kavi_runtime/runtime/dedup.py` |
| 120 | `_per_message_lock` | shared | `kavi_runtime/runtime/dedup.py` |
| 127 | `_eval_inbox_judgments_path` | i2t | `capabilities/inbox_to_task/paths.py` |
| 134 | `_runtime_events_path` | shared | `kavi_runtime/runtime/paths.py` |
| 140 | `_latency_sec` | shared | `kavi_runtime/runtime/time_utils.py` |
| 153 | `_decision_id` | shared | `kavi_runtime/runtime/ids.py` |
| 178 | `_log_email_event` | i2t | `capabilities/inbox_to_task/eval_log.py` |
| 271 | `_get_clients` | shared | `kavi_runtime/runtime/clients.py` |
| 282 | `_send_imessage_with_fallback` | shared | `kavi_runtime/runtime/send_imessage.py` |
| 460 | `send_imessage_raw` | shared | `kavi_runtime/runtime/send_imessage.py` |
| 479 | `_normalize_email` | i2t | `capabilities/inbox_to_task/selection.py` |
| 503 | `_build_thread_state` | i2t | `capabilities/inbox_to_task/selection.py` |
| 520 | `_inbox_owner_abbrev` | i2t | `capabilities/inbox_to_task/owner_resolver.py` |
| 543 | `_resolve_shared_list_id` | i2t | `capabilities/inbox_to_task/task_writer.py` |
| 565 | `_account_to_owner_name` | i2t | `capabilities/inbox_to_task/owner_resolver.py` |
| 579 | `_apply_lifecycle_update` | i2t | `capabilities/inbox_to_task/task_writer.py` |
| 735 | `_log_webhook_redup_hit` | i2t | `capabilities/inbox_to_task/eval_log.py` |
| 774 | `email_arrived` | i2t | `capabilities/inbox_to_task/webhook.py` (entry point) |
| 809 | `_email_arrived_impl` | i2t | `capabilities/inbox_to_task/webhook.py` |
| 1571 | `_check_spend_cap_after_call` | shared | `kavi_runtime/runtime/spend_cap.py` |
| 1604 | `_send_or_queue_alert` | shared | `kavi_runtime/runtime/alerts.py` |
| 1622 | `imessage_received` | kp | `capabilities/kavi_persona/qa_loop/inbound.py` (entry point) |
| 1669 | `_imessage_received_impl` | kp | `capabilities/kavi_persona/qa_loop/inbound.py` |
| 1867 | `_classify_pause_intent` | kp | `capabilities/kavi_persona/actions/pause_correction.py` |
| 1880 | `_handle_resume` | kp | `capabilities/kavi_persona/actions/pause_correction.py` |
| 1949 | `_handle_pause_ambiguous` | kp | `capabilities/kavi_persona/actions/pause_correction.py` |
| 1963 | `_load_recent_outbound` | kp | `capabilities/kavi_persona/actions/_outbound_context.py` |
| 1987 | `_log_action_intent_decision` | kp | `capabilities/kavi_persona/actions/eval_log.py` |
| 2019 | `_owner_prefix_from_sender` | kp | `capabilities/kavi_persona/actions/owner.py` |
| 2045 | `_parse_deadline_iso` | kp | `capabilities/kavi_persona/actions/deadlines.py` |
| 2066 | `_handle_create_task_verb` | kp | `capabilities/kavi_persona/actions/create_task.py` |
| 2236 | `_enforce_g_a1_or_alert_fallback` | kp | `capabilities/kavi_persona/actions/g_a1_enforcer.py` |
| 2310 | `_send_action_layer_reply` | kp | `capabilities/kavi_persona/actions/send_reply.py` |
| 2359 | `_compose_action_clarifying_reply` | kp | `capabilities/kavi_persona/composers/action_clarifying_reply.py` |
| 2399 | `_save_pending_clarification_for_action` | kp | `capabilities/kavi_persona/actions/clarification_state.py` |
| 2461 | `_try_resolve_pending_clarification` | kp | `capabilities/kavi_persona/actions/clarification_state.py` |
| 2678 | `_try_anchor_then_sweep` | kp | `capabilities/kavi_persona/actions/anchor_sweep.py` |
| 2895 | `_topic_tokens` | kp | `capabilities/kavi_persona/actions/topic_match.py` |
| 2928 | `_share_topic_keyword` | kp | `capabilities/kavi_persona/actions/topic_match.py` |
| 2939 | `_resolve_candidate_ids` | kp | `capabilities/kavi_persona/actions/candidate_resolver.py` |
| 2966 | `_try_handle_action_intent` | kp | `capabilities/kavi_persona/actions/dispatch.py` |
| 3504 | `_try_handle_coordination_intent` | coord | `capabilities/coordination/dispatch.py` |
| 3575 | `_send_imessage_with_fallback_and_context` | shared | `kavi_runtime/runtime/send_imessage.py` |
| 3610 | `_handle_correction` | kp | `capabilities/kavi_persona/actions/correction.py` |
| 3796 | `_strip_kavi_prefix` | kp | `capabilities/kavi_persona/actions/title_edits.py` |
| 3810 | `_swap_owner_in_title` | kp | `capabilities/kavi_persona/actions/title_edits.py` |
| 3824 | `_replace_body_in_title` | kp | `capabilities/kavi_persona/actions/title_edits.py` |
| 3843 | `_replace_tag_in_title` | kp | `capabilities/kavi_persona/actions/title_edits.py` |
| 3862 | `_adjust_confidence_marker` | kp | `capabilities/kavi_persona/actions/title_edits.py` |
| 3871 | `_apply_qa_resolutions` | kp | `capabilities/kavi_persona/qa_loop/resolutions.py` |
| 3926 | `_handle_qa_reply` | kp | `capabilities/kavi_persona/qa_loop/inbound.py` |
| 3946 | `_handle_q_and_a_reply` | kp | `capabilities/kavi_persona/qa_loop/inbound.py` |
| 3992 | `_handle_examples_proposal_reply` | kp | `capabilities/kavi_persona/qa_loop/examples_proposal.py` |
| 4035 | `_append_household_example` | kp | `capabilities/kavi_persona/qa_loop/examples_proposal.py` |
| 4081 | `_fetch_open_todo_task_ids` | kp | `capabilities/kavi_persona/selection.py` |
| 4120 | `_filter_summary_queue_by_open_status` | kp | `capabilities/kavi_persona/selection.py` |
| 4155 | `_filter_pending_questions_by_open_status` | kp | `capabilities/kavi_persona/selection.py` |
| 4200 | `_count_pending_facts` | kp | `capabilities/kavi_persona/selection.py` |
| 4220 | `_read_pending_facts_for_summary` | kp | `capabilities/kavi_persona/selection.py` |
| 4269 | `_periodic_summary_state_path` | kp | `capabilities/kavi_persona/composers/periodic_summary.py` |
| 4277 | `_periodic_summary_input_hash` | kp | `capabilities/kavi_persona/composers/periodic_summary.py` |
| 4320 | `_load_periodic_summary_last_hash` | kp | `capabilities/kavi_persona/composers/periodic_summary.py` |
| 4336 | `_save_periodic_summary_last_hash` | kp | `capabilities/kavi_persona/composers/periodic_summary.py` |
| 4360 | `_should_suppress_periodic_summary` | kp | `capabilities/kavi_persona/composers/periodic_summary.py` |
| 4398 | `_pick_summary_anchor` | kp | `capabilities/kavi_persona/selection.py` |
| 4423 | `periodic_summary` | kp | `capabilities/kavi_persona/composers/periodic_summary.py` (entry point) |
| 4444 | `_periodic_summary_impl` | kp | `capabilities/kavi_persona/composers/periodic_summary.py` |
| 4639 | `correction_pattern_check` | kp | `capabilities/kavi_persona/qa_loop/correction_patterns.py` |
| 4659 | `_correction_pattern_check_impl` | kp | `capabilities/kavi_persona/qa_loop/correction_patterns.py` |
| 4770 | `subscription_renewal_check` | rtk | `capabilities/realtime_kavi/subscription_renewal.py` |
| 4785 | `subscription_renewal_check_all_accounts` | rtk | `capabilities/realtime_kavi/subscription_renewal.py` |

## `kavi_runtime/claude_client.py` (2,635 lines)

Module-level helpers:

| Line | Def | Owner | New home |
|---|---|---|---|
| 27 | `_est_input_tokens` | shared | `kavi_runtime/runtime/llm_client.py` |
| 38 | `_safe_clarify_fallback_question` | kp | `capabilities/kavi_persona/composers/action_clarifying_reply.py` |
| 79 | `_resolve_anthropic_api_key` | shared | `kavi_runtime/runtime/llm_client.py` |

`ClaudeClient` class (line 105–end). The class itself becomes the thin LLM wrapper; per-composer methods move to their capability dirs.

| Line | Method | Owner | New home |
|---|---|---|---|
| 106 | `__init__` | shared | stays — `kavi_runtime/runtime/llm_client.py` |
| 147 | `_log_call_start` | shared | stays |
| 162 | `_log_call_done` | shared | stays |
| 228 | `_log_call_failed` | shared | stays |
| 244 | `_model_for_call_type` | shared | stays |
| 260 | `_max_tokens_for_call_type` | shared | stays |
| 277 | `_skill` | shared | stays |
| 280 | `_household` | shared | stays |
| 283 | `_inbox_to_task` | shared | stays |
| 295 | `_persona` | shared | stays |
| 306 | `_security_baseline` | shared | stays |
| 317 | `_build_system_prompt` | shared | stays |
| 392 | `_persona_system_block` | shared | stays |
| 427 | `run_email_to_tasks` | i2t | `capabilities/inbox_to_task/compose.py` (top-level fn) |
| 552 | `_extract_json` | shared | stays (static helper) |
| 572 | `classify_correction` | kp | `capabilities/kavi_persona/composers/correction_classifier.py` |
| 628 | `compose_summary` | kp (deprecated alias) | `capabilities/kavi_persona/composers/periodic_summary.py` |
| 632 | `compose_periodic_summary` | kp | `capabilities/kavi_persona/composers/periodic_summary.py` |
| 739 | `check_semantic_duplicate` | i2t | `capabilities/inbox_to_task/dedup.py` |
| 823 | `classify_pause_intent` | kp | `capabilities/kavi_persona/composers/pause_intent.py` |
| 890 | `compose_weekly_self_check` | kp | `capabilities/kavi_persona/composers/weekly_self_check.py` |
| 940 | `classify_self_check_reply` | kp | `capabilities/kavi_persona/composers/weekly_self_check.py` |
| 1012 | `compose_qa_question` | kp | `capabilities/kavi_persona/composers/qa_question.py` |
| 1072 | `classify_qa_reply` | kp | `capabilities/kavi_persona/composers/qa_reply.py` |
| 1165 | `classify_action_intent` | kp | `capabilities/kavi_persona/composers/action_intent.py` |
| 1270 | `match_target_to_open_task` | kp | `capabilities/kavi_persona/composers/target_match.py` |
| 1456 | `resolve_pending_action_clarification` | kp | `capabilities/kavi_persona/composers/clarification_resolver.py` |
| 1616 | `compose_action_clarifying_reply` | kp | `capabilities/kavi_persona/composers/action_clarifying_reply.py` |
| 1760 | `compose_post_action_reply` | kp | `capabilities/kavi_persona/composers/post_action_reply.py` |
| 1823 | `compose_batch_action_reply` | kp | `capabilities/kavi_persona/composers/batch_action_reply.py` |
| 1919 | `compose_conversational_reply` | kp | `capabilities/kavi_persona/composers/conversational_reply.py` |
| 1988 | `classify_coordination_intent` | coord | `capabilities/coordination/composers/intent_classifier.py` |
| 2104 | `compose_coordination_ack` | coord | `capabilities/coordination/composers/ack.py` |
| 2132 | `compose_coordination_addressee_message` | coord | `capabilities/coordination/composers/addressee_message.py` |
| 2242 | `parse_coordination_reply` | coord | `capabilities/coordination/composers/reply_parser.py` |
| 2342 | `classify_coordination_course_correction` | coord | `capabilities/coordination/composers/course_correction.py` |
| 2487 | `compose_coordination_outcome` | coord | `capabilities/coordination/composers/outcome.py` |
| 2577 | `_compose_with_voice_cap` | shared | stays |

## `kavi_runtime/structural_checks.py` (363 lines)

Constants/cross-cutting gates stay. Kavi-persona-specific:

| Lines | Symbol | Owner | New home |
|---|---|---|---|
| 117-146 | `FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES`, `text_contains_forbidden_clarify_state_claim` | kp | `capabilities/kavi_persona/composers/action_clarifying_reply.py` |
| 289-345 | `compute_action_claim_correspondence_rate` | kp (weekly metric) | `capabilities/kavi_persona/weekly_self_check.py` |

Everything else (constants `LENGTH_CAP_*`, `ALLOWED_EMOJIS`, regexes, `passes_g_*`, `text_contains_action_verb`, `structural_check`, `all_structural_pass`) is cross-cutting; stays.

## `kavi_runtime/synthetic_compose.py` (513 lines)

| Lines | Function | Owner | New home |
|---|---|---|---|
| 45-112 | `replay_periodic_summary` | kp | `capabilities/kavi_persona/verify.py` |
| 115-183 | `replay_email_classify` | i2t | `capabilities/inbox_to_task/verify.py` |
| 189-290 | `verify_email_classify_selection` | i2t | `capabilities/inbox_to_task/verify.py` |
| 312-381 | `_title_keywords`, `_output_names_title`, `_detect_duplicate_phrases` | kp | `capabilities/kavi_persona/verify.py` |
| 384-513 | `verify_periodic_summary_selection` | kp | `capabilities/kavi_persona/verify.py` |

After the move, `synthetic_compose.py` exists only as a thin re-export glue OR is deleted, with server.py importing directly from `capabilities.<name>.verify`.

---

## Order of execution

1. Build `kavi_runtime/runtime/` shared modules (`dedup.py`, `clients.py`, `send_imessage.py`, `time_utils.py`, `ids.py`, `paths.py`, `spend_cap.py`, `alerts.py`, `llm_client.py`).
2. Move handlers.py shared helpers into `runtime/` first. Tests pass.
3. Per capability, smallest first:
   - `coordination` — only `_try_handle_coordination_intent` + 6 ClaudeClient methods.
   - `realtime_kavi` — `subscription_renewal_check{_all_accounts}` + already-existing server/scheduler files.
   - `inbox_to_task` — webhook entry + 4 task-writer helpers + `run_email_to_tasks` + `check_semantic_duplicate`.
   - `kavi_persona` — biggest, everything else.
4. After each capability move: tests pass, commit, push, deploy, verify_deploy.sh PASS.
5. Lock with `test_no_capability_code_in_runtime.py`.

---

## Pragmatic note on imports inside capability dirs

`capabilities/<name>/*.py` files MAY import from `kavi_runtime.runtime.*` (cross-cutting plumbing — the LLM client wrapper, send_imessage, dedup cache, etc.) and from `kavi_runtime.*` for canonical infra modules (`state`, `state_per_concept`, `graph_client`, `bluebubbles_client`, `lifecycle`, `package_extractor`, `guardrails`, `structured_log`, `trace_log`, `outbound_log`, `outbound_scanner`, `runtime_status`, `runtime_health`, `secrets`, `config`, etc.).

They MAY NOT import from `kavi_runtime.handlers` or `kavi_runtime.claude_client` (those are the monoliths being broken up). After the move, `handlers.py` is a tiny shell (≤200 lines) holding only the orchestration glue + module-level state that two-or-more capabilities both need; `claude_client.py` is the LLM client wrapper (≤300 lines).
