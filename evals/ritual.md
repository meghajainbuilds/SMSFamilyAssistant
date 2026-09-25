# Eval ritual — daily and weekly

The eval *practice*. This file codifies what Megha actually does, when, and why. Updated when the ritual evolves.

## Context

Megha is the PM. Claude is the AI engineer. The ritual exists because:
1. The eval JSONL files (`eval-inbox-judgments.jsonl` on Kavi, runtime-appended) are for machines. Without a human-readable surface, evals don't happen.
2. Megha is doing L1 evals today via the shared HTML viewer at `evals/viewer.html`, which loads a weekly trace JSONL per capability and exports a labeled CSV for axial coding in Sheets. The chat-based daily slash command surfaces (`/eval-inbox-judgments`, `/eval-persona-outbound`) were retired 2026-05-27.
3. Without a frozen eval set, prompt changes have no regression test. Once L1 has accumulated enough labeled data, weekly ritual proposes additions to `golden-set/` and L2 unlocks.

The two-tier ritual produces graded data without friction (daily) and aggregates patterns / updates the spec (weekly). Golden set freezes when the data is good enough to regression-test against.

## The eval ladder (where HomeOS sits)

Adapted from the maturity model Hamel and Eugene Yan describe:

- **L0 — Vibes.** Ship and notice when it feels wrong. ~80% of LLM products live here.
- **L1 — Look at your data.** Read 50-100 actual traces by hand. Highest-leverage step most teams skip.
- **L2 — Labeled eval set.** Frozen 50-200 input/output pairs with what "good" looks like. Run on every prompt change.
- **L3 — LLM-as-judge.** Scale grading by having a model score outputs. Calibrated against humans first, or you're auditing yourself.
- **L4 — Production evals.** Continuous monitoring of real traffic, drift detection, segment analysis, online experiments tied to product metrics.

**HomeOS today: L1, target L2.** Annotation infrastructure exists (`/inbox-audit`, `runs.jsonl`, rubric in `capabilities/inbox-to-task.md`). What's missing: a frozen golden set that's gating prompt changes. We add to `golden-set/` weekly via the weekly ritual.

## Daily ritual (5 min, once per day)

**Forcing function:** Kavi's periodic summary iMessage (7 AM digest, end-of-day rollup at 8 PM, or any priority/low-conf interrupt) includes a one-line grading prompt for any tasks added since the last grading. No need to remember to invoke anything.

Grading happens once per day against all tasks added since yesterday's grading. The grading prompt and friction target stay identical.

**Surface:**
- iMessage from Kavi (existing) lists today's tasks with row numbers.
- New line at end: *"Reply when ready: e.g. 1✅ 2⚠️ 3❌ missed:0"*
- Megha replies inline via iMessage. Reply triggers the `/inbox-audit` flow that writes annotations to `runs.jsonl`.

**Annotation vocabulary** (per `evals/definitions.md`):
- ✅ correct (correct task + owner)
- ⚠️ wrong_owner (real task, wrong owner)
- ❌ false_positive (not a real task)
- 📭 missed (real task Megha added by hand)
- ⛓️‍💥 ungrounded_reason (correct decision, hallucinated reason — feeds Groundedness)

**Friction target:** 60 seconds.

**Failure mode if skipped:** Up to 24h of ungraded data accumulates. After 3 ungraded days, escalation banner in next Claude Code session: "you have N ungraded runs. Run `/inbox-audit` or grade via iMessage."

## Weekly ritual (30 min, Friday 2pm — placeholder, configurable)

**Forcing function:** Calendar block (Megha adds) + scheduled iMessage from Kavi Friday 2pm: *"Weekly eval time. Run /eval-week."* (Note: `/eval-week` skill is deferred to a follow-up; until then, the weekly ritual is run by hand against the past 7 days of `runs.jsonl` + `scan-logs/`.)

**What happens (when /eval-week ships):**

1. **Open the digest.** Megha runs `/eval-week`. Claude renders past 7 days of `runs.jsonl` as a markdown table in chat.
2. **Read patterns.** Claude surfaces 3-5 patterns from the week (e.g., "3 false negatives all on Bright Horizons early-dismissal emails," "skip rate jumped Tuesday after a capability-doc edit").
3. **Aggregate metrics vs. ship gates.** Two capabilities to aggregate now:
  - **inbox-to-task:** Precision, owner accuracy, recall, dedup, latency, cost — rolling 7d numbers vs. thresholds in `capabilities/inbox-to-task.md`. Source: `evals/inbox-to-task/eval-inbox-judgments.jsonl` joined with the latest labeled CSV under `evals/inbox-to-task/traces/`.
  - **kavi-persona:** DAM (7-day count + ≥3-word counter), Tone / Personalization / Groundedness / Completeness pass-rates over the past 7 days of Kavi outbound (Glean rubric, binary 0/1), day-mute events count over rolling 7 days, weekly self-check rating (this Friday's row). Sources: `evals/kavi-persona/eval-persona-day-mute-events.jsonl`, `evals/kavi-persona/eval-persona-weekly-self-check.jsonl`. Manual binary scoring on Kavi outbound until L2 lands; document samples in chat for now.
4. **Goodhart check.** Counter-metrics that should NOT have moved: emails Megha forwarded back from main inbox after Kavi missed them, % messages ≥3 words (DAM quality), precision split by source (Hard rule vs LLM).
5. **Decide what to update.** Three buckets:
  - Update the relevant `capabilities/*.md` (Hard rules, Examples, Q&A learned, behavior, failure modes).
  - Update `household.md` only if a roster, identity, or accountability fact changed.
  - Add the email to `evals/golden-set/` for regression testing.
6. **Reflect (Megha's voice, \~5 lines).** Top 2-3 decisions this week. What surprised. What carrying to work conversations / interview prep. One AI-adjacent decision Megha made at work this week — rate fast / hedged / regret.

**Megha's job:** Approve any capability-doc / household.md edits Claude proposes. Pick golden-set additions. Write the reflection paragraph.

**Anything updated → must pass \****`golden-set/`**\*\* on next prompt change.**

## Golden set rules

The golden set is a frozen folder of high-confidence examples used to regression-test prompt or rule changes. It grows from real failures and ambiguous-but-resolved cases, not from synthesis.

**When emails get added:**
- Weekly ritual surfaces a confident annotation that's interesting (failure mode, learning-from-correction win, ambiguous case Megha resolved).
- Megha approves "add to golden set." Claude writes a markdown file to `evals/golden-set/<YYYYMMDD>-<slug>.md` containing: email payload (subject, sender, body, thread state), expected output (task / skip / [?]), expected owner, expected confidence, reason.

**When emails get retired:**
- Email's pattern has been consistently passing for 4+ weeks AND the sender pattern has been generalized into the originating capability's Examples or Hard rules. Then the specific email is no longer load-bearing for the golden set.
- Megha approves retirement. File moves to `evals/golden-set/archive/`.

**When the golden set runs:**
- Before any prompt change to `email-to-tasks` or `correction-classifier`.
- Before any structural change to a capability's Behavior section (Hard rules edit, Examples reorganization).
- After 4 weeks of running, the golden set should detect at least one regression that would otherwise have shipped silently. If it never catches anything in 4 weeks, the set is too easy — add harder cases.

## Goodhart watches

Each metric is gameable in a specific way. Counter-metrics catch the gaming.

| Metric | Gaming pattern | Counter-metric |
| --- | --- | --- |
| Skip rate trending down | LLM defaulting to skip on ambiguous emails | Weekly count of emails Megha forwards back from main inbox after Kavi missed them |
| Precision climbing | Over-pruning; recall collapses | 📭 (missed) count per week |
| High-confidence outputs all from Hard rules | LLM judgment isn't earning its keep | Precision split by source (Hard rule vs LLM) |
| Groundedness > 95% but precision flat | LLM citing safe Examples patterns regardless of fit, just to score grounded | Spot-check 5 random correct rows/week against the actual Examples + Q&A patterns referenced |
| DAM = 7/7 | Perfunctory replies | % of Megha→Kavi messages ≥3 words |
| Repeat-error rate < 10% | Kavi avoiding corrections altogether | Total corrections logged should stay 3-5/day, not collapse |
| Correction → Examples-row rate ≥40% | Approving low-quality proposals | Precision after Examples additions should not regress |
| Decision-log entries 3-5/week | Trivia padding | % of entries flagged "muscle: governance" or "muscle: risk" |

The senior move (Goodhart trap) is to throw out an eval that is winning while users are losing. If skip rate hits target but Megha is forwarding emails back from her inbox, the eval is broken — not the system.

## What this ritual replaces

Pre-2026-04-29:
- `judgment-log/weekly.md` — Megha's hand-written end-of-week reflection. Empty template. Folded into the weekly-ritual reflection step above.
- `judgment-log/decisions.md` — separate decisions log. Per-capability changelogs in `capabilities/*.md` now own these entries. Same "Claude proposes, Megha confirms, Claude writes" rule applies.
- `metrics/daily-report.md` — auto-generated stub. Removed. Daily surface is the iMessage grading prompt; weekly surface is `/eval-week`.

## Open questions

- Friday 2pm is a placeholder. Weekly ritual time is Megha's call. Add to her calendar after first run.
- `/eval-week` skill is deferred. First weekly ritual will be done manually against `runs.jsonl` + `scan-logs/`. If manual is fine, we don't need the skill. If it's friction, build it.
- 50 emails as golden-set target is a guess (Hamel uses 50). For an N=1 product with low daily volume (~10-20 emails/day), the right number might be 20-30 and grow weekly. Resize after 2 weeks.
- Whether to add a SessionStart hook that nudges Megha if a daily ritual was skipped 3+ days. Defer until the calendar block + iMessage forcing function fail.
