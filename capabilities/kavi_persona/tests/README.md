# kavi-persona tests

Cross-cutting tests live under `kavi-runtime/tests/`. The full list is
long; below is grouped by axis.

## Selection layer
- `test_periodic_summary_open_status_filter.py`
- `test_pending_facts_freshness_filter.py`
- `test_pending_facts_surfacing.py`
- `test_summary_anchor_then_sweep.py`

## Composers
- `test_periodic_summary_debounce.py`
- `test_synthetic_compose.py`
- `test_synthetic_verify_periodic_summary.py`
- `test_action_clarifying_composer_fix2.py`
- `test_build_system_prompt_includes_persona_from_loader.py`
- `test_build_system_prompt_includes_security_baseline.py`
- `test_persona_loader.py`
- `test_composer_skill_isolation.py`
- `test_skill_files_have_no_embedded_persona.py`

## Action handlers
- `test_action_layer_llm_first_2026_05_08.py`
- `test_mark_done_verb.py`
- `test_create_task_verb.py`
- `test_pending_clarification.py`
- `test_structural_g_a1.py`

## Q&A loop
- `test_handler_alerts.py`
- `test_handlers_imports.py`

## Deep verify
- `test_synthetic_verify_periodic_summary.py`
- `test_synthetic_compose.py`
