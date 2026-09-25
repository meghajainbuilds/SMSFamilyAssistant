# realtime-kavi — runtime infrastructure capability

This directory is the canonical entry point for any bug, change, or new
behavior tied to the always-on runtime (webhook reception, scheduler ticks,
the FastAPI server shell, route registration, runtime health, deploy
plumbing).

## Where code lives

`capability_type: [runtime]`. No LLM composer; no spec.md compose rules.
The webhook reception, scheduler shell, FastAPI server shell, and health
endpoint live in cross-cutting runtime modules under
`kavi-runtime/kavi_runtime/` (the runtime package) because the webhook
shell is one process for ALL capabilities. Capability-specific runtime
hooks (e.g., subscription renewal) live physically in this directory.

| If you're looking for... | Open... | Source of truth |
|---|---|---|
| Behavior spec (what the runtime does) | `spec.md` | this file |
| Webhook reception + route registration | `server.py` | `kavi_runtime/server.py` (process-wide shell) |
| Scheduler shell | `scheduler.py` | `kavi_runtime/scheduler.py` (process-wide shell) |
| Subscription renewal (Graph webhook keepalive) | `subscription_renewal.py` | **physically here** (def body lives in this dir; scheduler imports it) |
| Health endpoint + startup probe | `health.py` | `kavi_runtime/runtime_health.py`, `kavi_runtime/startup_probe.py`, `kavi_runtime/state_invariants.py` |
| Deep verify procedure | `verify.py` | `kavi_runtime/runtime_status.py` (status endpoint) |
| Guardrails (webhook flood, spend cap, recipient allowlist) | `guardrails.py` | `kavi_runtime/guardrails.py`, `kavi_runtime/outbound_scanner.py`, `kavi_runtime/runtime/spend_cap.py` |
| Deploy script + verifier | `deploy/` | `kavi-runtime/scripts/deploy.sh`, `verify_deploy.sh` |

## Three-axis split

`runtime` capabilities don't have a PERSONA / STRUCTURAL / BEHAVIOR axis (no
LLM composer). The shape is simpler: webhook in → side effect → response.

## Deep verify

Capability type is `runtime` only. Per the role-registry policy, shallow
verify is acceptable: `shallow:status_endpoint` curls `GET /status` and
asserts `spec_loaders_ok: true`. See `verify.py` for the re-export.

Rationale for not deep-verifying: there's no LLM composer to replay. The
right deep verify for runtime infra is synthetic webhook integration
testing — different machinery, build when a real bug surfaces.

## Tests

- Cross-cutting runtime tests: `kavi-runtime/tests/test_status_endpoint.py`,
  `test_funnel_reachability.py`, `test_startup_probe.py`,
  `test_subscription_state_migration.py`, `test_runtime_smoke_test.py`.
- New tests scoped to this capability go in `tests/`.
