# capabilities/ — one living document per capability

Loaded only when Claude is working in `capabilities/`. This file is **navigation only** — read sibling files when you need their content.

## What lives here

One markdown file per capability. Each file is a living behavior spec (Anthropic Model Spec ancestry, not a 2025 PRD).

**Frontmatter — ****`capability_type`**** (added 2026-04-30):** every capability doc declares one or more types from this list, defined at the top of `Templates/HomeOS-capability.md`:

- **judgment** — classifier outputs (skip / create / route / label). Eval = precision/recall/F1.
- **generative** — produces text humans read. Eval = Glean rubric (Completeness / Personalization / Tone / Groundedness), binary 0/1.
- **two-way** — multi-turn conversation; user can correct, redirect, send free-form replies.
- **agentic** — multi-step planning + tool use.
- **retrieval** — answers grounded in indexed sources.
- **meta** — role frame, principles, onboarding plan (e.g., the meta layer of `kavi-persona`).
- **runtime** — always-on infrastructure / orchestration (e.g., `realtime-kavi`).

A capability can be more than one type (e.g., `kavi-persona` is `[generative, two-way]`).

**Section order — exhaustive template, prune at use-time.** The template at `Templates/HomeOS-capability.md` covers every section any capability type might need; each section carries an `<!-- applies-to: ... -->` tag. When a section doesn't apply to a capability's `capability_type`, omit it from the doc and list it in the footer audit line:

```
_Sections omitted as N/A for `capability_type: [<types>]`: <list of pruned sections>_
```

Always-present spine (applies to all):
1. **TL;DR** — 1-2 sentences, current state
2. **Open questions** — live, unresolved, PM-actionable
3. **Why now** — Cagan VVUF + AI risk taxonomy + Doshi LNO
4. **Behavior (the spec)** — Good / Bad / Few-shot examples (this IS the eval rubric)
5. **Metrics** — thresholds + rationale + Goodhart watches inline
6. **Architecture** — implementation as it stands
7. **Out of scope** (optional)
8. **Changelog** — append-only, dated; written as decisions are made (no per-entry approval needed; surface in chat for visibility)

Conditional sections (per applies-to tags): Vision / Principles / Onboarding plan (meta) · Persona / Voice rules / Steering (generative, two-way) · System prompt + sub-sections (LLM-driven types) · Tool list (agentic) · Grounding (retrieval).

Updated in place as the capability evolves.

## Template for new capabilities

When creating a new capability, copy `Templates/HomeOS-capability.md` to `capabilities/<slug>.md`, set `capability_type` in frontmatter, then keep only the sections whose applies-to tag matches your type(s). Append the footer audit line. Do NOT fork the template — extend in place and grow the applies-to tags as new capability types emerge.

For interview-driven capability builds, run `Templates/PROMPT - Discovery interview.md`. The interview opens by inferring `capability_type` and listing which stages will run vs skip; user confirms before stages execute. Output maps 1:1 to the spec template above.

## Files in this directory

- **`_role_registry.md`** (registry, not a capability doc) — one row per capability mapping it to the Investigator + Verifier sub-agent procedures (log path + verifier_procedure). Phase 0c shipping gate for any new capability lives here. Read when adding a new capability or wiring a Verifier procedure.
- **`inbox-to-task.md`** (status: shipped v0; v0.1 thread-aware in flight) — the v0 capability. Read Megha's Outlook inbox → write tasks to the McMullen-Jain Shared MS To Do list with single-owner accountability. Owns the precision/recall/owner-accuracy ship gates.
- **`kavi-persona.md`** (status: in progress; renamed from `chief-of-staff.md` 2026-04-30) — Kavi as the household's named Chief of Staff PLUS the universal character / voice / system-prompt layer that applies to every surface where Kavi speaks. `capability_type: [meta, generative, two-way]`. Three onboarding stages. Owns role-level metrics (DAM, pre-mortem accuracy) AND generative-rubric metrics (to be added during Stages 5-13 build). Surface-specific voice rules live in per-surface capabilities (e.g., `kavi-imessage.md`).
- **`realtime-kavi.md`** (status: in progress) — v0.2 always-on runtime. Closes the real-time loop, skip-correction capture, learning-from-correction. Owns latency + guardrail metrics.
<!-- private:ccm-001 -->- **`kavi-coordinates.md`** (status: proposed, 2026-05-05) — Cap 2. Reactive coordination across household members: Megha asks Kavi, Kavi messages Max, branches on his reply, reports outcome. `capability_type: [agentic, two-way, generative]`. Behavior spec drafted from the cash-scenario walkthrough; canonical few-shot is Rosa cash. Depends on action layer Stage 2 (`create_task` verb, parked) and `durable_facts.py` (shipped). The orchestration core that Cap 4 + Cap 6 build on top of.<!-- /private -->
- **`kavi-anticipates.md`** (status: proposed, 2026-05-05) — Cap 4 stub. Proactive coordination ahead of recurring household obligations (cleaner cash, diapers, chef pre-prep, school prep). `capability_type: [agentic, two-way, generative]`. Queued behind reactive Cap 2 (`kavi-coordinates`) and `scheduled-reminders`. Discovery interview in flight.
- **`url-summarizer.md`** (status: proposed, 2026-05-05) — Cap 5 stub. Fetches + summarizes content behind URLs (newsletter linked content, school portal pages). `capability_type: [agentic, generative]`. Callable from `inbox-to-task` for newsletter-shaped emails. Discovery pending; could ship before Cap 4 (simpler scope).
<!-- private:ccm-002 -->- **`external-thread-monitor.md`** (status: proposed, 2026-05-05) — Cap 6 stub. Reads iMessage threads that include non-household third parties (Nadia first), surfaces actionable content into the household chat. Surface-only at v0 (does NOT post into the external thread). `capability_type: [judgment, agentic]`. Queued behind Cap 1, 2, 3.<!-- /private -->

## When to write here

- **Before** a new capability ships: create `capabilities/<name>.md` with status `proposed`. Fill TL;DR, Why now, Behavior, Metrics, Open questions. Architecture and Changelog grow as the capability is built.
- **During** the build: update sections in place. Append decisions to the Changelog as they're made (one line per decision, dated, with **how I'll know I was wrong**).
- **After** ship: flip status to `shipped`. Revisit Behavior failure-mode predictions; mark which came true.
- **Write Changelog entries as decisions are made.** No per-entry approval needed (rule reversed 2026-05-05). Surface each new entry in chat for visibility (one line: "added entry to capabilities/X.md changelog: <summary>") so Megha can flag if anything looks wrong. Format: dated line, what changed, why, "how I'll know I was wrong."
- **If your capability declares any metric, the eval surface ships with the spec.** Required: `evals/<capability-slug>/` folder, `eval-<scope>-<thing>.jsonl` files (naming convention enforced — see `evals/CLAUDE.md`), and an entry in `evals/definitions.md` for every new metric. This is a single ship gate, not optional. Caught 2026-05-04 when `kavi-persona.md` declared Stage 4 metrics with no eval surface; rule added to prevent the next capability from escaping it.
- **If your `capability_type` includes `generative` / `two-way` / `agentic` / `runtime`, the action-claim principle must appear in Bad outputs of Behavior as a one-line plain-English rule** (e.g., "Kavi reports an action only after verifying the tool result succeeded"). The full Action-claim correspondence section in System Prompt is optional (updated 2026-05-26) and included only when capability-specific runtime gate scaffolding needs documentation. The runtime gate `passes_g_a1` in `kavi_runtime/structural_checks.py` is the backstop. Fills the gap that surfaced 2026-05-07 when the conversational composer fabricated past-tense action claims with no upstream tool call.

## Cross-references

- Metrics formulas (precision = X / Y, etc.) live once in `evals/definitions.md` as a glossary. Each capability doc names which metrics matter and why, with thresholds inline.
- Eval ritual (daily + weekly), golden set rules, and Goodhart watches live in `evals/ritual.md`.
- Run data (`runs.jsonl`), per-run reasoning (`scan-logs/`), and frozen examples (`golden-set/`) live in `evals/`.
