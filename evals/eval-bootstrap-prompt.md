# Eval Bootstrap Prompt for HomeOS

> Hand this to Claude Code at the start of an evals-focused session.
> Last updated: 2026-05-12

## Context

We are starting from zero on real failure data for Kavi (iMessage AI assistant). Weeks of iteration on prompts, tools, and unit tests have not converged. The diagnosis: I built test scaffolding without first doing structured error analysis on real traces. The existing analysis is half baked, painful to do on iMessage output and isn't improving the outcomes. We are resetting to the Hamel Husain / Shreya Shankar approach: error analysis first, then automated evaluators only where prompt edits can't fix the failure.

The blocker right now is infrastructure. I don't believe we are capturing traces in a form I can actually open-code. Fix that first.

I am the benevolent dictator on labeling. You build the tooling. I bring the household context and the product judgment about what is wrong.

## Methodology (non-negotiable)

1. Capture real traces of Kavi exchanges in a form I can read.
2. I open-code a random sample: free-form notes on the first/most upstream thing wrong in each trace. Specific, not "janky." 
3. Continue until theoretical saturation (no new failure modes appearing). 
4. Use an LLM to cluster my open codes into axial codes (failure-mode categories). I refine the categories. None-of-the-above is always a category.
5. Categorize all traces into axial codes. Count with a pivot table.
6. For obvious failure modes, just fix the prompt. No eval.
7. For the residual pesky failure modes, build narrow LLM-as-judge evals (binary pass/fail, one failure mode per judge, validated against my labels with a confusion matrix, not just an agreement percentage). 4 to 7 judges total, max.
8. Run judges in CI and on a daily production sample.

## Plan mode first

Do not write code or modify files yet. Produce a plan with three parts.

### Part 1: Audit existing evals scaffolding

Read these files and tell me which are premature (built on guesses about failure modes I haven't actually observed) versus which are still useful:

- `evals/CLAUDE.md`
- `evals/ritual.md`
- `evals/definitions.md`
- `evals/runs.jsonl`
- `evals/golden-set/` (list contents)
- `evals/scan-logs/` (list contents)
- `capabilities/kavi-persona.md`
- `capabilities/realtime-kavi.md`
- `capabilities/inbox-to-task.md`
- `kavi-runtime/` (whatever is built today)

Use the diagnosis format from `CLAUDE.md` (WHAT BROKE / WHAT STILL WORKS / WHAT THIS AFFECTS / WHAT TO DO). Per artifact, recommend one of:

- **PAUSE**: park, do not maintain until I have real failure data
- **EVOLVE**: modify to fit the new workflow
- **KEEP**: still useful as-is

Be honest. The `scan-logs/` folder existing before any open-coding work is a smell. Call it out if it is one.

### Part 2: Design trace capture and review workflow

This is the critical infrastructure piece.

**Trace capture requirements**

- Every Kavi exchange: inbound message, system prompt at that moment, any retrieved context, tool calls and their results, outbound response (or the fact that none was sent)
- Metadata per trace: `trace_id`, `thread_id`, `timestamp`, `channel` (iMessage / inbox / etc.), `participant`
- Stored as JSONL on Kavi's Mac (`kavi-runtime` path)
- Append-only. No schema churn after we commit.

**Propose, concretely**

1. **JSONL schema** for one trace per line. Show the actual field names and types.
2. **Instrumentation point** in `kavi-runtime`. Be specific: which Python module, which call site. If the runtime isn't built far enough yet to instrument, tell me what needs to land first and what the minimum surface is.
3. **Sampling export script**: pulls N random traces from a date range and writes a CSV with columns: `trace_id`, `timestamp`, `channel`, `trace_content_readable`, `my_open_code` (blank), `my_axial_code` (blank). The readable column is what I will actually read while labeling, so it needs to be a flat, human-readable rendering of the trace that fits inside a single Google Sheets cell (50,000 char limit, line breaks preserved).
4. **Viewing tool recommendation** for browsing traces *before* I label in Sheets. One recommendation, not a list. My budget for new tools is low. Candidates to consider: Phoenix/Arize (local, open source), Braintrust (hosted, costs money, what Hamel demoed), a tiny custom local HTML viewer that renders the JSONL, or "just use VS Code on the JSONL." Pick one. Defend it on setup time, dollar cost, and ongoing maintenance burden.
5. **End-to-end flow check**: walk me through the exact sequence from "Kavi exchanges happened today" to "30 of them are sitting as rows in a Google Sheet I can open and label." Surface anything in that chain that requires my action.

### Part 3: Plan to leverage traces we already have
Show:

- Hours I spend on labeling vs. hours of your engineering time
- What blocks on me vs. what blocks on you
- What artifact gets produced at the end of week one. Target: a counted, axial-coded failure-mode list grounded in real Kavi traces.

## Hard constraints

- No LLM judges this session. Not even speculatively.
- No new unit tests.
- No additions to the golden set.
- No dashboards.
- Do not extend or change `runs.jsonl` format.

All of the above is downstream of error analysis on real data.

## Output format

Plan mode response only. Three sections matching Part 1, Part 2, Part 3. Use the diagnosis format from `CLAUDE.md` for Part 1. Be specific and grounded. If you have not read a file, do not opine on it.

Wait for my approval before writing any code or changing any files. After approval, log the architectural decisions to the appropriate capability changelog and to per-project memory at `/Users/meghajain/.claude/projects/-Users-meghajain-Documents-HomeOS/memory/`.

## Why now

Kavi is failing the most basic value prop: understanding what I mean and taking the right action. Every week we run on guess-based fixes is a week the household keeps not trusting Kavi. The infrastructure spend (a few days) buys a permanent improvement loop. Without it, we keep iterating in the dark.
