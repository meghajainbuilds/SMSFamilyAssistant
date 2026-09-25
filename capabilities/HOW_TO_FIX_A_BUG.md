# How to fix a bug — 4-line decision tree

Use this when a user-visible symptom shows up. The tree gets you from
"Megha says X is broken" to the right file in under a minute.

## 1. Name the user-visible symptom

Start with what Megha actually observed. Not "the runtime crashed" — "I
sent a Q&A reply and nothing happened" or "the 9 PM rollup mentioned a
task I already marked done."

## 2. Which capability owns this behavior?

| Symptom | Capability |
|---|---|
| Email arrived but no task was created (or wrong task) | `inbox_to_task/` |
| Email matched a Hard rule skip but got created anyway | `inbox_to_task/` |
| Task got created with the wrong owner | `inbox_to_task/` |
| A periodic_summary (morning digest or 9 PM rollup) said something wrong | `kavi_persona/` |
| Q&A reply not resolving the right task | `kavi_persona/qa_loop/` |
| Action layer ("mark done", "snooze") not firing | `kavi_persona/actions/` |
| Action-clarifying reply is wrong | `kavi_persona/composers/` (action_clarifying_reply_composer) |
| Weekly self-check message wrong / not firing | `kavi_persona/weekly_self_check.py` |
| Pause / resume / spend cap behavior | `kavi_persona/actions/` (pause_correction) |
| Megha asks Kavi to coordinate with Max; flow broken | `coordination/` |
| Webhook arrived but nothing happened | `realtime_kavi/server.py` (route + dispatch) |
| Scheduler tick missed or fired wrong time | `realtime_kavi/scheduler.py` |
| `/health` returns 503 | `realtime_kavi/health.py` |
| `/status` page wrong | `realtime_kavi/server.py` + `kavi_runtime/runtime_status.py` |

## 3. Open `capabilities/<name>/`

The directory's `README.md` has the axis-by-axis map: selection /
compose / verify / skills / actions / qa_loop / tests. Every file the
bug could live in is listed.

## 4. Fix in the right axis file

| Axis | What it owns | Files |
|---|---|---|
| **Selection** | Which inputs reach the LLM (filters, dedup, anchor pick) | `selection.py` |
| **Compose** | The LLM call site itself (system prompt, user prompt, parsing) | `compose.py` or `composers/` |
| **Verify** | Deep-verify procedure (shape + selection gates) | `verify.py` |
| **Skill** | BEHAVIOR-only rules (what to surface, when to skip) | `skill.md` or `skills/` (pointer to `kavi-runtime/skills/`) |
| **Action handler** | What to do after intent is classified (mark done / snooze / create) | `actions/` (kavi_persona only) |
| **Q&A loop** | Outbound questions + inbound replies state machine | `qa_loop/` (kavi_persona only) |
| **Spec** | Behavior spec — Good / Bad / Few-shot examples (the eval rubric) | `spec.md` (pointer to `capabilities/<name>.md`) |

## Three-axis split (LLM composers)

Every LLM composer has three axes; each axis has ONE canonical home.

- **PERSONA** (voice, identity, refusal) — `capabilities/kavi-persona.md`,
  loaded by `kavi_runtime/persona_loader.py`.
- **STRUCTURAL** (length caps, prose-vs-lists, JSON shape) —
  `kavi_runtime/structural_checks.py`. The runtime enforces these
  post-compose; the user_msg references the constants by name.
- **BEHAVIOR** (what to surface, when to skip, input-shape rules) —
  the per-skill file under `kavi-runtime/skills/`.

The architectural test
`kavi-runtime/tests/test_composer_skill_isolation.py` enforces that
skills do NOT re-encode voice or structural rules.

## Architectural tests (sanity checks before merging)

Four tests lock the shape. All must stay green:

- `tests/test_capability_isolation.py` — no cross-capability imports;
  every capability dir has the required entry-point files.
- `tests/test_no_legacy_state_refs.py` — no production module routes
  through the legacy load/save_imessage_state shim.
- `tests/test_deep_verify_parity.py` — every LLM-shaped capability has
  a deep verify endpoint with both shape and selection gates.
- `tests/test_composer_skill_isolation.py` — composer skills own
  BEHAVIOR only (no voice rules, no hardcoded length caps).

## Claim gates

Two phrases require pre-conditions before they leave the engineer's
mouth:

- **"X is broken because Y" / "the root cause is..."** — requires a
  fresh Investigator report. Spawn `subagent_type="investigator"`.
- **"X now works on Kavi" / "fixed and deployed"** — requires a fresh
  Verifier PASS. Spawn `subagent_type="verifier"` with the
  Investigation report. Curl the deep verify endpoint as evidence.

A passing test suite is NOT a Verifier PASS. The test suite measures
Python code; the Verifier measures live deployed behavior on Kavi.

## Deep verify endpoints (live, today)

<!-- private:fix-001 -->- `POST http://100.64.0.10:8080/synthetic/compose/kavi-persona` —
  raw compose for periodic_summary.
- `POST http://100.64.0.10:8080/synthetic/verify/kavi-persona` —
  compose + selection gates (done_task_surfaced, past_event_surfaced,
  duplicate_phrase).
- `POST http://100.64.0.10:8080/synthetic/compose/inbox-to-task` —
  raw compose for inbox classifier.
- `POST http://100.64.0.10:8080/synthetic/verify/inbox-to-task` —
  compose + selection gates (expected_decision, expected_owner,
  expected_confidence).
- `POST http://100.64.0.10:8080/synthetic/compose/kavi-coordinates` —
  raw compose for the coordination addressee-message composer.
- `POST http://100.64.0.10:8080/synthetic/verify/kavi-coordinates` —
  compose + gates (vague_addressee_message, empty_content_outbound,
  length_cap, prose_required).
- `GET http://100.64.0.10:8080/status` — shallow verify for
  realtime-kavi.<!-- /private -->

## Per-concept state files (post Phase 3, 2026-06-02)

One concept = one file = one canonical home. On Kavi:

```
/Users/kavi/HomeOS/state/
├── action_clarifications.json    # action layer pending clarifications
├── alert_dedupe.json             # handler_alerts dedupe
├── pause_state.json              # auto_runs_paused + paused_email_queue
├── pending_alerts.json           # quiet-hours alert queue
├── questions.json                # pending Q&As (the "iMessage state" of old)
├── self_check.json               # weekly self-check pending state
├── summary_queue.json            # periodic_summary anchor + queued items
├── coordination_sessions.json    # active coordination sessions (2026-06-10)
├── pending_facts.jsonl           # durable cross-day facts
├── imessage-state.json.pre-phase3-backup    # FROZEN, do not read
```

Helpers: `kavi_runtime/state_per_concept.py` defines `load_<concept>` /
`save_<concept>` for every concept. NEW code uses these directly.
