---
description: DEPRECATED 2026-05-27 — weekly aggregator for the inbox-to-task capability. Retired alongside the chat-based daily labeling flow. The new weekly surface is the HTML viewer at evals/viewer.html loading evals/inbox-to-task/traces/eval-inbox-week<N>.jsonl, with axial coding in Sheets afterward. File preserved for historical procedure reference only.
allowed-tools:
  - Read
  - Bash
argument-hint: "(deprecated)"
---

> **Kavi's address:** `100.64.0.10` and `kavi-mac.your-tailnet.example` below are public placeholders. Before any ssh or curl, read `CLAUDE.local.md` at the repo root (gitignored) for the real values and substitute them.
# /eval-inbox-week — DEPRECATED 2026-05-27

**This skill is retired.** It depended on the now-archived `eval-inbox-labels.jsonl` (see `archive/2026-05-pre-html-pivot/`) and on the also-retired companion weekly persona aggregator. The replacement weekly surface for inbox-to-task is the shared HTML viewer at `evals/viewer.html` loading `evals/inbox-to-task/traces/eval-inbox-week<N>.jsonl` (viewer build pending; see backlog), with axial coding in Sheets afterward.

The procedure below is preserved verbatim as historical reference for what the chat-based weekly aggregator computed; do not run it.

## Inputs

- `--days <N>` — optional. Default: 7. Window in days, ending today (Pacific).

## Procedure

### 1. Window resolution

Compute `start_date = today_pacific - <days> + 1`, `end_date = today_pacific`.

### 2. Fetch judgments from Kavi's runtime

For each date in [start_date, end_date], fetch from the runtime endpoint:

```bash
ssh kavi@100.64.0.10 "for d in <date_list>; do curl -s --max-time 10 'http://127.0.0.1:8080/evals/inbox-to-task/recent?date='\$d'&limit=200'; echo; done"
```

Parse each line as JSON, collect all `rows`. Filter to `capability == "inbox-to-task"` (defensive).

If SSH fails: print the unreachable message and exit.

### 3. Read labels

Read `/Users/meghajain/Documents/HomeOS/evals/inbox-to-task/eval-inbox-labels.jsonl`. Build a map `decision_id → most_recent_label` (most recent by `annotated_at`). Synthetic `missed:N` rows have `decision_id == null` — keep them as a separate count.

### 4. Compute metrics

Per `evals/definitions.md` formulas:

- **Precision** = `correct / (correct + wrong_owner + false_positive)`
- **Owner accuracy** = `correct / (correct + wrong_owner)`
- **Recall** = `(correct + wrong_owner) / (correct + wrong_owner + missed)`
- **Groundedness** = `correct_grounded / (correct_grounded + ungrounded_reason)` (computed only on labels among `correct` rows; `ungrounded_reason` rows count as ungrounded; un-flagged correct rows count as grounded)
- **Dedup correctness** — derived from `decision == "dedup_hit"` rows; flag any false-positive dedup hits via Megha's `❌` labels on dedup decisions.
- **Latency** — median + p95 of `latency_first_action_sec` across all rows.
- **Token cost** — sum of `usage.input_tokens + output_tokens × model_price` across the window.
- **Confidence calibration** — split precision by `confidence == "high"` vs `confidence == "low"`.

Only count labeled rows for precision/recall/owner-accuracy. Note unlabeled count separately.

#### 4a. Package lifecycle metrics (added 2026-05-05)

Three watch metrics that piggyback on the same judgments × labels join. Slot under the existing metrics table in section 8 as new rows. Compute over the same window.

- **Tier-2 false-merge rate.** Among judgment rows where `package_match_tier == "tier2_heuristic"`, count how many were labeled as a wrong merge.
  - Numerator: rows joined to a label where `label == "false_merge"` (🔀; added 2026-05-05 to the annotation vocabulary in `evals/definitions.md`). Precise count — no note-text scan, no LLM judgment over free-form notes.
  - Denominator: total `tier2_heuristic` rows in the window.
  - Report as: `false_merges / tier2_total = X%` plus absolute counts.
  - **Goodhart watch:** if rate >25% across ≥4 tier-2 rows in the window, surface `⚠️ tier-2 heuristic drifting — false-merge rate >25% this week; review the merchant + recipient + 7d window rule in capabilities/inbox-to-task.md "Package lifecycle"`.

- **Audit-log length distribution.** For each lifecycle transition this week, measure the task body character length captured at write time.
  - Source: read `task_body_length` directly from judgment rows in the window where `lifecycle_state != null` AND `task_body_length != null`. The runtime captures this field at write time inside `_apply_lifecycle_update` (handlers.py, added 2026-05-05) — no round-trip to MS To Do needed.
  - Report: p50, p95, max body length in characters across the matching rows.
  - **Goodhart watch:** if p95 > 4000 chars, surface the line `⚠️ audit-log bloat risk — p95 body length crossed 4000 chars; consider lifecycle audit-trail trimming or rotation`.

- **Carrier inference miss rate.** Among judgment rows that look like package emails (any row with `lifecycle_state` set OR `package_id` set), count how many have `carrier == null` or carrier missing entirely from the row.
  - Numerator: package rows where `carrier` is None / missing.
  - Denominator: total package rows in the window (`lifecycle_state` set OR `package_id` set).
  - Report as: `unknown_carrier / total_package_emails = X%`.
  - **Goodhart watch:** if rate > 30%, surface `⚠️ new carrier format may have appeared — carrier inference missed >30% of package emails this week; review package_extractor patterns against last week's package senders`.

### 5. Compare vs thresholds

Read thresholds from `capabilities/inbox-to-task.md` Metrics table (lines starting with `| Precision`, `| Owner accuracy`, etc.). For each metric, mark `✓` if at/above threshold, `⚠️` if below for the window, `🛑` if it's a hard gate breached.

### 6. Pattern surfacing

Read all reasons from labeled rows. Cluster by sender/subject keywords. Surface the top 3-5 patterns by frequency. For each:
- Pattern (sender or subject keyword)
- Count
- Outcome distribution (correct / wrong_owner / false_positive / missed)

### 7. Goodhart watches (per `evals/ritual.md`)

Compute counter-metrics:
- `% of Megha→Kavi messages ≥3 words` — proxy for DAM quality. Read from corrections.jsonl + qa replies. Flag if <70%.
- Precision split by Hard rule vs LLM source — flag if Hard rule precision <80%.
- Forwarded-from-main-inbox count — emails Megha forwarded to herself after Kavi missed them. Manual check; surface "review main inbox forwards" reminder.

### 8. Render markdown report

Print:

```markdown
# /eval-inbox-week — <start_date> to <end_date>

## Volume
- Judgments processed: N
- Labeled: M (P% of judgments)
- Synthetic misses logged: K

## Metrics (rolling 7d)

| Metric              | Value      | Threshold | Status |
| ------------------- | ---------- | --------- | ------ |
| Precision           | 92.3%      | ≥90%      | ✓      |
| Owner accuracy      | 96.1%      | ≥95%      | ✓      |
| Recall              | 78.0%      | ≥85%      | ⚠️     |
| Groundedness        | 91.0%      | ≥90%      | ✓      |
| Dedup correctness   | 100%       | 100%      | ✓      |
| Latency p95         | 12s        | ≤15s      | ✓      |
| Cost (7d)           | $1.24      | <$5/wk    | ✓      |
| Confidence calibration | high=95%, low=64% | high>low | ✓ |
| Tier-2 false-merge rate | 1/8 = 12.5%      | <25%     | ✓      |
| Audit-log length p95 | 1820 chars       | <4000     | ✓      |
| Carrier inference miss | 2/14 = 14%       | <30%     | ✓      |

## Patterns
1. **Maple school** (n=12): 9 correct, 2 wrong_owner, 1 false_positive
2. **Boonli** (n=4): 2 correct, 2 missed
...

## Goodhart watches
- DAM quality: 78% of replies ≥3 words ✓
- Hard rule precision: 100% ✓
- Audit-log bloat (p95 body length): 1820 chars ✓
- Carrier inference miss rate: 14% ✓
- (manual): Did you forward anything from main inbox after a Kavi miss this week?

## Alerts
🛑 / ⚠️ rows from above

## Recommended updates
- Per the spec, ⚠️ on Recall for 3+ days triggers a Hard rule / Examples review.
- Megha decides: open `capabilities/inbox-to-task.md`, add Examples for Boonli pattern (2 missed in 7d).
```

### 9. Cluster notes → propose backlog entries

Read all `note` fields from `eval-inbox-labels.jsonl` in the window. Cluster by recurring theme (e.g., "Boonli pattern misclassified," "Hard rule fires on edge case," "Owner accuracy drops on shared accounts"). Clusters are LLM synthesis, not regex.

For each cluster of ≥2 notes, surface a PROPOSED backlog entry in chat:

```
Cluster: <theme>  (N notes, M decision_ids)
Examples: <quoted snippets from notes>

Proposed backlog entry for kavi-runtime/backlog.md:
  Title: <short>
  PM frame:
    What's broken: <user-visible failure>
    Who feels it: <Megha / family member, when>
    Why now: <Leverage / Neutral / Overhead per Doshi>
  Eng sketch: <what the fix looks like>
  Verification check: <command or observation that proves the fix>

Approve? (yes / edit / no)
```

Wait for Megha's per-entry response. On `yes`, append to `kavi-runtime/backlog.md`. On `edit`, accept her revisions and append. On `no`, drop.

Single-note items (no cluster) are flagged at the bottom as "uncategorized notes for review."

### 10. Spec changelog updates

If any metric is `⚠️` or `🛑` for ≥3 consecutive days OR Megha approves a spec change in this rollup, write the changelog entry directly to `capabilities/inbox-to-task.md` (no per-entry approval required, per `feedback_capability_changelog_policy.md`). Format: dated line + what changed + why + "how I'll know I was wrong." Surface a one-line confirmation in chat: `added entry to capabilities/inbox-to-task.md changelog: <summary>`.

For non-trivial changes (Hard rule additions, threshold reversals, new acceptance criteria), surface the full draft text in chat alongside the write.

## What this command does NOT do

- Modify Kavi's runtime, judgments, or labels (read-only).
- Write to `kavi-runtime/backlog.md` without per-entry approval. Backlog is forward-looking eng commitment; each entry needs Megha's yes.
- Aggregate across capabilities. Use `/eval-persona-week` for the generative side.

## Cross-references

- Metric formulas: `evals/definitions.md`
- Threshold sources: `capabilities/inbox-to-task.md` Metrics
- Daily L1 surface: `/eval-inbox-judgments`
- Companion: `/eval-persona-week`
