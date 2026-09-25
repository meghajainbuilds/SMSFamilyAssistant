# evals/ — eval data and practice for HomeOS

Loaded only when Claude is working in `evals/`. This file is **navigation only** — read sibling files when you need their content.

## Files in this directory

- **`inbox-to-task/`** — per-capability eval folder for the inbox-to-task capability. Contains:
  - `eval-inbox-judgments.jsonl` (runtime data feed; runtime appends rows on Kavi).
  - `traces/` — viewer input/output for the HTML-viewer + weekly open-coding flow (see "Trace inputs and labeled outputs" below).
- **`definitions.md`** — metric formula glossary, annotation vocabulary, JSONL schemas. Slim by design: thresholds and rationale live with the capability they measure (`capabilities/<name>.md`). **Read this when implementing or modifying observability code.**
- **`ritual.md`** — daily and weekly eval ritual, codified. The eval ladder (L0-L4) and where HomeOS sits. Golden set rules. Goodhart watches per metric. **Read this every Friday before the weekly ritual; read it again before any prompt or capability-doc change that affects judgment.**
- **`golden-set/`** — folder of frozen example emails used for regression testing. Cross-capability. One markdown file per locked example: email payload + expected output + reason. L2 surface; deferred until L1 has accumulated enough labeled data to seed it.

## When to read what

- Writing or changing eval surfaces or `/metrics` → read `definitions.md` first.
- Adding a new capability eval surface → read `definitions.md` (schema patterns) + `ritual.md` (cadence patterns), then create a sibling `evals/<capability>/` folder following the inbox-to-task structure.
- Running the weekly ritual → `ritual.md`.
- Looking up a metric's threshold → don't look here; look at the relevant `capabilities/<name>.md`. The capability doc is the source of truth for thresholds + rationale.

## Naming convention (REQUIRED)

Every per-capability eval surface follows the same shape:

- **Folder:** `evals/<capability-slug>/` — slug matches `capabilities/<slug>.md`.
- **JSONL files:** `eval-<scope>-<thing>.jsonl`. `<scope>` is a short capability nickname (e.g., `inbox` for inbox-to-task, `persona` for kavi-persona); `<thing>` describes what each row captures (e.g., `judgments`, `weekly-self-check`, `day-mute-events`).
- **Traces subfolder:** `evals/<capability-slug>/traces/` for the HTML-viewer + weekly open-coding flow (see next section).

When a new capability with metrics ships, the matching `evals/<slug>/` folder + JSONL files + `definitions.md` schema additions ship with it. Capability docs that declare metrics without an eval surface are an incomplete ship (rule enforced in `capabilities/CLAUDE.md` "When to write here").

## Held-out improvement set (prompt-optimizer surface, added 2026-06-24)

`evals/<slug>/improvement-set.jsonl` is the **held-out set the prompt optimizer is allowed to climb**. It is the climbing target; the frozen matrix under `matrix/` stays the don't-regress guard. Two distinct files, two distinct jobs — never the same file.

- **Row schema:** identical to the frozen matrix — one JSON object per line, `{case_id, payload, expect}` where `expect` carries `verdict` plus optional `must_contain_any` / `must_not_contain`. Same `payload` is the exact `/synthetic/verify/<capability>` body.
- **SEPARATE from the frozen matrix on purpose.** It lives at `evals/<slug>/improvement-set.jsonl`, NOT under `matrix/`, so editing or growing it never trips `matrix_freeze.py`'s tamper check. The optimizer reads this file; it never reads or writes the frozen matrix.
- **Not populated with real cases here by design.** A seed/example row set lives only as a test fixture at `kavi-runtime/tests/fixtures/improvement-set.example.jsonl`. Populate the real per-capability file when you start optimizing that capability.
- **Consumed by** `kavi-runtime/scripts/optimize_prompt.py` (Phase 3 prompt-improvement engine): scores the current skill on this set (measured baseline), proposes N skill variants, ranks them, and writes `evals/<slug>/optimizer/variant-*.md` + `optimizer/optimizer-report.json`. Variant scores in the report are **predicted, not measured**, until the variant is deployed to staging and re-scored (the report is explicit about this).

## Trace inputs and labeled outputs (HTML-viewer surface)

Each capability with an HTML-viewer eval surface owns a `traces/` subfolder under its `evals/<capability-slug>/` directory:

- **Input to viewer:** `traces/eval-<scope>-week<N>.jsonl` — one row per unit-to-be-labeled (session for persona, decision for inbox). Built weekly by `scripts/build_eval_traces.py` (forthcoming).
- **Output from viewer:** `traces/eval-<scope>-labeled-week<N>-<YYYY-MM-DD>.csv` — Megha's labels + open codes + axial codes after open coding in the viewer and downstream axial coding in Sheets.

The HTML viewer itself lives at `evals/viewer.html` (shared tool, not per-capability — no viewer forks).

## What does NOT live here

- Why a metric exists, what threshold it has, what counts as Goodhart gaming → these live in `capabilities/<name>.md` because metrics are owned by the capability they measure (Hamel: don't separate evals from the behavior they grade).
- Decision history → per-capability changelogs in `capabilities/<name>.md`.
- Behavior specs (what "good" looks like) → `capabilities/<name>.md` Behavior section.

## When NOT to write here

- Never edit `eval-inbox-judgments.jsonl` or `eval-persona-outbound-judgments.jsonl` by hand. The runtime appends to judgments.
- Never duplicate threshold values from a capability doc. If a threshold needs to be referenced operationally (e.g., regression alert rule), reference it once and pull from the capability doc when the system reads it.
