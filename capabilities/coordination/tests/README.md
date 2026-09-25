# coordination tests

Cross-cutting tests live under `kavi-runtime/tests/`:

- `test_coordination_handler.py` — happy path, branch routing, session
  lookup, outcome composer.
- `test_coordination_course_correction.py` — requester corrects mid-flow,
  reply parser semantics.
