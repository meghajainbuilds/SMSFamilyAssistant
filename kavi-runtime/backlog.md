# kavi-runtime engineering backlog

Engineering and infrastructure work that is scoped but not yet shipped. Capability-scoped open questions live in their `capabilities/<name>.md` Open questions section; this file is for runtime/infra work that doesn't belong to a single capability.

Each entry: short title, what's broken or at risk (PM lens), engineering sketch, status, and a verification check that proves whether it's still open.

---

## Channel heartbeat synthetic probe replaced with passive observation

**Status:** CLOSED 2026-06-04. The 2026-06-03 design sent a synthetic one-character ping to each household recipient and polled BlueBubbles' chat history for that ping — exactly the chat-mirror false-negative class the receipt-as-truth verify rewrite (SHA `af66b47`) had just removed. The 08:00 PT cron on 2026-06-04 false-fired two "channel degrading" emails to Megha: one for her own number (Kavi was actively texting her) and one for Max's number (Max had texted Kavi back the same morning). The chat-history poll for Megha returned 200 OK but didn't see the ping in the recent-50 window, AND the predicate `last_rt is None or (now - last_rt) > 24h` treated first-run identically to 24h-of-degradation. The fix is structural: the synthetic probe is deleted (no `bb.send_message`, no `bb.fetch_recent_messages`, no `bb.chat_guid_for`, no `time.sleep`). Two write-through hooks now track real traffic: `record_outbound_receipt` fires from `runtime/send_imessage.py` on every `verified=True` + message_guid receipt for a household recipient; `record_inbound` fires from `runtime/imessage_dispatch.py` on every webhook arrival from a household sender. The daily 08:00 PT cron is a pure read: healthy when either signal is within 24h. 72h grace period on first appearance prevents cold-start false-fires. Existing dedupe semantics preserved. New architectural test `tests/test_heartbeat_passive_only.py` AST-walks the module and asserts zero `bluebubbles_client` calls, zero sleeps, zero forbidden imports — regression-verified by temporarily re-injecting a `bb.send_message` call and confirming the test fails. Schema migration drops `last_ping_temp_guid`, `last_ping_at`, `last_round_trip_at`; preserves `last_alert_sent_at`; adds `recipient_added_at`, `last_outbound_receipt_at`, `last_inbound_at`.

---

## Phase 4 physical move — SHIPPED 2026-06-02 (`e159719`)

**Status:** SHIPPED.

Phase 4 is complete. Every capability lives in its own `capabilities/<name>/`
directory with real function bodies (no re-export shims). Final monolith
sizes:
- `handlers.py`: 3,366 -> 208 lines (cap: 300)
- `claude_client.py`: 1,837 -> 391 lines (cap: 400)
- `structural_checks.py`: 369 lines (cap: 400)
- `synthetic_compose.py`: 38 lines (cap: 50)

Architectural tests (`test_no_capability_code_in_runtime.py`):
- Line caps tightened (3500 -> 300, 1900 -> 400).
- `FORBIDDEN_DEF_PATTERNS_IN_HANDLERS` + `FORBIDDEN_DEF_PATTERNS_IN_CLAUDE_CLIENT`
  text-pattern gates added.
- `KNOWN_PASSTHROUGH_FILES = set()` (empty).

All 587 tests pass. Deploy verify-passed on Kavi at SHA `e159719`.

Linked: Anita bug fix (2026-06-01 -> 2026-06-02) shipped in the same
PR set. See handoffs/handoff-2026-06-02-phase4-shipped.md.

---

## External dead-man's-switch monitoring

**What's broken or at risk:** Kavi can fail completely (Mac frozen, network down, runtime crashed, balance zero) and Megha won't know unless she's actively in the HomeOS Claude Code session running `/healthz`. A 7am digest that didn't fire looks identical to "no email worth surfacing today." 2026-05-02 incident: 17 hours of dropped email before noticed.

**Who feels it and when:** Megha, every time she's not in the project (i.e. most days). Trust collapses silently.

**Why now (Doshi):** Leverage. `/healthz` (in flight) covers in-session checks, but a system can't reliably signal its own death. The watchdog must be external.

**Engineering sketch:**
- Sign up for healthchecks.io free tier (10 checks free; supports calendar-style "expected at 7am ±15min" for the morning-digest cadence).
- Kavi's runtime pings the healthchecks.io URL every N minutes (cron in `scheduler.py`).
- Alert email to `megha@example.com` when pings stop. Healthchecks.io handles the email send.
- Optional follow-up: a separate calendar-mode check tied to the 7am digest event, so a missed digest pages even if the runtime is otherwise alive.

**Verify with:** healthchecks.io account exists, ping URL is configured in `kavi-runtime/config.yaml`, and at least one alert email landed in `megha@example.com` (intentionally trip it once during commissioning).

**Status:** queued 2026-05-04. Deferred until after Stage 4 measurement implementation lands.

---

## Steering implementation (blocks day-mute event logging)

**What's broken or at risk:** Steering is spec'd in `capabilities/kavi-persona.md` (mid-thread redirects, time-bounded quiet, undo) but unbuilt in production code. As a result, `quiet_until` state doesn't exist, and the Stage 4 day-mute-events leading-indicator metric has no event to log.

**Who feels it and when:** Megha, every time she wants to tell Kavi to be quiet for the day and the runtime keeps sending. Also: day-mute frequency is the one objective signal of "Kavi is annoying"; without it, only the weekly self-check (subjective) covers that surface.

**Why now (Doshi):** Neutral. The Friday self-check already covers the direct signal. Day-mute is a leading indicator, not a load-bearing measurement on its own. Build steering when Megha actually wants to tell Kavi to be quiet, OR when we have enough self-check data to want the leading indicator alongside.

**Engineering sketch:**
- LLM-judged steering classifier on every inbound iMessage from Megha (cheap, ~$0.001/call): {redirect | quiet | undo | none}.
- New runtime state field `quiet_until` (ISO timestamp) in `imessage-state.json`.
- Scheduler/handlers check `quiet_until` before sending non-emergency outbound.
- When steering classifier returns `quiet`: parse duration ("for the day" / "for 3 hours" / no qualifier → rest of day), set `quiet_until`, append a row to `eval-persona-day-mute-events.jsonl` (schema in `evals/definitions.md`).
- Auto-resume when `now >= quiet_until`.
- Path already wired in `config.yaml`: `eval_persona_day_mute_events_jsonl`.

**Verify with:** Send Kavi an iMessage saying "be quiet for an hour"; runtime sets `quiet_until` ~1h ahead; a row appears in `eval-persona-day-mute-events.jsonl` with `trigger: explicit_steering`. Runtime sends nothing during the window.

**Status:** queued 2026-05-04. Discovered when wiring Stage 4 — the JSONL schema and config path landed, but no event source exists yet.

---

## Cross-day durable facts (G-C3)

**What's broken or at risk:** Persona spec says when Megha tells Kavi a durable fact ("Max is out of town this week, route everything to me"), Kavi persists it as a learned pattern and applies it across days until expired. Today nothing persists across messages — every conversation starts blank, conversation-state memory only stretches as far as the recent outbound buffer (5 entries).

**Who feels it and when:** Megha, every time she has to repeat context she already gave Kavi. ("I told you Monday Max was out — why are you still routing his stuff to him?")

**Why now (Doshi):** Neutral. G-C2 simple thread memory (last 5 outbound) is enough for in-session conversation. G-C3 only matters once we have multi-day threads with durable preferences. Build when first violation surfaces.

**Engineering sketch:**
- New JSONL `learned_facts.jsonl` on Kavi: `{ts, fact, source_inbound_text, expires_at?, applied: false}`.
- Conversation handler emits a fact-extraction call when Megha says something durable; Sonnet judges {is_durable_fact: bool, fact_text, expires_at?}.
- All persona system prompts include the active learned facts as cached context (similar to household.md inclusion).
- Megha can override / expire ("forget about Max being away" → set expires_at to now).

**Verify with:** Tell Kavi "Max is out of town this week" → Kavi acks + writes to learned_facts.jsonl. Send a Max-related email next day → Kavi routes to Megha not Max, citing the learned fact.

**Status:** queued 2026-05-04. Spec'd in capabilities/kavi-persona.md G-C3.

---

## Steering implementation expansion (G-S1, G-S3, G-S4)

**What's broken or at risk:** G-S2 (quiet_until) is already in this backlog; the persona spec also defines two more steering classes that are equally unbuilt:
- G-S1 redirects mid-thread ("shorter", "stop", "reword")
- G-S3 undo patterns (delete-task, undo-last-action, learn-skip)
- G-S4 ambiguity-ask: when steering intent is unclear, Kavi asks rather than guesses

**Who feels it and when:** Megha, every time she wants to tell Kavi to be shorter and Kavi keeps sending the same long format. Or when she wants to undo a task and has to do it manually in MS To Do.

**Why now (Doshi):** Neutral. Workarounds exist (just delete in MS To Do; Megha learns to live with the format). Build when frustration hits.

**Engineering sketch:**
- Single LLM-judged "steering classifier" on every inbound from Megha (~$0.001/call): {redirect | quiet | undo | none}. Routes to handlers per intent.
- Redirects modify next outbound's prompt with a "shorten by 30%" or "no emoji" instruction passed to compose_*.
- Undo: parse target ("the Boonli task", "that last thing"), confirm match, mutate MS To Do or roll back state.
- Ambiguity: if classifier confidence is low, Kavi asks one clarifying question rather than acting.

**Verify with:** Send "shorter" after a long Kavi message → next message ≤80 chars. Send "delete the Boonli task" → MS To Do task gone. Send something ambiguous like "no" → Kavi asks "no to what?"

**Status:** queued 2026-05-04. Spec'd in capabilities/kavi-persona.md G-S1/S3/S4.

---

## Pre-mortem accuracy + decision-latency manual ritual (G-RF2, G-RF3)

**What's broken or at risk:** Two role-frame metrics in the spec require Megha's manual reconcile during weekly ritual: (1) re-checking changelog "How I'll know I was wrong" predictions against actual outcomes 4 weeks later, (2) self-rating one AI-adjacent decision per week as fast/hedged/regret. Today nothing prompts her to do this.

**Who feels it and when:** Megha, weekly. Without the prompt, the metrics stay aspirational and the reflection muscle (Doshi five judgment muscles) doesn't get worked.

**Why now (Doshi):** Neutral. The weekly HTML-viewer open-coding pass is the natural surface to hang both prompts onto. What's missing: tooling to capture decision-latency replies into a JSONL automatically and reconcile pre-mortem predictions against changelog "How I'll know I was wrong" lines.

**Engineering sketch:**
- `evals/kavi-persona/eval-persona-decision-latency.jsonl` — append-only, one row per week per decision rated. The weekly ritual prompts Megha; she replies; the append happens at ritual time.
- Pre-mortem reconcile: walk `capabilities/*.md` Changelog entries within last 4 weeks, surface each "How I'll know I was wrong" line, ask Megha "did this come true?" Yes/no per entry, append to `eval-persona-premortem-reconcile.jsonl`.

**Verify with:** During the weekly ritual, the prompts surface and a decision-latency entry plus a pre-mortem reconcile entry land for at least one changelog from the past 4 weeks.

**Status:** queued 2026-05-04. Re-scoped 2026-05-27 to hang on the HTML-viewer weekly ritual instead of the retired `/eval-persona-week` skill.

---

## Action layer for "mark task done" + similar imperative requests

**What's broken or at risk:** Kavi says "On it" / "Done" but doesn't actually call the MS To Do API. AV (Action verified) failed on 3 of 4 labeled rows in the 2026-05-05 eval session. Trust collapses on every false-completion claim.

**Who feels it and when:** Megha, every time she asks Kavi to mark tasks done. Currently has to manually mark them in MS To Do despite Kavi claiming completion. The claim itself becomes a trust deduction.

**Why now (Doshi):** Leverage. Every "On it" without action is a trust deduction. The action layer is what turns Kavi from a chat partner into a Chief of Staff. Surfaced by today's eval session as the dominant failure pattern.

**Engineering sketch (5 sub-pieces, staged delivery):**
1. Action intent classifier (new skill: `kavi-runtime/skills/action_intent_classifier.md`) — LLM parses "Mark X done" / "Cancel Y" / "Add Z" into structured actions `[{action_type, target_text, confidence}]`. Multi-action support per inbound.
2. Task matcher — fuzzy-match the natural-language target to MS To Do task IDs in the McMullen-Jain Shared list. High-confidence → execute. Low-confidence → ask Q&A before acting.
3. Action executor — MS Graph API calls (`PATCH /todo/lists/{listId}/tasks/{taskId}` with `status=completed`). Failure handling: log + iMessage Megha if API errors.
4. Reply composer — replaces "On it" pre-action pattern with post-action: "Done. Marked 5 of 7; couldn't find: Kelly meeting, RR Newsletter." If execution >5s, two-stage reply: ack first, result when done.
5. Outbound row instrumentation — `context` field captures `actions_executed: [{task_id, action, result}]` so the eval surface auto-fills AV at label time.

**Recommended staging:**
- **Stage 1 (smallest meaningful slice):** pieces #1 + #3 + #4. Single-action only. Exact-title task matching. Lets Kavi actually mark explicit-by-title tasks done. Validates the architecture.
- **Stage 2:** piece #2 (fuzzy matching) + multi-action. Handles the "mark these 7 done" case from row #3.
- **Stage 3:** piece #5 (instrumentation) + AV auto-fill in eval surface. Closes the eval loop.

**Verify with:** `ssh kavi@100.64.0.10 'grep "actions_executed" /Users/kavi/HomeOS/evals/kavi-persona/eval-persona-outbound-judgments.jsonl | tail -5'` returns rows with non-empty actions_executed. AV pass-rate in the weekly HTML-viewer open coding climbs above 90% on action-implying rows over 7 days.

**How I'll know I was wrong:** Kavi shipping Stage 1 doesn't move AV pass-rate (means action layer isn't being triggered, or the task matching is too strict); OR Kavi starts incorrectly marking tasks done that you didn't ask about (false-positive action execution); OR latency on action-implying replies climbs past 10s (action call blocking the reply path too long).

**Status:** queued 2026-05-05. Driver: 2026-05-05 eval surface labeling session — rows #1, #2, #3 all AV=n; Megha had to manually mark tasks done after Kavi claimed completion.

---

## Runtime heartbeat: drop the 60-min iMessage alert, replace with pull-based status

**What's broken or at risk:** Every 60 minutes Kavi sends Megha an iMessage saying it hasn't seen any activity recently. The eval rubric flagged this 11 times across 84 labeled sessions; Megha's note on every one is the same: "we need to work on figuring out a better solution for monitoring Kavi's runtime." Over the last 7 days, 75 of 89 persona-eval rows are these alerts. It is not a persona failure; it is an ops-channel mismatch. Megha's iMessage thread with Kavi is action-oriented; interrupting it with system-health pings turns Kavi from "the role getting things done" into "the system reporting on itself."

**Who feels it and when:** Megha, every hour the runtime is alive and has no inbound to act on (most hours overnight, working hours when nothing is happening). Pattern: alert fires when nothing has happened. Lack of inbound email is the most common cause and not actionable. The alert is a false-positive generator by design.

**Why now (Doshi):** Leverage. Removing this path drops the persona failure rate from roughly 76% to 63% mechanically and clears 80% of the eval-surface labeling backlog so the surface starts grading actually-LLM-composed outbound. Engineering effort is small. The persona spec rewrite (2026-05-26 dispatch) already added the out-of-scope bullet ("Kavi never sends ops-monitoring messages over iMessage"), so the spec is ready ahead of the runtime change.

**Engineering sketch:**

The shape is inverted alerting: stop pushing health into Megha's iMessage; let Megha pull status when she wants it; rely on the external watchdog to page only on real failure.

1. **Remove the periodic "no activity" alert path.** The current `alert_fallback` outbound that fires on inactivity goes away entirely. Silence is not failure. The cron job, the helper that composes the alert, and the routing into the outbound pipeline all delete.
2. **Add a `/status` HTTP endpoint to kavi-runtime.** Single-page HTML view, served via the existing Tailscale Funnel URL. Fields: last inbound timestamp (and from whom), last outbound timestamp (and to whom), last cron tick, last successful LLM call, last successful Graph call, today's Anthropic spend, this-month's Anthropic spend, spend-cap state. Megha bookmarks on her phone home screen; one tap to know if Kavi is alive.
3. **Reuse the external dead-man's-switch entry above for real-failure paging.** That entry already covers healthchecks.io for true silent failures. No new push channel needed.

**Decisions Megha needs to make before dispatch:**
- Status page format: plain HTML (5-minute build) versus a richer dashboard (more work, more polish). Recommend plain HTML for v0; richer later if she opens it daily.
- Auth on `/status`: open to anyone with the Tailscale URL (Megha's phone is on Tailscale; URL is already a non-guessable secret), or add a token check. Recommend open for v0.
- Out of scope for this work: replacing the inactivity alert with a smarter alert (e.g., "no inbound for over 24h AND a webhook arrived but failed"). The dead-man's-switch already covers that shape externally; double-instrumenting is wasted effort.

**Verify with:** `grep -n alert_fallback kavi-runtime/kavi_runtime/handlers.py` returns no matches in the inactivity-check code path. `curl https://kavi-mac.your-tailnet.example/status` returns 200 and renders the eight fields. New `eval-persona-outbound-judgments.jsonl` rows over 7 days contain zero `kind: alert_fallback` entries.

**How I'll know I was wrong:** Megha never opens the `/status` page in a 4-week window (means Megha didn't actually want pull-based status; she wanted a smarter push). OR a real Kavi outage happens during the 4-week window and Megha doesn't notice for more than 2 hours (means the external dead-man's-switch wasn't actually sufficient and the inactivity alert was doing more work than I gave it credit for). OR the persona failure rate doesn't drop materially after removal (means alert_fallback wasn't the bulk of the noise after all and something else is generating false alerts).

**Status:** queued 2026-05-26. Driver: open-coding rubric session 2026-05-12 surfaced the 11-instance pattern; persona spec rewrite 2026-05-26 added the out-of-scope bullet that this runtime change implements. **SHIPPED 2026-05-27** in commit b03aed5; `/status` live at `https://kavi-mac.your-tailnet.example/status`.

---

## Re-auth alert: detect AADSTS70000 and ping Megha with the exact command

**What's broken or at risk:** Microsoft personal-account refresh tokens (outlook.com, hotmail.com) expire on a rolling window shorter than work/school M365 accounts. When the token expires, the `subscription_renewal_check_all_accounts` cron fails every 30 minutes with WARNING logs but does not alert anyone. On 2026-05-14 Megha's refresh token expired silently and was not noticed until 2026-05-26, twelve days later. During those twelve days, Kavi processed zero new email; the gap was invisible from the outside.

**Who feels it and when:** Megha, every time a refresh token expires and goes unnoticed. The cost compounds with the silence: each day without alerting is a day of missed email-to-task processing.

**Why now (Doshi):** Leverage. Removes silent-failure risk for the highest-impact runtime dependency. Engineering is small. Persona spec amended 2026-05-27 to allow actionable ops alerts over iMessage subject to the constraints below.

**Engineering sketch:**

Detection is easy; the design discipline is "fire once, do not spam."

1. **State tracking.** Add a per-account counter `subscription_renewal_failures` in `state.json`. Increment on each failed renewal. Reset to zero on success.
2. **Detection trigger.** When the counter reaches 3 consecutive failures (90 minutes of confirmed-broken), inspect the most recent failure reason. If the MSAL error is AADSTS70000 with suberror `bad_token` or `invalid_grant`, classify as "re-auth needed." Other failures (network timeout, Graph 5xx) classify as "transient" and do not alert.
3. **Alert delivery.** Send ONE iMessage to Megha. Deterministic text (NOT LLM-composed; the persona spec exception is conditional on this). Template:
   `Re-auth needed for <account>. Run on Kavi's Mac: ssh kavi@100.64.0.10 then "cd kavi-runtime && .venv/bin/python -m kavi_runtime.add_account <account>". I'll let you know when the subscription is back.`
4. **Suppression.** After alerting, set `alert_sent_at` timestamp in state. Do not send another re-auth alert for the same account until the counter resets by a successful renewal. `/status` page already surfaces the broken state continuously.
5. **Confirmation.** On the next successful renewal, send ONE follow-up iMessage: `Re-auth confirmed; subscription back. <N> new email decisions queued.`

**Verify with:** Simulate by writing an invalid blob to `/Users/kavi/.config/kavi/tokens/<account>.json`. Observe the renewal cron firing 3 times over 90 minutes, then a single iMessage landing with the re-auth command. Confirm no further alerts until a manual add_account run. Confirm a confirmation iMessage after success.

**How I'll know I was wrong:** Re-auth alerts fire too frequently (means 3-failure threshold or suppression is wrong). OR a real re-auth need does not fire an alert within 2 hours (means detection logic missed AADSTS70000). OR the confirmation iMessage feels like noise (means the follow-up is wrong shape).

**Status:** queued 2026-05-27. Driver: 2026-05-14 silent breakage incident; 12-day gap before Megha noticed; persona spec amended 2026-05-27 to allow actionable ops alerts.

---

## Email backfill: 2026-05-14 to 2026-05-26 (12-day catchup)

**What's broken or at risk:** Megha's email-to-task pipeline was offline from 2026-05-14 to 2026-05-26 due to the expired Graph subscription. The emails are still in her Outlook inbox but never reached the inbox-to-task classifier. MS To Do does not reflect 12 days of actual workload she has.

**Who feels it and when:** Megha right now. Estimated 200-300 emails in the window, of which 30-50 are likely actionable. Without backfill, those tasks live only in Outlook and add to her cognitive load.

**Why now (Doshi):** Leverage. One-time catch-up. Sonnet cost is small (~$2-3 at full volume).

**Engineering sketch:**

1. **Date-range fetch.** Add or use `graph_client.list_messages_in_range(start, end, account)` that hits `/me/messages` with `$filter=receivedDateTime ge <start> and receivedDateTime le <end>` and paginates as needed.
2. **iMessage suppression.** Thread a `backfill_mode=True` flag through `email_arrived` and downstream into the iMessage outbound path. In backfill mode, the runtime creates tasks in MS To Do but does NOT fire per-task iMessage notifications. Prevents 30-50 individual pings flooding Megha.
3. **Per-email classification.** For each fetched email, call the existing `email_arrived` handler. Reuses LLM classifier, dedup, task creation. No new pipeline.
4. **Dedup safety.** The existing semantic-dedup check (against last ~25 active tasks) prevents re-creation of tasks Megha already has. If a task pre-dates the May 14 outage and a new email about it arrives in the backfill window, dedup merges correctly.
5. **Cost gate.** Hard cap: backfill aborts if total Sonnet spend exceeds $10 (sanity gate, above the $2-3 estimate).
6. **Summary report.** On completion, send ONE iMessage: `Backfill complete. <N> emails processed, <M> tasks created, <K> skipped. Open MS To Do to review.`

**Verify with:** `ssh kavi@100.64.0.10 "wc -l /Users/kavi/HomeOS/evals/inbox-to-task/eval-inbox-judgments.jsonl"` row count climbs by the processed-email count. New tasks appear in the McMullen-Jain Shared list. The summary iMessage lands.

**How I'll know I was wrong:** Backfill produces >5% false-positive tasks (classifier confidence threshold needs raising for backfill mode). OR dedup misses a category and creates duplicates. OR the iMessage summary feels like noise (means we should silently complete and let Megha discover via MS To Do).

**Status:** queued 2026-05-27. Driver: 2026-05-14 to 2026-05-26 subscription outage, 12 days of unprocessed email. **SHIPPED 2026-05-27** in commit a1ae2ac. Real-run results: 1334 emails processed, 72 tasks created, 40 dedup hits, 1218 skipped (newsletters and marketing), 4 content-scanner blocks on financial emails. Spend $26.51 (under $30 cap). Wall time 2h 49m. Summary iMessage sent. The 4 blocked emails carry account-number or credit-card patterns; the security scanner correctly refused to write those to MS To Do; the underlying emails sit in Outlook unchanged for manual review.

---

## Secrets.json fallback staleness: stale blobs hijack the token-cache load path

**What's broken or at risk:** The 2026-05-06 keychain migration created `/Users/kavi/.config/kavi/secrets.json` as a filesystem fallback for keychain reads that fail. Stale token blobs in that file can hijack the token-cache load path BEFORE the fresh per-account file (`/Users/kavi/.config/kavi/tokens/<account>.json`) is consulted. On 2026-05-27, after a fresh re-auth, the runtime under launchctl context hit keychain read failure (-25308), fell back to secrets.json, got stale 2026-05-06 tokens, and never loaded the fresh per-account file. Three deploys failed with "graph token refresh failed" before the stale entries were manually cleared.

**Who feels it and when:** Whoever does a re-auth and then deploys. Today: Megha and me, with a 3-hour debug cycle to identify the load-path hijacking.

**Why now (Doshi):** Neutral. Today's specific failure resolved by manually deleting both secrets.json entries and the stale keychain entry. The underlying logic is a latent bug that recurs on every re-auth.

**Engineering sketch:**

1. **In `secrets.read_secret`:** when both keychain AND filesystem fallback have a value, prefer the source with the most recent write timestamp. Today's logic is "keychain first, then filesystem" without freshness comparison.
2. **In `graph_client._save_cache`:** dual-write to BOTH keychain AND secrets.json fallback, not only keychain plus per-account file. Today's save path writes the per-account file and tries the keychain; secrets.json never gets updated post-migration.
3. **In `add_account`:** also write to secrets.json fallback on cache persist. Today `add_account` writes only the per-account file, leaving secrets.json stale.
4. **Tests** simulating the launchctl-keychain-readable plus ssh-keychain-blocked split that this bug surfaced.

**Verify with:** Re-auth via add_account. Restart runtime. Confirm secrets.json fallback has fresh tokens. Confirm token refresh works under both ssh and launchctl contexts. The pre-fix offending state is preserved at `/Users/kavi/.config/kavi/secrets.json.before-2026-05-27-fix` on Kavi's Mac for reference.

**How I'll know I was wrong:** Same staleness bug recurs after this fix (means there is a third cache backend I did not account for). OR dual-write adds measurable latency to the hot path (measure; should stay sub-millisecond).

**Status:** queued 2026-05-27. Driver: 2026-05-27 deploy debugging cycle, see commit history for context.

---

## LLM-judge eval for persona drift

After axial coding stabilizes (~3 weeks of weekly batches), pick top 2-4 residual failure modes per `evals/eval-bootstrap-prompt.md` step 7. Build one narrow binary LLM judge per mode. Validate each against Megha's open-coded labels with a confusion matrix (not just agreement %). Run daily on a production sample. Goal: catch persona drift over time without re-doing manual labeling weekly. Dep: ~3 weeks of labeled axial data from the HTML-viewer flow.

**Status:** queued 2026-05-27.

---

## LLM-judge eval for inbox-to-task drift

Same shape as the persona LLM-judge entry above. After ~3 weeks of inbox HTML-viewer batches stabilize. Goal: drift detection without daily labeling.

**Status:** queued 2026-05-27.

---

## Rename `eval-persona-outbound-judgments.jsonl` → `eval-persona-judgments.jsonl`

Pending a runtime code patch + redeploy. The "outbound" prefix is redundant (persona IS the generative capability); canonical form per the standardization is `eval-<scope>-judgments.jsonl`. Coordinate the rename with the next runtime touch. Touchpoints: runtime writer (find with `grep -rn "eval-persona-outbound-judgments" kavi-runtime/`), the `evals/definitions.md` schema, the SessionStart/runtime endpoint that exposes the file via HTTP.

**Status:** queued 2026-05-27.

---
## Cold-fallback audit and deletion (architectural refactor, phase 1 of 3)

**What's broken or at risk:** Every "cold fallback" / "safe fallback" / "f-string assembly" path in the runtime is a frozen snapshot of the spec at the moment it was written. When a new rule lands (stale-date skip, 120-char cap, no "reply N yes" syntax, anything voice-related) it goes into the LLM composer path but NOT into the fallbacks. Within weeks, each fallback violates more rules than it honors. The fallback was originally added with "Better Kavi sounds robotic than goes silent." That ratio inverts the moment the spec evolves past the fallback's snapshot.

**Who feels it and when:** Megha, every time an LLM call legitimately fails (API timeout, JSON-shape failure, over-length output) AND the fallback fires. 2026-05-29 9 PM rollup is the canonical incident: four distinct symptoms (Parent Assoc surfaced, done tasks shown, dump-everything format, "reply N yes" syntax we removed weeks ago) all from one f-string fallback that hadn't been touched since 2026-05-07.

**Why now (Doshi):** Leverage. The cost of leaving stale fallbacks in place is at least one full investigation per incident plus a fix that may target the wrong layer. The cost of deleting them is one helper rewrite each. Already shipped this pattern for `_compose_periodic_summary_fstring` (commit 56ae349); same template extends to the others.

**Engineering sketch:**
- Audit candidates (find with): `grep -n "cold_fallback\|fstring\|_safe_.*_fallback\|cold fallback" kavi-runtime/kavi_runtime/*.py | grep -v test_`
- Known candidates (2026-05-29):
  - `_compose_clarifying_reply_cold_fallback` in `handlers.py` — when clarifying composer LLM call fails
  - `_safe_clarify_fallback_question` in `claude_client.py` (~line 47) — when clarifying composer trips the forbidden-state-claim check
  - Post-action-reply fallback (find precise location during audit)
  - Likely more in coordination, qa, weekly-self-check composers
- Per-fallback procedure: (a) read the fallback's output template, (b) check it against the current capability spec rules + structural_checks, (c) if ANY rule is violated by the template, delete the fallback function and replace the call site with one deterministic safe sentence ("Pausing reply while I sort the inputs. <one-line surface to Megha>"), (d) if the template currently honors all rules, leave it for now but add a code comment "AUDIT 2026-MM-DD: rules honored at this date. Re-audit when [persona spec / structural_checks] changes." (e) write a test that locks the safe-message behavior so future LLM-failure tests fail open to the safe sentence, not back to the deleted fallback.
- Update `CLAUDE.md` role contract: "Cold fallbacks are forbidden going forward. When an LLM call fails, the only allowed recovery is (a) one retry with tighter prompt, OR (b) one deterministic safe sentence. No frozen templates that pretend to be Kavi."

**Verify with:** `grep -nc "cold_fallback\|_safe_.*_fallback" kavi-runtime/kavi_runtime/*.py | grep -v test_ | awk -F: '{s+=$NF} END {print s}'` returns ≤2 (only the audit-comment-tagged ones that are still safe). Currently 6+.

**How I'll know I was wrong:** Megha reports an incident where the runtime fired the new safe sentence in a context where she'd rather have had the old fallback's content. (Means the safe sentence is too generic; iterate the wording per call type.) OR auditing reveals a fallback whose template honors all current rules AND will continue to honor them as the spec evolves (means it's a true template, not a snapshot — keep it; rare).

**Status:** SHIPPED 2026-05-29 (commit 8ffec47). 8 fallback paths audited. 1 deleted (`_compose_clarifying_reply_cold_fallback` — violated g_v2_prose enumeration + g_p1 status board language). 7 kept with `AUDIT 2026-05-29` code comments naming the rules checked + re-audit trigger. CLAUDE.md gains a Cold-fallback policy section forbidding new ghost-spec templates. 505 tests pass. Re-verify count: `grep -c "AUDIT 2026-05-29" kavi-runtime/kavi_runtime/*.py kavi-runtime/kavi_runtime/coordination_handler.py` returns ≥7.

---

## Composer skill is the single spec for that composer (architectural refactor, phase 2 of 3)

**What's broken or at risk:** For any LLM composer (periodic_summary, conversational, clarifying reply, qa, etc.), behavior rules live in 4-5 places that must evolve together by hand: `capabilities/kavi-persona.md` (anticipation, voice traits, refusal layer), `kavi-runtime/skills/<composer>.md` (per-composer task instructions), `kavi_runtime/structural_checks.py` (length, format, prose-vs-list gates), `kavi_runtime/claude_client.py` (the user_msg framing for the API call), and sometimes a cold fallback template that mirrors a frozen snapshot of all the above. When a rule lands, the engineer has to remember which N of these 5 to edit. The 2026-05-29 parent-assoc cycle is the canonical incident: stale-date skip went into the skill, NOT into the fallback (caught), NOT into structural_checks (could have caught), NOT into kavi-persona.md (one layer up).

**Who feels it and when:** Megha, every time a rule fix doesn't fully take because one of the 5 places didn't get updated. The engineer, every time an investigation fans out across the same 5 files to find which one owns the rule that fired the bug.

**Why now (Doshi):** Neutral leaning Leverage. Each unfanned rule costs an investigation. But this refactor has higher coordination cost than phase 1 (cold-fallback audit). Recommend after phase 1 ships and the cold-fallback class is closed; the surface area for "where does a rule live" shrinks meaningfully once fallbacks are gone.

**Engineering sketch:**
- Define the three axes once: PERSONA (voice, identity, refusal — loaded from `capabilities/kavi-persona.md` via persona_loader), STRUCTURAL CONSTRAINTS (length, format, prose-vs-list, no-status-board — loaded from `structural_checks.py` rules + verified post-compose), BEHAVIOR (what to surface, when to skip, how to anchor — owned by `kavi-runtime/skills/<composer>.md`).
- Inventory: for each composer (periodic_summary, conversational, clarifying_reply, qa_question, qa_ack, post_action_reply, coordination_*, correction_classifier, etc.) — currently has rules in N places. Pick one composer (recommend periodic_summary since it's freshest in mind) as the pilot.
- For the pilot composer: rewrite the skill file as the SINGLE behavior source. Move anything voice-y in the skill to persona. Move anything length/format/structural in the skill to structural_checks. The skill should ONLY answer "what should this composer surface / skip / anchor on / sequence" and nothing about voice or form.
- Rewrite the user_msg in `claude_client.py` for that composer to JUST load skill + load persona + plug in input. Zero duplication of skill content.
- Add an architectural test: `tests/test_composer_skill_isolation.py` that loads each composer's skill file and asserts the text doesn't contain voice-y phrases that belong in persona (e.g., "warm", "first-person", "no signoff") or structural phrases that belong in structural_checks (e.g., "<=120", "prose", "no enumeration"). Fails on regression.
- Once the pilot ships clean, extend to other composers one at a time.
- Update `CLAUDE.md` role contract: "Each composer skill owns BEHAVIOR. Persona owns VOICE. Structural checks own CONSTRAINTS. Never duplicate across axes."

**Verify with:** `tests/test_composer_skill_isolation.py` passes for the pilot composer (periodic_summary_composer.md). `grep -ci "120\|prose\|first-person\|no signoff\|warm" kavi-runtime/skills/periodic_summary_composer.md` returns ≤2 hits (currently many).

**How I'll know I was wrong:** Investigations on rule-bug incidents don't get faster after this lands (means the fan-out wasn't the bottleneck). OR the three-axis split forces awkward judgments where a rule genuinely belongs in two axes (means the split isn't crisp; revisit).

**Status:** PILOT SHIPPED 2026-05-29 (commit fa47fa7) for `periodic_summary_composer.md`. Three coordinated edits: (1) `LENGTH_CAP_TARGET` / `LENGTH_CAP_HARD` promoted to module-level constants in `structural_checks.py`; (2) `claude_client.compose_periodic_summary` imports them in the user_msg + over-length check (no more hardcoded 120/180); (3) skill file stripped of `≤120 characters, in prose` (structural — owned by structural_checks) and `warm note` (voice — owned by persona); HTML comment names the three-axis contract. New `tests/test_composer_skill_isolation.py` enforces the contract with two parametrized assertions (no hardcoded length caps; no voice-leak phrases). 507 tests pass. Smoke verified end-to-end. CLAUDE.md gains a "Three axes for any LLM composer" section.

**Remaining (queued):** extend the three-axis split to other composers one at a time. As of Phase 6 (2026-06-02, commit `97f457e`), the architectural test `test_composer_skill_isolation.py` now auto-discovers every skill file under `kavi-runtime/skills/` and requires each to be classified as either `REFACTORED_COMPOSER_SKILLS` (three-axis-clean) or `KNOWN_UNREFACTORED_SKILLS` (debt list). The KNOWN_UNREFACTORED_SKILLS count is the cleanest measure of remaining work. To migrate a skill OFF the unrefactored list: audit skill for voice/structural leaks → move voice rules to `capabilities/kavi-persona.md` → reference `structural_checks.LENGTH_CAP_TARGET` constants in `claude_client.py` user_msg → strip hardcoded numerics from the skill body → move the filename across the two sets in `test_composer_skill_isolation.py`. ~15-30 min per composer. *Verify with:* `len(KNOWN_UNREFACTORED_SKILLS)` in `tests/test_composer_skill_isolation.py` trends toward zero. Currently 18 unrefactored, 1 refactored.

---

## One state file per concept (architectural refactor, phase 3 of 3)

**What's broken or at risk:** Runtime state is scattered across 6+ files with no contract for what each owns. `pending_facts.jsonl` (durable facts awaiting confirmation). `imessage-state.json` (which contains both `questions` Q&A AND `summary_queue` AND probably more). MS To Do via Graph (the actual task list). `durable_facts` (cross-day facts in a different store than pending_facts). Eval JSONLs (audit trail, but de facto source for some Investigator queries). `runtime-events.jsonl` (operational events). Every Investigator rebuilds the mental model of "which file holds what" from scratch. The 2026-05-29 first investigation looked at `pending_facts.jsonl` and missed `imessage_state.json` entirely; the parent-assoc bad input was in `questions` inside `imessage-state.json` the whole time. Wasted a full investigation cycle.

**Who feels it and when:** Engineer (every investigation pays a "which file" tax). Megha indirectly (longer time to fix). Recurs on every cross-state bug.

**Why now (Doshi):** Neutral. This is the biggest refactor in scope. Real migration risk (existing on-Kavi state files have to be read and split atomically without dropping data). The benefit (one mental model) compounds but doesn't unblock immediate work. The Investigator's `live_state_source` registry column (added today) is a sufficient band-aid for now. Recommend last. Only ship once phase 1 and phase 2 are in.

**Engineering sketch:**
- Inventory: list every state file the runtime currently reads or writes. For each, classify by concept (Q&A awaiting Megha's reply, summary queue items, durable facts, transient cache, subscription state, etc.). Many files hold one concept; `imessage-state.json` holds at least three.
- Define the contract: one concept = one file = one read path = one write path. The role registry's `live_state_source` column lists exactly one place per concept.
- Pilot migration: split `imessage-state.json` into `questions.jsonl` (pending Q&A) and `summary_queue.jsonl` (queue items). Keep `imessage-state.json` for anchor + miscellaneous transient until those get their own files too.
- Atomic migration on first runtime boot after deploy: detect the legacy combined file, write the split files, archive the legacy with `.legacy-archived` suffix. Same pattern as `state_invariants.py` already does for legacy `.claude/` state paths.
- For each existing reader and writer, update to use the new file paths. Add tests that assert reads return the same content as before migration.
- After pilot stabilizes (~1 week of clean fires), tackle the next concept split.
- Update `_role_registry.md` `live_state_source` column per capability when files change.

**Verify with:** `ssh kavi@100.64.0.10 "ls /Users/kavi/HomeOS/state/*.jsonl /Users/kavi/HomeOS/state/*.json 2>/dev/null | wc -l"` returns one file per concept (post-migration, more files but each is one-concept). Currently 1-2 files holding multiple concepts.

**How I'll know I was wrong:** Investigators don't get faster after this lands (the file-fan-out wasn't the bottleneck; the real problem was something else). OR the split creates new sync bugs where one concept ends up in two places (means the migration wasn't atomic or the contract wasn't enforced).

**Status:** SHIPPED 2026-06-02. Migration completed on Kavi at SHA 90500eb (boot-time log `migration_phase3_completed`, all 7 concepts written, 0 unowned keys, 0 concepts skipped, backup file `/Users/kavi/HomeOS/state/imessage-state.json.pre-phase3-backup`). Per-concept files now on disk: `questions.json`, `summary_queue.json`, `pause_state.json`, `pending_alerts.json`, `action_clarifications.json`, `alert_dedupe.json`, `self_check.json`. Code shipped: `kavi_runtime/state_per_concept.py` (load/save helpers + boot migration entry point), `kavi_runtime/_phase3_migrate.py` (shared slicer), `scripts/migrate_state_to_per_concept.py` (CLI), `kavi_runtime/main.py` boot wiring, `kavi_runtime/state.py` compatibility shim (load/save_imessage_state now dispatches across per-concept files transparently). `_role_registry.md` `live_state_source` column points at per-concept files. 565 tests pass (was 534; +31 from Phase 3 suite); `verify_deploy.sh` PASSED post-deploy; kavi-persona deep verify endpoint returned PASS on a done-task+past-event synthetic payload. 5 commits: `34a0711` (inventory), `13fd641` (abstraction + tests), `e1fb0d7` (migration script + tests), `f1e4ae8` (boot wiring + tests), `e259107` (compatibility shim), `90500eb` (registry update).

**Phase 3b SHIPPED 2026-06-02 (Phase 5 of the architectural refactor):** every production call site under `kavi_runtime/` was migrated from the `load_imessage_state` / `save_imessage_state` shim to direct per-concept calls (`state_per_concept.load_<concept>` / `save_<concept>`). Migrated modules: `guardrails.py` (12 sites), `weekly_self_check.py` (6 sites), `handlers.py` (3 sites — `_apply_qa_resolutions` + pause_state lookup), `handler_alerts.py` (2 sites — also fixed a production bug where dedupe was silently writing to a non-existent legacy file post Phase 3 rename). `state.py` internal helpers (`add_pending_question`, `enqueue_summary_item`, `save_pending_clarification`, `save_summary_anchor`, `clear_*`) also rewired to call per-concept directly. The legacy `load_imessage_state` / `save_imessage_state` API remains in `state.py` solely as a test-fixture helper (130+ test sites use it to reset state to `{}`); the architectural test `test_no_legacy_state_refs.py` enforces zero production call sites. Commit `cbaca16`. *Verify with:* `cd kavi-runtime && .venv/bin/python -m pytest tests/test_no_legacy_state_refs.py -v` passes.

---

## BlueBubbles `fetch_recent_messages` returns 404 from one-shot context

**Status:** CLOSED 2026-06-03 by `af66b47` (receipt-as-truth verify rewrite, Layer A). The 404 from one-shot context was specifically a symptom of the per-message verify flow polling `/api/v1/message/query` against a chat BlueBubbles' chat.db had not mirrored. With per-message receipt as ground truth, the verify flow no longer calls `fetch_recent_messages` at all — there is no longer a per-message code path that can hit this 404 in either the runtime process or a one-shot context. The poll API stays available for the heartbeat callers (which target Megha's already-mirrored chat) and for the new daily channel heartbeat (which seeds and confirms recipient chats one at a time).

Surfaced 2026-06-02 evening during Max's intro-message send.

---

## Outlook email fallback resolves recipient to Megha regardless of intended recipient

**Status:** FULLY CLOSED 2026-06-03 by `af66b47` + `4596b99` + `6451516`. The original symptom — false-alarm "iMessage failed silently" Outlook emails to Megha for Max-bound sends — is eliminated end to end:

1. `af66b47` killed the chat-history-mirror false-negative class (receipt-as-truth verify, Layer A) and added the daily channel heartbeat (Layer B).
2. The follow-on commit (today) suppresses the per-message Outlook fallback on the documented BlueBubbles hang shape (`send_response.status == "timeout"` with no GUID), which Apple Push still delivers but BlueBubbles never returns a receipt for. The fallback now fires only on genuine send failures (HTTP non-2xx, network error, BlueBubbles unreachable, 200 with no GUID). BB-hang silent delivery logs `BB_HANG_SILENT_DELIVERY` for observability instead. Architectural test `tests/test_fallback_only_on_real_send_failure.py` locks the contract.
3. The same follow-on lands a chat-seed step in `add_account` (and Max's chat seeded manually on Kavi as part of this PR) so future household members never hit the BB-hang shape on their first send.

Surfaced 2026-06-02 evening during Max's intro-message send.

---

## Channel heartbeat read-after-send timing produces false-degraded verdicts

**Status:** CLOSED 2026-06-03 `6451516`. Tonight's probe false-flagged both Megha and Max as degraded because `_find_ping_in_chat` ran immediately after `send_message`, and BlueBubbles' chat.db sync from Apple Push takes 10-30s. The heartbeat now sends all pings in pass 1, sleeps `POST_SEND_SETTLE_SEC` (30s) once, then reads all chats in pass 2. Test: `tests/test_channel_heartbeat.py::test_settle_sleep_fires_after_sends_before_reads`.

---

## Outbound content scanner blocks heartbeat degradation alerts on phone-number content

**Status:** CLOSED 2026-06-03 `6451516`. The scanner's `account_number` regex (10-12 standalone digits) matched the recipient's phone number in the alert body and silently dropped the email — Megha would have gotten no signal that the channel was degraded. The heartbeat now passes `bypass_scanner=True` to `graph.send_mail` for degradation alerts. The bypass is safe: `graph.send_mail` enforces that `bypass_scanner` callers must target a household handle, so the bypass cannot exfiltrate arbitrary content to arbitrary addresses. Test: `tests/test_channel_heartbeat.py::test_alert_fires_when_no_round_trip_recorded_ever` asserts every alert call uses `bypass_scanner=True`.

---
