# coordination (kavi-coordinates) — household coordination flows

This directory is the canonical entry point for any bug, change, or new
behavior tied to multi-party coordination (Megha asks Kavi → Kavi messages
Max → branches on his reply → reports outcome back to Megha).

`capability_type: [agentic, two-way, generative]`. Spec:
`capabilities/kavi-coordinates.md`.

## Where code lives

| Axis | File | Source of truth |
|---|---|---|
| Behavior spec | `spec.md` (pointer) | `capabilities/kavi-coordinates.md` |
| Selection (intent classifier — is this a coordination ask?) | `selection.py` | `kavi_runtime/coordination_handler` + `kavi_runtime/handlers._try_handle_coordination_intent` |
| Compose (intent classifier + addressee composer + outcome composer + reply parser) | `composers/` | `kavi_runtime/claude_client.compose_coordination_*` |
| Verify (deep verify procedure) | `verify.py` | `verify.py` IS the canonical home (2026-06-10); runtime endpoints `/synthetic/compose/kavi-coordinates` + `/synthetic/verify/kavi-coordinates` import from it via `kavi_runtime/synthetic_compose.py` |
| Skills (BEHAVIOR-only composer specs) | `skills/` | `kavi-runtime/skills/coordination_intent_classifier.md`, `coordination_addressee_message_composer.md`, `coordination_reply_parser.md`, `coordination_outcome_composer.md` |
| Session state | `session_state.py` | `handler.py` (in-process registry, mutation owner) + per-concept file `coordination_sessions.json` (write-through persistence since 2026-06-10; helpers in `kavi_runtime/state_per_concept.py`) |

## Three-axis split

- **PERSONA** — coordination messages route through the kavi-persona voice
  layer (loaded by `persona_loader.py`). The voice rules are NOT duplicated
  in coordination skills.
- **STRUCTURAL** — length caps + format gates live in
  `kavi_runtime/structural_checks.py`. Coordination skills reference the
  constants by name, not literal values.
- **BEHAVIOR** — per-skill files under `skills/` (intent classification,
  addressee message composition, reply parsing, outcome reporting).

## Deep verify

Capability type is LLM-shaped (`[agentic, two-way, generative]`). Per the
Phase 0c gate, deep verify shipped 2026-06-10. `verify.py` is the canonical
home of the gate logic; registry row + procedure definition
(`deep:coordination_addressee`) live in `capabilities/_role_registry.md`.

- `POST /synthetic/compose/kavi-coordinates` — raw replay of the
  addressee-message composer (Investigator surface).
- `POST /synthetic/verify/kavi-coordinates` — compose + verdict. Shape
  gates: `length_cap`, `prose_required`. Selection gates:
  `vague_addressee_message` (output must carry at least one content
  keyword from the injected coordination ask), `empty_content_outbound`
  (an empty-content session must not produce an outbound).

Further gate candidates (addressee misroute, requester double-ack, session
timeout leak, outcome mismatch with addressee reply) stay proposed; add
them when an incident shows the shape.

## Tests

Cross-cutting tests live under `kavi-runtime/tests/`:

- `test_coordination_handler.py`
- `test_coordination_course_correction.py`
