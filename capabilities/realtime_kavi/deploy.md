# realtime-kavi deploy pointer

The deploy + verify scripts live in `kavi-runtime/scripts/`:

- `deploy.sh` — rsyncs `kavi_runtime/`, `tests/`, `skills/`, `capabilities/`,
  `config.yaml`, and `scripts/` to Kavi; kickstarts the launchd service;
  records the deployed SHA; runs `verify_deploy.sh`; auto-rolls back on
  verify failure.
- `verify_deploy.sh` — 4-step harness (SHA match, hash match per dir,
  `/health`, end-to-end smoke webhook → outbound chain).
- `migrate_state_to_per_concept.py` — Phase 3 state migration CLI (idempotent;
  invoked at boot by `kavi_runtime/main.py` but also runnable standalone).

Auth and SSH setup live in the runtime engineering wiki; the launchd plist
lives at `kavi-runtime/launchd/`.
