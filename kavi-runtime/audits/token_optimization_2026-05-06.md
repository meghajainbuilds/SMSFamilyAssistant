# Token optimization audit — HomeOS — 2026-05-06

Read-only audit. No code or capability docs were modified. Complementary to today's Haiku staged rollout (`kavi-runtime/audits/model_audit_2026-05-06.md`); does not contradict it.

## Bottom line

- **Current 7-day spend: $26.99. Daily average: $3.37/day. Run rate: ~$101/mo.**
- **One capability dominates.** `inbox-to-task` (the email-to-tasks judgment call) accounts for **$21.33 / 79%** of all 7-day spend across 496 calls. Everything else combined is $5.66.
- **Total estimated savings if all recommendations applied: ~$70-95/month, roughly 70-90% of current spend.** Floor (LLM stays on Sonnet, denylist + 1-hr cache only): ~$70/mo. Ceiling (full prompt-doc trim + max_tokens tightening): ~$95/mo. After savings, projected run rate: ~$10-30/mo.
- **Top three highest-leverage changes:**
  1. **REC-1: Pre-LLM marketing/newsletter denylist.** ~$60/mo. 92% of inbox-to-task calls today resolve to "skipped"; 71% of those skips are marketing/newsletter the LLM unanimously rejects. A sender-domain denylist + `List-Unsubscribe` header check catches them at the door, with zero LLM call.
  2. **REC-2: Move inbox-to-task system prompt to Anthropic 1-hour cache TTL.** ~$25-35/mo. Cache writes alone cost $14.24/7d (67% of inbox-to-task spend) because 40% of arrivals exceed the 5-min ephemeral TTL gap. 1-hour TTL collapses re-writes ~60-80%.
  3. **REC-3: Trim `capabilities/inbox-to-task.md` to LLM-relevant sections only.** ~$8-12/mo. The 70KB capability doc is injected verbatim into every email-to-tasks system prompt; ~40KB (60%) is Metrics + Architecture + Multi-account runbook + Known issues + Changelog — content the LLM never uses at decision time.

The first three changes alone get spend under $30/mo with no model changes and no quality risk to composer paths. The Haiku rollout already in flight stacks on top for an additional ~$3-6/mo on top.

## Call-site inventory

`claude_client.py` has **18 call sites** (no other module calls the Anthropic SDK directly — checked `coordination_handler.py`, `handlers.py`, `outbound_scanner.py`, `package_extractor.py`, `persona_prompts.py`; all routes go through `ClaudeClient`). All cache the system prefix via `cache_control: ephemeral` (5-min TTL).

Real-data table for the calls we have usage rows on; rough estimates for everything else (the runtime-events / structured anthropic.call_done log only landed today, so 7-day data exists only for `email_to_tasks` via `eval-inbox-judgments.jsonl` and indirectly via the cost dashboard's `email_arrived` total).

| Call site | Calls/day | Model | P50 input | P95 input | P50 output | P95 output | $/day | Cache-hit rate |
|---|---|---|---|---|---|---|---|---|
| `run_email_to_tasks` | **70.9** | sonnet | 3,059 | 4,869 | 208 | 284 | **$3.05** | **49.3%** mean (60% of calls have any read; 40% are cold) |
| `compose_periodic_summary` | 2 | sonnet | n/a | n/a | n/a | n/a | <$0.05 | n/a (no per-call log) |
| `compose_weekly_self_check` | 0.14 | sonnet | n/a | n/a | n/a | n/a | <$0.01 | n/a |
| `compose_qa_question` | 1-3 | sonnet | n/a | n/a | n/a | n/a | <$0.05 | n/a |
| `classify_correction` | 5-15 | sonnet | est ~3,500 | est ~4,000 | <50 | <80 | est <$0.30 | n/a |
| `check_semantic_duplicate` | 30-80 | sonnet | est ~400 | est ~800 | <50 | <80 | est ~$0.15 | n/a |
| `classify_pause_intent` | <1 | sonnet | est ~3,500 | est ~4,500 | <50 | <80 | <$0.05 | n/a |
| `classify_self_check_reply` | 0.14 | sonnet | est ~3,500 | est ~4,000 | <50 | <80 | <$0.01 | n/a |
| `classify_qa_reply` | 3-10 | **haiku (flipped today)** | est ~3,500 | est ~5,000 | 50-150 | 200 | est ~$0.05 (was ~$0.20) | n/a |
| `classify_action_intent` | 3-15 | **haiku (flipped today)** | est ~6,500 | est ~8,000 | <50 | <80 | est ~$0.05 (was ~$0.30) | n/a |
| `match_target_to_open_task` | 1-5 | sonnet | est ~7,000 | est ~9,000 | <100 | <150 | est <$0.10 | n/a |
| `compose_post_action_reply` | 1-5 | sonnet | est ~3,800 | est ~4,500 | 50-120 | 180 | est <$0.10 | n/a |
| `compose_conversational_reply` | 5-15 | sonnet | est ~4,000 | est ~5,000 | 50-120 | 180 | est ~$0.30 | n/a |
| `classify_coordination_intent` | 1-5 | sonnet | est ~7,800 | est ~9,500 | <80 | <120 | est <$0.10 | n/a |
| `compose_coordination_ack` | 1-5 | sonnet | est ~4,800 | est ~5,500 | 50-120 | 180 | est <$0.10 | n/a |
| `compose_coordination_addressee_message` | 1-5 | sonnet | est ~6,500 | est ~8,000 | 100-200 | 250 | est <$0.10 | n/a |
| `parse_coordination_reply` | 1-5 | sonnet | est ~8,500 | est ~10,000 | 50-150 | 200 | est <$0.10 | n/a |
| `compose_coordination_outcome` | 1-5 | sonnet | est ~5,800 | est ~7,000 | 50-120 | 180 | est <$0.10 | n/a |

**Reading the table:** real numbers come from 7 days × 496 rows on `run_email_to_tasks`. Other rows are estimates derived from skill-file size + the `email_arrived` cost dashboard category total ($5.66/day across all non-`inbox-to-task` calls in the last 7 days). The newly-added `anthropic.call_done` structured-log event will give per-call-type real numbers within 7 days; flag this as a re-audit checkpoint.

## Recommendations ranked by $/mo savings

### REC-1 — Pre-LLM marketing/newsletter denylist (the biggest find)

- **Call site:** `run_email_to_tasks` only.
- **Change:** Insert a deterministic pre-filter ahead of the LLM call in `handlers.py` (where the email payload is built). Skip without an LLM call if any of these match: (a) sender domain in a maintained denylist (top-15 senders observed: `e.m.hannaandersson.com`, `mail.vogue.com`, `eml.nordstrom.com`, `jobalerts-noreply@linkedin.com`, `news@parentmap.com`, `dailyskimm@morning7.theskimm.com`, `farfetch@emails.farfetch.com`, etc.); (b) `List-Unsubscribe` header present (Graph exposes `internetMessageHeaders`); (c) `auto-submitted: auto-generated` header. Each match emits a `decision=skipped, skip_reason=pre_filter_marketing` row to `eval-inbox-judgments.jsonl` for audit, but no Anthropic call.
- **Data behind it:** of 496 inbox-to-task calls in the last 7 days, **455 (92%) resolved to "skipped"**. Of skips, **334 (73%) cite "marketing", "newsletter", "promotional", "advertisement" in the LLM's reason field** — the LLM is unanimous on these. The top-15 senders alone account for 207 calls / $8.72/7d / **$37.37/mo**, all skipped. Total marketing/newsletter LLM-skip cost: **$14.04/7d → $60.18/mo**.
- **Savings:** $50-60/month (conservative; assumes denylist catches 80-95% of these; the rest still go through the LLM).
- **Quality risk:** **Low.** The denylist is a deterministic short-circuit on senders the LLM already consistently skips. The risk is a real action item from a denylisted sender (e.g., a Nordstrom order confirmation), but this stream is empirically zero in the corpus reviewed. Mitigation: ship the denylist behind a 7-day shadow mode that logs what *would* have been skipped, then promote.
- **Effort:** **S-M.** Sender-domain denylist is a config edit + 30 lines in `handlers.py`. Header-based detection adds another 20 lines. The eval-judgments row format already supports a synthetic skip row.
- **Why now vs later:** Single biggest dollar lever in the system; no model risk; ships before any persona work. Deferring 30 days = $50-60 of avoidable spend.

### REC-2 — Move inbox-to-task system prompt to Anthropic 1-hour cache TTL

- **Call site:** `run_email_to_tasks` (highest leverage). Stretch: every other call that uses `_build_system_prompt`.
- **Change:** Anthropic prompt caching exposes two TTL tiers — `ephemeral` (5 min, 1.25× input price) and a 1-hour TTL (2× input price for the write, but cache reads stay at $0.30/M for Sonnet). Today every cache-control block is `ephemeral`. Switch the inbox-to-task system prefix to the 1-hour tier. The runtime's traffic pattern (median 75-second inter-call gap, but 40% of calls fall in gaps > 5 minutes) means the 5-min TTL is **expiring between 4 of every 10 emails**. Each expiry forces a fresh ~16K-token cache write at $3.75/M ($0.06 per write).
- **Data behind it:** cache_write tokens = 3,797,614 tokens / 7d = **$14.24/7d**. Cache_read tokens = 4,973,425 / 7d = $1.49/7d. **Cache write cost is 90.5% of total cache cost.** 200 of 495 inter-call gaps (40.4%) exceed 300 seconds. Modeling: if 1-hour TTL converts 60% of cold-cache misses into reads, we save **$8.54/7d → $36.62/mo**. The 1-hour write is 2× the ephemeral price (so a real cache write costs more), but two writes that become one writes-plus-many-reads is net massively cheaper at this volume.
- **Savings:** **$25-35/month** (conservative band, accounting for the 1-hour TTL's higher per-write cost).
- **Quality risk:** **Low (mechanical).** Same model, same prompt, same output; only the cache TTL flag changes. Behavior is identical.
- **Effort:** **S.** One-line `cache_control: {"type": "ephemeral", "ttl": "1h"}` change in `_build_system_prompt`. Validate via the structured-log `cache_creation` vs `cache_read` totals over a 24-hour window post-flip.
- **Why now vs later:** stacks with REC-1. After REC-1 cuts call volume ~60%, the remaining inbox-to-task calls cluster more sparsely in time — making 5-min TTL even less effective. 1-hour TTL is the right tier for the post-denylist arrival pattern.

### REC-3 — Trim `capabilities/inbox-to-task.md` to LLM-relevant sections only

- **Call site:** `run_email_to_tasks`.
- **Change:** The capability doc is 69,668 chars (~17K tokens) and is concatenated into every email-to-tasks system prefix. Section-by-section size audit:
  - TL;DR + Why now + Behavior spec + Hard rules + Examples + Q&A learned + Precedence: ~28KB → **the LLM uses this**.
  - Metrics (Stage 1, eval pass criteria, error budget): ~12KB → **the LLM does not use this at decision time**.
  - Architecture (data flow, stack, lookups): ~9KB → **doesn't drive any individual decision**.
  - Multi-account onboarding runbook: ~5KB → **runbook for humans, not the LLM**.
  - Known issues / queued: ~10KB → **engineering backlog, no decision relevance**.
  - Changelog: ~5KB → **historical context only**.
  - Total non-decision content: **~41KB / ~10K tokens per cache write**.
- **Implementation:** two options. (a) **In-place trim** of `inbox-to-task.md` to remove non-decision sections — risky because Megha and the team rely on those sections for ops/product work. (b) **Split injection** — keep `inbox-to-task.md` whole as the human-facing doc; let `_inbox_to_task()` in `claude_client.py` strip sections matching `^## Metrics`, `^## Architecture`, `^## Multi-account onboarding runbook`, `^## Known issues / queued`, `^## Changelog` before injection. The model gets a leaner version; humans keep the full doc.
- **Savings:** removing ~10K tokens from each cache write at $3.75/M = $0.0375 per cache write × ~44 cache writes/day × 30 days = **$50/mo at face value, but most of that is already eaten by REC-1 cutting call volume and REC-2 cutting cache-write frequency.** **Stand-alone savings after REC-1 + REC-2 land: $8-12/mo** (a smaller cache prefix means cheaper writes for the writes that still happen).
- **Quality risk:** **Medium.** Lower-risk path is option (b) — runtime-side stripping with a unit test verifying the stripped output matches an expected "decision-relevant" prefix. Option (a) is a doc rewrite that touches a high-traffic capability spec.
- **Effort:** **M.** Section-stripping helper + tests. Need to iterate with eval data to confirm the stripped sections weren't load-bearing for some edge case (Megha's Q&A entries may inadvertently reference Architecture context — a 7-day shadow eval would catch that).
- **Why now vs later:** least urgent of the top three. The dollar value is modest. But this is also a **future-proofing** change: the capability doc grew 30KB in the last two weeks; without a strip rule, every Q&A entry adds another ~$0.10/mo permanently to the runtime.

### REC-4 — Tighten `max_tokens=1024` on `run_email_to_tasks` to 512

- **Call site:** `run_email_to_tasks`.
- **Change:** Default `max_tokens` for email-to-tasks is 1024 (`config.yaml: claude.max_tokens: 1024`). Real P95 output is 284 tokens. The model never gets close to 1024. Tightening to 512 saves nothing on output cost (output is billed per token actually emitted, not per `max_tokens`) but **prevents runaway emit on a malformed prompt or jailbreak** — which would cost ~$0.015 per blown call. This is more of a guardrail than an optimization, but it ships in 5 minutes.
- **Savings:** $0/mo direct. Blast-radius reduction: a single jailbreak attempt that gets the model to emit a 1024-token output costs ~$0.015 vs a capped 512-token output costing ~$0.0075. Across the year, this is an order of $0-$5/mo expected value.
- **Quality risk:** **Low.** P95 is 284 tokens; 512 is 1.8× P95. No real call would be truncated.
- **Effort:** **S.** Three-character edit in `config.yaml`.
- **Why now vs later:** ship-with-rest. Trivial.

### REC-5 — Drop redundant payload fields from `run_email_to_tasks` user message

- **Call site:** `run_email_to_tasks`.
- **Change:** The user payload today serializes the email as `json.dumps(email_payload, indent=2)` with all fields. Audit the field list against what email_to_tasks actually uses. Likely candidates to drop or shorten:
  - `internetMessageId` (Graph debugging field; not needed for judgment).
  - `conversationId` (used for thread fetch upstream, but the LLM doesn't need it).
  - Long `internetMessageHeaders` array (the LLM uses sender + subject + body; a dozen `Received:` headers add tokens with no signal).
  - HTML body when `bodyPreview` would suffice for marketing detection (already addressed by REC-1).
- **Savings:** $1-3/mo (rough; depends on how chatty the payload is — without a quick instrumentation pass I can't compute exactly, and didn't have time in this audit window).
- **Quality risk:** **Low** if scoped to fields the LLM demonstrably ignores. Validate by running a 50-row eval on the trimmed payload vs full payload — accuracy delta should be 0.
- **Effort:** **M.** Code edit + 50-row eval validation.
- **Why now vs later:** smaller dollar value; can wait until REC-1/2/3 ship.

### REC-6 — Memoize `check_semantic_duplicate` for identical (proposed_title, recent_tasks_hash) over 24h

- **Call site:** `check_semantic_duplicate`.
- **Change:** Today this fires on every task-create candidate, even if the same proposed title against the same recent_tasks set was checked an hour ago. Memoize the result keyed on `(proposed_title.lower(), hash(tuple(sorted(t.id for t in recent_tasks))))` with a 24-hour TTL.
- **Savings:** $1-3/mo. Today's cost dashboard shows 30-80 calls/day and the call is small (~400 input tokens). Memoization probably halves call volume.
- **Quality risk:** **Low.** Memoization invalidates whenever the recent_tasks set changes — which is whenever any task is created or completed.
- **Effort:** **M.** In-process LRU cache + invalidation on task-list change events.
- **Why now vs later:** minor; ship when convenient.

### REC-7 — After 24-hour Haiku eval data lands, flip the staged classifiers

- **Call site:** `classify_correction`, `classify_self_check_reply`, `classify_pause_intent`, `classify_coordination_intent`, `parse_coordination_reply` (the 5 commented-out routes in `config.yaml`).
- **Change:** Already staged per `audits/model_audit_2026-05-06.md`; flip pending eval results.
- **Savings:** $3-6/mo per the prior audit.
- **Quality risk:** medium per call-by-call (eval gate exists).
- **Effort:** S.
- **Why now vs later:** after 24h of `classify_action_intent` + `classify_qa_reply` Haiku data validates accuracy — gate recommended in the prior audit.

## Systemic findings (not per-call)

### F1: 92% of inbox-to-task calls are LLM-skips. The LLM is being used as a marketing classifier.

This is the dominant pattern in the corpus. The LLM is doing work a regex + sender denylist could do for ~80% of inputs, and the remaining 20% (the actually-actionable email) is where the LLM's judgment is load-bearing. Today we pay full Sonnet rates on every email arrival even when 19 out of 20 are an obvious skip.

The architectural lens: the LLM call is the wrong primitive for "is this a real action item or marketing noise." That's a routing problem — pre-filter at the door, send only ambiguous-or-actionable through the LLM. The same pattern likely applies to `classify_correction` and `classify_action_intent` (regex-detect "delete", "move", "rename" patterns first — only fall through to LLM on ambiguous text).

[Pattern flag: pre-filter-before-llm-classify]

### F2: Cache writes cost 7× cache reads, and the 5-min TTL is the bottleneck

For inbox-to-task in the last 7 days: $14.24 in cache writes vs $1.49 in cache reads. The 5-min TTL was the right default when the runtime fired at the rate of a slash command; it's wrong now that webhook arrivals are sparse and bursty. 1-hour TTL is the right default for any always-on runtime where call inter-arrival exceeds the 5-min cliff in 30%+ of cases.

[Pattern flag: cache-ttl-matches-arrival-pattern]

### F3: Cache prefix size grows with capability docs that aren't decision-relevant

`inbox-to-task.md` is the canonical Hard rules + Examples + Q&A doc but also the operational playbook (Metrics, Architecture, Runbook, Known issues, Changelog). Right now we pay to cache all of it, every time. Without a strip-at-injection rule, every doc edit raises baseline cost. Similar risk in `kavi-persona.md` if it gets full-injected anywhere.

### F4: 16K-token cache write is the modal value (185 of 307 writes)

Histogram of cache_write sizes shows 16,237 tokens as the dominant size — that's the email_to_tasks prefix. Other sizes (25K, 26K) appear when additional context is concatenated. There's no fragmentation; the cache is consistently sized. Trimming the prefix once trims it everywhere.

## Cache-hit rate findings

`run_email_to_tasks`: **49.3% mean cache-hit rate across all calls; 60% of calls have any read; 40% are full cold writes.** This is below Anthropic's typical 70%+ benchmark for stable-prefix workloads. Diagnosis: the runtime's webhook arrival pattern (P50 inter-call gap = 75 sec, P75 = 16 min, P90 = 41 min) crosses the 5-min ephemeral TTL threshold on roughly 4 of every 10 calls. Solution = REC-2 (1-hour TTL).

No other call has 7-day usage data (the structured `anthropic.call_done` log only landed today). Re-audit cache-hit rates across all 18 calls in 7 days.

## Risks of NOT optimizing

At today's $3.37/day run rate with no changes, monthly spend lands at **$101/mo, 3.4× the configured $30/mo cap**. The runtime would auto-pause within ~9 days each month per `guardrails.monthly_anthropic_spend_usd_cap`. Megha would see: Kavi stops responding to email after 9-10 days/month for a cost reason, not a value reason. That's a reliability failure that masks an architectural inefficiency.

The user-visible failure mode is worse than the dollar number: **the most expensive emails to process are also the least valuable ones** (marketing). Hitting the cap means Kavi goes silent on the actually-actionable email arriving on day 11+. Cost optimization here is not "saving money" — it's **buying back uptime on the days when Kavi's judgment matters**.

Three-month projection at current trajectory: $300 of spend, with ~$210 of it on email Kavi unanimously skipped. The denylist is the most embarrassingly leveraged change in the audit.

## Out of scope but worth flagging

- **Structured outputs / JSON mode.** All 18 calls today use prompt-driven JSON ("Return ONLY a single JSON object"). Anthropic released structured-output enforcement that can guarantee JSON shape via the API; would eliminate parse-error fallback paths and shave a few output tokens per call (not having to emit `\`\`\`json` fences). Out-of-scope because it's a refactor, but worth a separate audit.
- **`max_tokens` audit on coordination calls.** The coordination composers use 200-512 max_tokens; outputs run 100-200. There's modest tightening room (~$0.50-2/mo) but I didn't have real per-call data to size precisely. Re-audit in 7 days when `anthropic.call_done` rows accumulate.
- **No structured-log usage rows for non-`email_to_tasks` calls.** Today's cost dashboard splits only `inbox-to-task` vs `email_arrived`. The `anthropic.call_done` event landed in `structured_log.py` today but no rows have accumulated. Re-audit 2026-05-13 with a full 7-day per-call-type breakdown.
- **Persona-refusal-layer caching.** The refusal layer is constant ~1K tokens, appended to ~12 of 18 system prompts. It's already inside the cache prefix, so no separate cache layer needed today. If a future composer prompt is dynamic per-call, the refusal layer should sit in its own stable cache block separated from the variable suffix.
- **Pricing of cache writes vs reads is ~12.5× ratio for Sonnet.** That's why cache writes dominate. Worth noting if a future model has a different ratio — REC-2's economics are model-specific.

## Re-audit checklist

- 2026-05-13: re-audit with 7-day `anthropic.call_done` data covering all 18 call sites.
- After REC-1 + REC-2 land: validate cost dashboard shows expected drop; re-compute cache-hit rate (target: >80% mean).
- After REC-3 lands: spot-check 50 inbox-to-task evals to confirm no accuracy drop from stripped prompt.
