# inbox-to-task tests

Cross-cutting tests live under `kavi-runtime/tests/`:

- `test_email_to_tasks_cache_ttl.py`
- `test_lifecycle.py`
- `test_inbox_pre_filter.py`
- `test_synthetic_compose.py` (deep verify endpoint)
- `test_matched_task_eval_fields.py`
- `test_pattern_audit.py`
- `test_open_tasks_fetch_2026_05_08.py`
- `test_backfill_email_range.py`
- `test_extended_sensitive_patterns.py` (security gates)
- `test_financial_redaction.py` (security gates on task bodies)
