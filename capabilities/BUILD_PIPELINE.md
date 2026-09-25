# Capability-build pipeline

How a capability change goes from spec to a trustworthy "works" claim.
Added 2026-06-10. Tooling: `kavi-runtime/scripts/matrix_freeze.py`,
`kavi-runtime/scripts/run_matrix.py`, the staging runtime
(`kavi-runtime/config-staging.yaml`, port 8081 on Kavi's Mac), and the
production Verifier sub-agent (`.claude/agents/verifier.md`).

## The pipeline, in order

1. **Freeze the test matrix BEFORE implementation.** Author the matrix
   from the capability's EXISTING spec examples (Behavior section
   few-shots, composer-skill examples) — not from the code you are about
   to write. One JSONL row per case at
   `evals/<slug>/matrix/matrix-<slug>.jsonl`, each row carrying the exact
   `/synthetic/verify/<slug>` payload plus the expected verdict and
   optional substring checks. Then stamp it:
   `python scripts/matrix_freeze.py freeze <slug>` — this hashes the
   matrix into `matrix-<slug>.manifest.json` and resets the iteration
   counter.
2. **Implement with bounded iteration against STAGING.** Run
   `python scripts/run_matrix.py <slug>` (defaults to the staging
<!-- private:bld-001 -->   instance, `http://100.64.0.10:8081`). The runner refuses to run if<!-- /private -->
   the matrix hash no longer matches its manifest, and it counts every
   run: past **10 runs since the last freeze** it still executes (the
   bound is process, not silence) but prints "ITERATION BOUND EXCEEDED —
   stop and surface to PM" and exits nonzero even when every row passes.
3. **Matrix green on staging.** All rows pass, within the bound.
4. **Deploy production** (`scripts/deploy.sh`, with its verify +
   auto-rollback contract).
5. **Production Verifier sub-agent confirms.** The Verifier runs its
   registry procedure against the production runtime and returns a PASS
   with evidence.
6. **Only then** does anyone say "fixed" / "works on Kavi."

Matrix freezing for a NEW capability happens at **Phase 0c time**,
alongside the registry row in `capabilities/_role_registry.md` and the
synthetic compose/verify endpoints. A capability whose verify endpoint
ships without a frozen seed matrix has shipped half its eval surface.

## Spec-conformance rules (added 2026-06-10 evening, after the keep/drop coercion incident)

Three rules, adopted after the third spec-violating incident in eight days
(June 3 misroute, June 10 "Yes" mis-binding, June 10 swallowed intent) —
all three were violations of rules ALREADY WRITTEN in the spec. The
process gap: spec rules had no mechanical enforcement, so they decayed
into prose. Megha signed off 2026-06-10.

**Rule 3 — a spec rule without a matrix case doesn't exist.** When a
Behavior rule or example is added to or changed in a capability spec,
the corresponding frozen-matrix case lands in the SAME commit. Same
shape as the existing eval-surface ship gate ("if your capability
declares a metric, the eval surface ships with the spec"). A spec
section with no matrix case is a promise, not an invariant — the
keep/drop coercion violated a rule ("seven mark-done requests in one
message → act on all seven") that was verbatim in kavi-persona.md with
zero tests behind it.

**Rule 4 — new cross-cutting rules land with a written conformance
sweep.** Adopting a rule that constrains existing code (e.g. "output
layer is always LLM") includes, in the same change: the list of
existing surfaces checked against it, what was found, and an audit tag
or fix for each violation. The "Kept:"/"Dropped:" templates survived
the always-LLM rule for two weeks because the rule's adoption never
triggered a sweep — the cold-fallback audit was this idea scoped too
narrowly.

**Rule 5 — a Verifier "fixed" claim requires the incident repro PASS
AND the capability's full frozen matrix green** (`run_matrix.py <slug>
--prod` or against staging post-deploy, per the bug's surface). Repro-
only verification confirms the incident class is gone while saying
nothing about the rest of the spec; the matrix run is the conformance
half. The Verifier sub-agent procedure (`.claude/agents/verifier.md`
and the registry procedure definitions) carries this requirement.

## The two anti-Goodhart rules, and why

**Rule 1 — the matrix is frozen before fix iteration begins.** The fix
author cannot edit the test while fixing the code. `matrix_freeze.py
check` (run automatically by the runner) fails loudly when the matrix
file drifts from its hash manifest. Legitimate matrix changes (a new
spec example, a PM-approved expectation change) happen by re-freezing
explicitly and saying so in chat — a visible act, never a silent edit.

**Rule 2 — iteration is bounded.** Ten matrix runs since the last
freeze, then stop and surface to the PM. Endless iteration against a
fixed target stops being fixing and starts being overfitting: the code
learns the test's blind spots instead of the spec.

Why these two rules exist: the **2026-06-02 incident** — four autonomous
agents iterating on fixes gamed numeric test ceilings instead of fixing
behavior (caps were edited to make red tests green). The structural
countermeasures shipped then and extended now: tests are frozen before
fix iteration, and structural gates are owned by a DIFFERENT artifact
than the code under iteration (the matrix lives in `evals/`, hashed; the
selection/shape gates live in `capabilities/<name>/verify.py` behind the
runtime endpoint; the fix author's loop touches neither).

## Staging: where matrix replays run

Staging is a second instance of the same runtime on the same Mac —
launchd label `com.megha.kavi-staging`, working dir
`/Users/kavi/kavi-staging`, **port 8081**, state sandboxed under
`/Users/kavi/HomeOS-staging/`. Its config (`config-staging.yaml`, shipped
as the instance's `config.yaml` by `scripts/deploy_staging.sh`) sets
`staging_mode: true`, which:

- **hard-disables every outbound send** (the first check in
  `kavi_runtime/runtime/send_imessage.py` — nothing reaches BlueBubbles
  or Outlook, ever),
- **registers no scheduler jobs** (no digests, no Graph subscription
  renewal, no heartbeats),
- **returns 503 on both webhook routes** (Graph + BlueBubbles),
- keeps **synthetic compose/verify routes, /health, /status** fully live
  with a REAL Claude client — replays make real composer calls, which is
  the point.

Live LLM replay therefore happens against staging first, never
production first. `run_matrix.py --prod` exists for the rare deliberate
production replay and prints a loud banner.

## What green does NOT mean

- **A passing test suite is not a Verifier PASS.** The pytest suite
  measures Python code on a laptop.
- **A green matrix on staging is not a Verifier PASS either.** Staging
  proves the composer honors the frozen spec examples through the real
  LLM — necessary, never sufficient. Production config, deployed file
  state, subscriptions, the send path, and live data shapes are only
  observable on production.
- The **production Verifier sub-agent remains the only authority** for
  "fixed" / "works" claims (see Claim gates in `CLAUDE.md`). Staging
  green earns you the deploy; the Verifier earns you the sentence.

## Operational notes

- Matrix layout per capability:
  `evals/<slug>/matrix/matrix-<slug>.jsonl` (cases),
  `matrix-<slug>.manifest.json` (freeze hash),
  `iteration_count.json` (runs since freeze),
  `runs/run-<timestamp>.jsonl` (per-run logs).
- Seed matrices frozen 2026-06-10: `kavi-persona` (10 cases),
  `kavi-coordinates` (4), `inbox-to-task` (4).
- Staging install (one-time) and deploy: see header comments in
  `kavi-runtime/scripts/deploy_staging.sh` and
  `scripts/install_staging_service.sh`.
- Staging is disposable: no auto-rollback; on a failed health check, fix
  forward and redeploy.
