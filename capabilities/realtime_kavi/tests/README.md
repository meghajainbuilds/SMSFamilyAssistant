# realtime-kavi tests

Cross-cutting tests for this capability live in `kavi-runtime/tests/`:

- `test_status_endpoint.py`
- `test_funnel_reachability.py`
- `test_startup_probe.py`
- `test_subscription_state_migration.py`
- `test_runtime_smoke_test.py`
- `test_webhook_redup_dedup.py`
- `test_deploy_scripts.py`

New tests scoped to this capability should be added in either location. As
the refactor settles, the canonical home will be `kavi-runtime/tests/`
(pytest discovers tests there today) until the per-capability test
discovery wiring is built.
