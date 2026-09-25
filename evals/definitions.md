# evals/definitions.md — formula glossary

Metric formulas, annotation vocabulary, and the eval JSONL schemas. Slim by design: **thresholds and rationale live in \****`capabilities/<name>.md`**, not here. This file is the formula glossary that capability docs reference.

If a formula here changes (e.g., recall denominator definition), update this file AND append a changelog entry to the relevant capability doc with rationale. Do not let them drift.

## Durable facts

Cross-day fact storage for Kavi (G-C3 v0, added 2026-05-05). NOT an eval surface — listed here because it is an append-only JSONL the runtime owns and Cap 2 (`kavi-coordinates`) will read at compose time. Schema lives here so capability docs can reference it without forking.

**Path.** `/Users/kavi/HomeOS/learned_facts.jsonl` (configurable via `kavi-runtime/config.yaml` -> `paths.learned_facts`).

**Module.** `kavi_runtime.durable_facts`. Public functions: `record_fact`, `read_active_facts`, `supersede_fact`, `expire_facts`. v0 is storage + helpers only; Cap 2 will wire reads into the reply-context builder.

**Schema (one JSON object per line, append-only):**

```json
{
  "fact_id": "f_2026-05-05T22:14:09Z_a8b3f291",
  "ts": "2026-05-05T22:14:09Z",
  "fact_text": "Kavi committed to only message Megha when a task is actually marked done",
  "scope": "kavi",
  "source_decision_id": "p_2026-05-05T22:13:50Z_conversational_77c2bf01",
  "expires_at": null,
  "status": "active"
}
```

- `fact_id`: stable id. Format: `f_<utc_iso>_<8hex>`.
- `ts`: UTC timestamp the row was appended.
- `fact_text`: plain-English fact (e.g., "Max is out of town this week, route everything to me").
- `scope`: one of `megha | max | household | kavi`. `kavi` is reserved for self-commitments Kavi made about its own behavior (the motivating G-C3 case).
- `source_decision_id`: id of the triggering decision (e.g., an outbound `decision_id`). Doubles as the back-pointer for `superseded` / `expired` follow-up rows: when status is `superseded` or `expired`, this field carries the prior `fact_id` being replaced.
- `expires_at`: ISO 8601 UTC at which the fact becomes stale, or null for indefinite.
- `status`: `active | superseded | expired`. `supersede_fact(fact_id)` writes a new `superseded` row; `expire_facts()` writes new `expired` rows for any active fact whose `expires_at` is in the past. Prior rows are never mutated; readers compute "is this still active" by collapsing the log to the latest row per `fact_id`.

**Read semantics.** `read_active_facts(scope=None, since=None)` collapses the log to the latest row per `fact_id`, filters out non-active rows, and applies optional `scope` and `since` filters. `since` is strictly-greater-than (a row whose `ts == since` is excluded).

**Concurrency.** Writes use `fcntl.flock` in EXCLUSIVE mode; reads use SHARED mode. Other append-only files in the runtime (corrections.jsonl, eval-*-judgments.jsonl) rely on POSIX append semantics without an explicit lock; durable facts adopt flock because a torn write would erase a commitment Kavi promised to keep.

## Metric formulas

Each metric is a formula. *What* threshold it has and *why* lives in the capability doc that uses it.

| Metric                                                            | What it measures                                                                                                                                                                                                                                                                                                                                                                                                    | Formula                                                                                                                                                                                                                            |        |                                            |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | ------------------------------------------ |
| **Precision**                                                     | Of the tasks Kavi created, what fraction were real tasks Megha wanted? Measures inbox trust — high precision = the shared list feels reliable, low precision = noise erodes the list.                                                                                                                                                                                                                               | `correct / (correct + wrong_owner + false_positive)` — wrong-owner tasks count as "real" because owner can be fixed in-app without invalidating the extraction                                                                     |        |                                            |
| **Owner accuracy**                                                | Of the real tasks created, what fraction went to the right person? Measures Fair Play single-CEO assignment quality — does Kavi pick correctly between Megha vs Max?                                                                                                                                                                                                                                                | `correct / (correct + wrong_owner)` — denominator excludes false positives (already penalized in precision)                                                                                                                        |        |                                            |
| **Recall**                                                        | Of all the real tasks across the inbox, what fraction did Kavi catch? Measures whether Megha can stop scanning her own inbox — high recall = trust the system to surface what matters.                                                                                                                                                                                                                              | `(correct + wrong_owner) / (correct + wrong_owner + missed)` — denominator includes 📭 tasks Megha added by hand                                                                                                                   |        |                                            |
| **Groundedness**                                                  | Of correct decisions, what fraction cite a real Hard rule, Example, or Q&A pattern (vs fabricated authority)? Measures audit-trail trust — when Kavi says "I did X because Y," is Y actually a real pattern in the spec?                                                                                                                                                                                            | `correct_grounded / (correct_grounded + ungrounded_reason)` — computed only on correct-decision rows; isolates reasoning quality from decision quality                                                                             |        |                                            |
| **Due-date accuracy**                                             | When Kavi extracted a deadline from email content, was the date right? Measures whether Megha can trust the dates Kavi puts on tasks without re-checking the source email.                                                                                                                                                                                                                                          | manual spot-check; counted only when Claude extracts a date                                                                                                                                                                        |        |                                            |
| **Dedup correctness**                                             | Did Kavi avoid creating the same task twice (from the same email or semantically equivalent emails)? Measures whether the shared list stays clean enough to feel usable.                                                                                                                                                                                                                                            | via `source_email_id` — skip if already on a task in To Do                                                                                                                                                                         |        |                                            |
| **Latency**                                                       | Wall-clock time from email arrival to task landing in MS To Do or iMessage going out. Measures whether Kavi feels fast enough to be useful in real time.                                                                                                                                                                                                                                                            | wall-clock seconds, end-to-end                                                                                                                                                                                                     |        |                                            |
| **Token cost**                                                    | Anthropic API spend per email processed. Measures whether the system is cheaper than the alternatives (Todoist Premium $60/yr, manual triage time).                                                                                                                                                                                                                                                                 | input + output tokens × model price                                                                                                                                                                                                |        |                                            |
| **Confidence calibration**                                        | Does Kavi's `high` confidence actually outperform `low` confidence in precision? Measures whether the `[?]` low-confidence prefix is a useful signal or just noise.                                                                                                                                                                                                                                                 | `precision(high)` vs `precision(low)` separately from annotations                                                                                                                                                                  |        |                                            |
| **Time-to-first-action** (v0.2)                                   | Speed of the realtime loop — when an email arrives, how soon does Megha hear about it on iMessage? Measures whether the v0.2 always-on architecture earns its complexity.                                                                                                                                                                                                                                           | email arrival timestamp (MS Graph) → outbound iMessage timestamp (BlueBubbles)                                                                                                                                                     |        |                                            |
| **Reply-to-update latency** (v0.2)                                | When Megha replies to Kavi via iMessage, how fast does MS To Do reflect the update + Kavi confirm back? Measures whether the corrections loop feels responsive enough to use.                                                                                                                                                                                                                                       | inbound iMessage timestamp → MS To Do mutation timestamp + outbound confirmation                                                                                                                                                   |        |                                            |
| **Daily Active Megha (DAM)** *(kavi-persona)*                     | Did Megha message Kavi at all today? Measures role survival — if Megha stops talking to Kavi, the role failed. Self-named failure mode.                                                                                                                                                                                                                                                                             | `1 if Megha→Kavi iMessage count today ≥ 1 else 0`, summed over 7 days. Counter-metric: % messages ≥3 words to catch perfunctory "ok" gaming.                                                                                       |        |                                            |
| **Pre-mortem accuracy** *(kavi-persona)*                          | Of the failure modes predicted in capability changelogs ("how I'll know I was wrong"), what fraction actually happened within 4 weeks of ship? Measures whether the pre-mortem rep is paying off — the trend is what matters, not the absolute number.                                                                                                                                                              | `failures_predicted_correctly / failures_predicted_total`, rolling over the last N changelog entries. NOT measuring: failures we never predicted.                                                                                  |        |                                            |
| **Decision latency & hedging** *(kavi-persona)*                   | Of one AI-adjacent decision/week Megha rates, what fraction does she rate as "fast" (not "hedged" or "regret")? Measures the internal success signal Megha named: faster, less-hedged AI product decisions at work.                                                                                                                                                                                                 | Self-rated tag per decision: `fast                                                                                                                                                                                                 | hedged | regret`. Trend over 4 weeks toward "fast." |
| **Tone** *(kavi-persona, Glean generative rubric)*                | Does the message match the voice register declared in the persona spec (≤120 char, prose, first-person, no formulaic warmth, no status-board language)? **The auto-generated feel is the user-felt failure mode**: tone-fail is what makes Megha mute the thread.                                                                                                                                                   | Binary 0/1 per message during weekly eval. Pass-rate = `messages_scored_1 / messages_scored_total` over the last 7 days.                                                                                                           |        |                                            |
| **Personalization** *(kavi-persona, Glean generative rubric)*     | Does the message use household knowledge correctly (right person, right context, right learned pattern)? Trend metric: improves over time as Q&A loop and Examples table grow.                                                                                                                                                                                                                                      | Binary 0/1 per message. Counter-metric: when Personalization=1, does the household reference serve the message or just decorate (cosmetic name-dropping)?                                                                          |        |                                            |
| **Completeness** *(kavi-persona, Glean generative rubric)*        | Does the message contain all the components needed for Megha to act? Tolerable-failure surface: incomplete messages can be patched by follow-up, unlike tone fails.                                                                                                                                                                                                                                                 | Binary 0/1 per message. Counter-metric: % messages exceeding the 120-char cap (catches stuffing).                                                                                                                                  |        |                                            |
| **Day-mute events** *(kavi-persona, system-level value)*          | Count of times in a 7-day window Megha set `quiet_until` via a steering command ("be quiet for the day," "no messages for 3 hours"). Behavioral signal that Kavi is annoying — observed, not self-reported. Leading indicator paired with the weekly self-check direct signal. **Theory caveat:** does NOT capture silent annoyance (e.g., system-level Do Not Disturb); the weekly self-check covers that surface. | Count of `quiet_until` set events in `eval-persona-day-mute-events.jsonl` over rolling 7 days. NOT distinguishing duration at write time — log raw, decide threshold at read time (rule: log-raw-decide-at-read-time, 2026-05-04). |        |                                            |
| **Weekly self-check rating** *(kavi-persona, system-level value)* | Megha's felt experience of whether Kavi saved or added cognitive load this week. Direct signal for the "more mental work than before Kavi" criterion that triggers permanent mute.                                                                                                                                                                                                                                  | LLM-classified into `{saved, added, neutral, unclear}` from Megha's free-form Friday-2pm reply. Rolling 4 weeks: target ≥3 of 4 weeks rated `saved`.                                                                               |        |                                            |
| **Coordination close rate without requester re-ping** *(kavi-coordinates)* | Of the coordinations Megha or Max initiated, what fraction did Kavi close out (report outcome back to requester) without the requester having to re-ping ("what happened with Max?"). Measures whether Kavi is actually absorbing the coordination loop. | `closed_without_repling / total_sessions`. A session counts as `closed_without_repling` when `outcome_report_sent_ts` is non-null AND there is no inbound from the requester between `ack_sent_ts` and `outcome_report_sent_ts` matching the pattern "what happened with..." (LLM-classified at label time). |        |                                            |
| **Follow-up window correctness** *(kavi-coordinates)* | When Kavi messages the addressee and the addressee doesn't reply right away, Kavi has to judge when to ping again. Measures whether the timing judgment was right — too quick = pesky, too slow = missed deadline. | Per-row binary label (0/1) at annotation time. `follow_up_window_correct = 1` when the labeler judged the timing right. Pass-rate = `rows_with_label_1 / rows_where_follow_up_was_needed` over the rolling 7-day window. |        |                                            |
| **False-positive task creation rate** *(kavi-coordinates)* | Of the coordinations where Kavi created a task in MS To Do, what fraction turned out to be unnecessary (addressee didn't actually commit, or commitment was misread). Measures trust erosion in the shared list at the coordinates entry point. | `false_positive_task_count / sessions_where_task_was_created` over the rolling 7-day window. `false_positive_task = 1` per row when the labeler flagged the creation as unnecessary. |        |                                            |
| **Attribution-judgment accuracy** *(kavi-coordinates)* | Per principle 1: attribution to the requester ("Megha mentioned cash for Rosa...") is judgment-based, not always-on. Personal asks need attribution; routine household tasks don't. Measures whether the persona made the right call per Kavi-to-addressee message. | Per-row binary label (0/1) at annotation time. `attribution_correct = 1` when the labeler agreed with the attribute-vs-omit call. Pass-rate = `rows_with_label_1 / total_kavi_to_addressee_messages` over the rolling 7-day window. |        |                                            |
| **Internal-ops leakage rate** *(kavi-coordinates)* | Per principle 2: Kavi reports outcomes, not internal steps. Measures whether the outcome report to the requester contains internal-mechanic narration ("I've set a reminder for him at 8am" / "I'll wait 30 minutes and ping him again"). Status-only updates ("Following up with Max, will let you know") do NOT count as leakage. | Per-row binary label (0/1) at annotation time. `internal_ops_leakage = 1` when the outcome report contained internal-mechanic narration. Rate = `rows_with_label_1 / total_outcome_reports` over the rolling 7-day window. Goodhart watch in `capabilities/kavi-coordinates.md`: 0% for 2+ consecutive weeks may signal Kavi has gone over-terse. |        |                                            |
| **Close-suggestion precision** *(kavi-persona, 2026-06-10)* | Of the close suggestions surfaced in the 9 PM rollup ("Looks like you already paid Boonli, want to close it?"), what fraction were tasks that really were done? Measures whether the suggest-to-close judge can be trusted — one wrong suggestion teaches Megha to ignore all of them. Pass/fail example: rollup suggests closing Boonli; Megha labels "yes, was done" → counts in numerator. Suggests closing the field-trip slip she hadn't signed → counts only in denominator. | `suggestions_labeled_actually_done / suggestions_surfaced_total` over the rolling 7-day window. Labels come from the weekly open-coding pass over `eval-persona-close-suggestions.jsonl` rows with `suggest_close=true` joined against the rollup outbound rows. Counter-metric (recall-flavored, computed from the same file): `suggest_close=true rate` over all judged pairs — a judge that never suggests is trivially precise and useless. |        |                                            |

### Why the denominators differ (Precision vs. Owner accuracy vs. Recall)

- **Precision** punishes false positives (stuff you didn't need to see).
- **Owner accuracy** doesn't punish false positives (already counted against precision) — it isolates the "pick the right person" skill.
- **Recall** punishes misses (stuff Claude should have caught) — orthogonal to the first two.

A system can have high precision + low recall ("silent and accurate, missing things") OR high recall + low precision ("catches everything, noisy as hell"). Capability docs explain why current thresholds favor one over the other.

## Annotation vocabulary

DEPRECATED 2026-05-27 along with the chat-based daily labeling surface. Kept here for historical reference (the symbol set drove the retired `/eval-inbox-judgments` slash command). New labels are free-form open codes captured in the HTML viewer; axial coding consolidates into clusters during the weekly Sheets step. The six symbols below mapped into the now-archived `eval-inbox-labels.jsonl`:

| Symbol | Meaning | Counts as |
| --- | --- | --- |
| ✅ | correct task + correct owner | `correct` |
| ⚠️ | correct task, wrong owner | `wrong_owner` |
| ❌ | not a real task (false positive) | `false_positive` |
| 📭 | real task missed (Megha added by hand) | `missed` |
| ⛓️‍💥 | correct decision, hallucinated reason | `ungrounded_reason` |
| 🔀 | tier-2 heuristic merge was wrong (right merchant, wrong order) | `false_merge` |

`ungrounded_reason` is annotated only when the *decision* is correct but the *reason* cites a Hard rule, Example, or Q&A pattern that doesn't actually exist in the capability spec. It feeds the **Groundedness** metric (separate dimension from precision/recall). If both decision and reasoning are wrong, label as `false_positive` (or `wrong_owner` / `missed` per the actual decision error) — `ungrounded_reason` is reserved for the right-call/fake-citation case.

`false_merge` (added 2026-05-05) is annotated only when `package_match_tier == "tier2_heuristic"` AND the merge was wrong (e.g., a Hanna shipped email got merged into an unrelated open Hanna task). The decision row's create-vs-update call was technically right (an update on an existing task is the right SHAPE of action), so `false_positive` overstates the harm; `false_merge` precisely names the heuristic miss. Feeds the **Tier-2 false-merge rate** watch metric directly (replaces the prior note-text scan).

Due-date accuracy and dedup correctness are audited separately — manually for v0, with their own run fields planned for v0.2+.

## JSONL schemas

The eval surface is two append-only files per capability, joined by `decision_id`. The unit of analysis is **one email-decision**, not one batch run. The runtime appends one row per decision; the PM's slash command appends one row per labeling event.

### `eval-inbox-judgments.jsonl` (lives on Kavi, runtime appends)

One JSON object per line. One record per email Kavi processed.

```json
{
  "decision_id": "d_2026-04-30T07:08:44Z_AQMkAD",
  "ts": "2026-04-30T07:08:44Z",
  "capability": "inbox-to-task",
  "email_id": "AQMkFAKE-email-0001...",
  "sender": "office@maplestreetschool.org",
  "subject": "Parent Association: meeting moved",
  "decision": "skipped",
  "reason": "FYI-only announcement, no specific ask",
  "confidence": "high",
  "task_id": null,
  "task_title": null,
  "task_owner": null,
  "imessage_sent": false,
  "latency_first_action_sec": 4,
  "usage": {"input_tokens": 3484, "output_tokens": 213, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 16195},
  "package_id": null,
  "package_match_tier": "none",
  "merge_target_task_id": null,
  "merge_reason": null,
  "lifecycle_state": null,
  "task_body_length": null,
  "source_account": "megha@example.com"
}
```

- `decision`: one of `created`, `skipped`, `deferred`, `dedup_hit`, `paused`, `token_blowup`, `updated` (added 2026-05-05 for package lifecycle UPDATE actions on an existing task).
- `task_id`, `task_title`, `task_owner`: populated only when `decision == "created"` or `decision == "updated"`. Null otherwise.
- `imessage_sent`: true when a low-confidence Q was sent to Megha OR a lifecycle ping (first create / OFD) fired.
- `confidence`: `high | medium | low` (LLM-reported judgment confidence). Null for hard-rule short-circuits.
- `source_account` (added 2026-05-05 evening): the Microsoft account whose mailbox received this email, e.g. `megha@example.com` or `max@example.com`. Null on rows generated outside an inbound (legacy single-account runs predating this field, or synthetic test rows). Lets the daily and weekly aggregators split precision / recall by stream so a regression on Max's inbox does not hide behind Megha's volume. Readers tolerate missing values per the additive-only authoring rule.

**Package lifecycle fields (added 2026-05-05; see `capabilities/inbox-to-task.md` "Package lifecycle" subsection for the state machine).**

- `package_id`: extracted order number, tracking ID, or merchant order URL. Null when no package signal in the email or the email is not a package-related email.
- `package_match_tier`: `"none" | "tier1_id" | "tier2_heuristic"`. Tier 1 = exact id match against an open task's body or linked-resource externalId. Tier 2 = heuristic merchant + recipient + ≤7-day window. `"none"` for first-occurrence emails or non-package emails.
- `merge_target_task_id`: when `package_match_tier != "none"`, the existing MS To Do task ID this email merged into. Null otherwise.
- `merge_reason`: human-readable string explaining a tier-2 merge (e.g., `"merchant=hannaandersson, recipient=Megha, gap=2d"`). Null on tier-1 (the id match is self-evident) or `none`.
- `lifecycle_state`: `"Ordered" | "Shipped" | "Out for Delivery" | "Delivered" | "Cancelled"` for package-related decisions. Null for non-package decisions.
- `task_body_length`: integer character count of the task body at write time, captured by `_apply_lifecycle_update` after the lifecycle templates render. Populated only on `lifecycle_updated` decisions. Null on creates and skips. Read by `/eval-inbox-week`'s audit-log length distribution metric (p50 / p95 / max) without re-fetching from MS To Do.

**Eval surfacing for tier-2 merges.** Tier-2 merges (`package_match_tier == "tier2_heuristic"`) surface in the HTML-viewer trace rows so Megha can confirm or split during weekly open coding. A wrong tier-2 merge gets coded as `false_merge` (per the historical annotation vocabulary above). Counts toward the Tier-2 false-merge rate watch metric.

### `eval-inbox-labels.jsonl` — DEPRECATED 2026-05-27

Retired alongside the `/eval-inbox-judgments` chat-based daily flow. Last snapshot archived at `archive/2026-05-pre-html-pivot/eval-inbox-labels.jsonl`. The new labeling surface is the HTML viewer at `evals/viewer.html` reading from `evals/inbox-to-task/traces/eval-inbox-week<N>.jsonl` and exporting `eval-inbox-labeled-week<N>-<YYYY-MM-DD>.csv`. See the trace input/output schemas at the end of this file.

### `eval-persona-weekly-self-check.jsonl` (lives on Kavi, runtime appends; mirrored to Megha's Mac via the same fetch pattern as inbox judgments)

One JSON object per line. One record per Friday self-check event. Append-only.

```json
{
  "ts": "2026-05-08T21:00:00Z",
  "capability": "kavi-persona",
  "question": "Did this week feel like Kavi saved you cognitive load, or added to it?",
  "reply": "Saved — I noticed I wasn't checking my inbox before bed.",
  "rating": "saved",
  "reply_received": true,
  "reply_latency_sec": 1843,
  "usage": {"input_tokens": 1245, "output_tokens": 87, "cache_read_input_tokens": 0}
}
```

- `ts`: timestamp of the Friday 2pm send (Pacific local rendered as UTC).
- `question`: actual LLM-composed question Kavi sent (free-form, persona-driven; not templated).
- `reply`: Megha's free-form text reply, verbatim.
- `rating`: LLM-classified bucket from the reply: `saved | added | neutral | unclear`. Reply parsing is full Sonnet judgment per persona spec; no rigid syntax.
- `reply_received`: false if Megha didn't reply within the configured window (default: 24 hours from send). When false, `reply` is null and `rating` is `"no_reply"`.
- `reply_latency_sec`: elapsed seconds from send to reply. Null when `reply_received` is false.

### `eval-persona-inbound.jsonl` (lives on Kavi, runtime appends; one row per inbound iMessage)

One JSON object per line. One record per inbound iMessage from Megha (or anyone Kavi listens to). Pairs with `eval-persona-outbound-judgments.jsonl` via `triggered_by` linkage so the daily eval surface can render context-aware blocks (inbound text → Kavi's reply). Append-only.

```json
{
  "inbound_id": "i_2026-05-04T22:06:30Z_a8b3f291",
  "ts": "2026-05-04T22:06:30Z",
  "capability": "kavi-persona",
  "source": "imessage",
  "sender": "+15555550101",
  "text": "mark the Dana Park task done please",
  "char_count": 35,
  "context": {}
}
```

- `inbound_id`: stable id used by outbound rows in `triggered_by`. Format: `i_<utc_iso>_<8hex>`.
- `source`: `"imessage"` for v1; reserved for future surfaces (email replies, web).
- `sender`: BlueBubbles handle/address if available; null for some webhook shapes.
- `text`: verbatim inbound text after `.strip()`.
- `char_count`: `len(text)` post-strip.

### `eval-persona-outbound-judgments.jsonl` — `triggered_by` field

The outbound JSONL gains a `triggered_by` field. Value:
- `<inbound_id>` of the inbound iMessage that caused this outbound (set automatically via `kavi_runtime.inbound_log.current_inbound_id` contextvar).
- `null` for cron-driven outbound (`periodic_summary`, `weekly_self_check`, `alert_*`) and email-driven outbound (low-confidence Q&A questions caused by an email arrival, not an iMessage).

The eval surface renders inbound text inline above each outbound when `triggered_by` is set; otherwise it shows `(cron-driven / email-driven)`.

### `eval-persona-day-mute-events.jsonl` (lives on Kavi, runtime appends; mirrored to Megha's Mac via the same fetch pattern as inbox judgments)

One JSON object per line. One record per `quiet_until` set event. Append-only. Logs raw — duration thresholding is a read-time decision, not a write-time decision (rule: log-raw-decide-at-read-time, 2026-05-04).

```json
{
  "ts": "2026-05-06T11:32:18Z",
  "capability": "kavi-persona",
  "command_text": "be quiet for the rest of the day",
  "quiet_until": "2026-05-07T07:00:00-07:00",
  "duration_sec": 70062,
  "trigger": "explicit_steering"
}
```

- `ts`: timestamp the command was received.
- `command_text`: Megha's verbatim iMessage that set the quiet state. Null if set internally (e.g., guardrail-driven pause; `trigger` distinguishes).
- `quiet_until`: ISO-8601 timestamp the runtime resumes outbound at. Pacific offset preserved for human readability.
- `duration_sec`: integer seconds from `ts` to `quiet_until`.
- `trigger`: `explicit_steering` (Megha said it) or `guardrail` (e.g., spend cap pause). Day-mute frequency metric counts `explicit_steering` only — guardrail pauses are not annoyance signals.

### `eval-persona-close-suggestions.jsonl` (lives on Kavi, runtime appends; mirrored to Megha's Mac via the same fetch pattern as inbox judgments)

One JSON object per line. One record per close-suggestion JUDGMENT — every (open task, newest sent reply) pair the LLM judge evaluated for the evening rollup, suggest and no-suggest verdicts alike (logging only the yeses would make precision unmeasurable against the judge's real behavior). Append-only; the judged-pair cache in `close_suggestions.json` guarantees at most one row per (task_id, reply_id) pair. Added 2026-06-10 with the evening suggest-to-close feature.

```json
{
  "ts": "2026-06-10T21:01:42Z",
  "capability": "kavi-persona",
  "kind": "close_suggestion_judgment",
  "task_id": "AQMkAD...",
  "task_title": "MJ Pay Boonli invoice",
  "reply_id": "AAMkAD...",
  "reply_sent_at": "2026-06-10T14:22:09Z",
  "suggest_close": true,
  "confidence": "high",
  "reason": "your reply says it was paid this morning",
  "usage": {"input_tokens": 912, "output_tokens": 38, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
}
```

- `task_id` / `task_title`: the open MS To Do task the judgment is about (title carries the owner prefix verbatim).
- `reply_id` / `reply_sent_at`: the sent-mail message the judgment is grounded in. `(task_id, reply_id)` is the judged-once cache key.
- `suggest_close`, `confidence`, `reason`: the judge's verdict as returned (before the runtime's high-confidence surfacing filter — a `suggest_close=true, confidence="medium"` row was judged yes but never surfaced).
- `usage`: token usage for the judgment call. Failed calls (API/parse error) write no row at all — they are not verdicts, are not cached, and re-judge on the next run.
- Labels (was the task actually done?) are weekly open codes in the HTML-viewer flow, not fields in this file. Feeds **Close-suggestion precision** (Metric formulas above).

One JSON object per line. Multiple rows per coordination session, joined by `session_id`. Append-only. Each row captures one state transition in a coordination (ack, addressee-reach, addressee-reply, branch-decision, outcome-report).

```json
{
  "decision_id": "c_2026-05-06T03:42:18Z_a8b3f291",
  "session_id": "s_2026-05-06T03:41:55Z_rosa_cash",
  "ts": "2026-05-06T03:42:18Z",
  "capability": "kavi-coordinates",
  "phase": "addressee_reach",
  "requester_handle": "+15555550101",
  "addressee_handle": "+15555550102",
  "inbound_text": "Hey Kavi, we need cash for Rosa tomorrow morning, can you check with Max if he already has it?",
  "ack_text": "Got it, checking with Max now. I'll let you know what he says.",
  "ack_latency_sec": 8,
  "addressee_message_text": "Hey Max, Megha mentioned cash for Rosa tomorrow morning. Do you have it on hand, or should one of you withdraw?",
  "addressee_message_attribution": true,
  "addressee_reply_text": null,
  "addressee_reply_latency_sec": null,
  "branch": null,
  "task_created": false,
  "task_id": null,
  "outcome_report_text": null,
  "outcome_report_latency_sec": null,
  "closed": false,
  "usage": {"input_tokens": 1842, "output_tokens": 92, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 14203}
}
```

- `decision_id`: stable id for this row. Format: `c_<utc_iso>_<8hex>`.
- `session_id`: stable id linking all rows for one coordination. Format: `s_<utc_iso>_<short_label>`.
- `phase`: one of `ack | addressee_reach | addressee_reply | branch_decision | outcome_report | follow_up_sent | escalated_to_requester | session_failed`. Each phase appends a new row.
- `requester_handle`, `addressee_handle`: BlueBubbles phone-number handles from `household.md`. Verbatim, not hashed.
- `inbound_text`: requester's original ask. Captured once per session on the `ack` phase row; null on subsequent rows.
- `ack_text`, `ack_latency_sec`: Kavi's ack message + seconds from inbound to ack-sent. Captured on `ack` phase only.
- `addressee_message_text`, `addressee_message_attribution`: Kavi's message to addressee + boolean for whether attribution was applied. Captured on `addressee_reach` phase.
- `addressee_reply_text`, `addressee_reply_latency_sec`: addressee's reply + seconds from Kavi's outbound to reply landing. Captured on `addressee_reply` phase; null elsewhere.
- `branch`: one of `4a_yes_have_it | 4b_will_grab | 4c_ambiguous | 4d_no_reply` per the canonical Rosa cash few-shot. Captured on `branch_decision` phase.
- `task_created`, `task_id`: populated when branch == `4b_will_grab` and the create-task verb fires.
- `outcome_report_text`, `outcome_report_latency_sec`: Kavi's outcome message back to requester + seconds from resolution event to outcome-sent. Captured on `outcome_report` phase.
- `closed`: true on the terminal row of a session (outcome reported OR escalated).
- `usage`: token usage for the LLM call that produced this phase's outbound (composer or parser, depending on phase). Null for phases without an LLM call (e.g., `addressee_reply` is a pure inbound capture).

### `eval-coordinates-labels.jsonl` (lives on Megha's Mac, slash command appends)

One JSON object per line. One record per labeling event. Joined to judgments by `session_id` (NOT `decision_id` — labels grade the whole session, not individual phases). Append-only; if a session is re-labeled, a new row is appended. Most-recent annotation per `session_id` wins.

```json
{
  "session_id": "s_2026-05-06T03:41:55Z_rosa_cash",
  "close_rate_pass": true,
  "follow_up_window_correct": null,
  "attribution_correct": true,
  "false_positive_task": false,
  "internal_ops_leakage": false,
  "notes": "Clean cash coordination; Max replied within 4 minutes so no follow-up was needed (window correctness N/A).",
  "annotated_at": "2026-05-06T15:08:22Z",
  "annotator": "megha"
}
```

- `close_rate_pass`: binary, did Kavi close the loop without the requester re-pinging? Required.
- `follow_up_window_correct`: binary 0/1 OR `null`. Null when no follow-up was needed (addressee replied within Kavi's first window).
- `attribution_correct`: binary, did the persona attribute or omit correctly per principle 1? Required when an addressee message was sent.
- `false_positive_task`: binary, was a created task unnecessary? Required when `task_created == true` in the corresponding judgment row; null otherwise.
- `internal_ops_leakage`: binary, did the outcome report narrate internal mechanics? Required when an outcome report was sent.
- `notes`: optional free-form rationale, especially on the disagreement cases.
- `annotator`: always `"megha"` for v1.

### `eval-<scope>-week<N>.jsonl` (HTML viewer input)

Lives under `evals/<capability-slug>/traces/`. Built weekly by `scripts/build_eval_traces.py` (forthcoming) from the canonical judgments + inbound JSONLs. One JSON object per line; one row per unit-to-be-labeled — a session for kavi-persona (`row_id` = `session_id`), a decision for inbox-to-task (`row_id` = `decision_id`).

Shared fields across scopes:

- `row_id`: stable id (session_id for persona, decision_id for inbox).
- `ts`: ISO 8601 timestamp of the row's anchor event (first message in a session; decision time for inbox).
- `capability`: capability slug, e.g. `kavi-persona` or `inbox-to-task`.
- `readable_text`: flat, human-readable rendering of the full row, formatted for the HTML viewer's per-row pane (no JSON nesting — already laid out chronologically with speaker labels for sessions, or sender/subject/decision blocks for inbox).

Per-scope fields are documented in the relevant `capabilities/<slug>.md` Eval-infrastructure section. Append-only is not required (each weekly trace file is freshly built; the source-of-truth JSONLs stay append-only).

### `eval-<scope>-labeled-week<N>-<YYYY-MM-DD>.csv` (HTML viewer output)

Lives under `evals/<capability-slug>/traces/`. Exported from the viewer after Megha finishes open coding. One row per labeled unit (subset of the week JSONL: only rows she actually annotated). Columns:

- Scope-specific id column (`session_id` for persona, `decision_id` for inbox).
- `ts`: anchor timestamp, copied from the input JSONL.
- `readable_text` fields: copied through so the CSV is self-contained for axial coding in Sheets without re-joining to the JSONL.
- `open_code`: free-form rationale Megha typed in the viewer (the upstream root cause of any failure mode, citing specific messages inline).
- `axial_code`: cluster label assigned later, during the axial coding pass in Sheets (empty on export; filled in Sheets).

### Authoring rules

- **Append-only.** Never edit prior rows in the judgments file. Runtime appends only.
- **New fields = additive only.** Older rows stay valid; readers tolerate missing fields as "not recorded."
- **Integer counts; float for money/latency fractions.** `latency_first_action_sec` is an integer.

## Regression alerts

Computed at weekly ritual time, reading `eval-inbox-judgments.jsonl` joined with the latest labeled CSV under `evals/inbox-to-task/traces/`. **Threshold values come from the relevant `capabilities/<name>.md`** — when a capability doc updates a threshold, this list updates too.

| Condition | Alert | Source threshold |
| --- | --- | --- |
| Rolling precision below threshold for 3+ consecutive days | ⚠️ PRECISION DRIFT | `capabilities/inbox-to-task.md` |
| Rolling owner accuracy below threshold for 3+ consecutive days | ⚠️ OWNER DRIFT | `capabilities/inbox-to-task.md` |
| Any day with dedup correctness below threshold | 🛑 HARD BUG — halt, do not write | `capabilities/inbox-to-task.md` |
| Rolling latency or token cost above budget for 3+ days | ⚠️ ECONOMICS | `capabilities/inbox-to-task.md` |
| Spend cap hit for current month | 🛑 PAUSE | `capabilities/realtime-kavi.md` |
| Webhook flood threshold exceeded | ⚠️ THROTTLED | `capabilities/realtime-kavi.md` |

The 🛑 conditions halt or pause the runtime. The ⚠️ conditions surface as warnings during weekly ritual; they are signals, not gates.

## Command output specs

### HTML viewer (L1 eval surface)

The L1 eval surface for both inbox-to-task and kavi-persona is the shared HTML viewer at `evals/viewer.html`. Loads a `traces/eval-<scope>-week<N>.jsonl` file, paginates one labelable unit per page (session for persona, decision for inbox), accepts free-form open codes per row, saves to localStorage, exports a `traces/eval-<scope>-labeled-week<N>-<YYYY-MM-DD>.csv` for axial coding in Sheets.

Daily chat-based slash commands `/eval-inbox-judgments`, `/eval-persona-outbound`, `/eval-persona-week` retired 2026-05-27.

### `/metrics` (deferred)

Will read `eval-inbox-judgments.jsonl` joined with the latest labeled CSV under `evals/inbox-to-task/traces/`, print a rolling dashboard to the terminal:
- **Last 7 days:** precision, owner accuracy, recall, dedup correctness, median latency, total cost
- **Last 30 days:** same metrics + week-over-week delta
- **Confidence calibration:** precision split by `high` vs `low` confidence
- **Flagged metrics:** any below threshold for 3+ days shown at top with ⚠️
- All thresholds pulled from `capabilities/<name>.md` (not duplicated here)

All reads come from the JSONL files. All reports are *views* — never edit the JSONL to "fix" a report.
