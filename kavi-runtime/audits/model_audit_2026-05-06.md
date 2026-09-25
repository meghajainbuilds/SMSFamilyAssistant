# Model audit — 2026-05-06

## What's broken or at risk

Every Anthropic call in `claude_client.py` runs on `claude-sonnet-4-6` (config.yaml `claude.model`). That's a single config knob — no per-call-type model routing. Some of those calls are tiny structured-JSON classifiers that produce <50 output tokens and run on every inbound iMessage; paying Sonnet rates for them is overkill. Others are persona-voiced composers where Sonnet quality is load-bearing.

This audit enumerates every call site, classifies it, and proposes a model per call. **No code changes** — recommendation only.

## Call site inventory

All call sites read `self._model` (config.yaml `claude.model`, currently `claude-sonnet-4-6`). All composer calls go through `_build_system_prompt` or `_persona_system_block`, which means cache-eligible system prefix.

| Method | Type | Skill loaded | Output shape | Output tokens (rough) | Estimated calls/day |
| --- | --- | --- | --- | --- | --- |
| `run_email_to_tasks` | Composer + judgment | `email_to_tasks` | JSON task or skipped | 200-400 | 30-80 |
| `classify_correction` | Classifier | `correction_classifier` | JSON 2 fields | <50 | 5-15 |
| `compose_periodic_summary` | Composer | `periodic_summary_composer` | JSON message | 50-120 | 2 |
| `check_semantic_duplicate` | Classifier | none (inline rule) | JSON 2-3 fields | <50 | 30-80 |
| `classify_pause_intent` | Classifier | `pause_intent_classifier` | JSON 2 fields | <50 | <1 (only while paused) |
| `compose_weekly_self_check` | Composer | `weekly_self_check_composer` | JSON message | 50-120 | ~0.14 (1/wk) |
| `classify_self_check_reply` | Classifier | `weekly_self_check_classifier` | JSON 3 fields | <50 | ~0.14 |
| `compose_qa_question` | Composer | `qa_question_composer` | JSON message | 50-120 | 1-3 |
| `classify_qa_reply` | Classifier | `qa_reply_classifier` | JSON resolutions | 50-150 | 3-10 |
| `classify_action_intent` | Classifier | `action_intent_classifier` | JSON 4 fields | <50 | 3-15 |
| `match_target_to_open_task` | Classifier+match | `action_target_matcher` | JSON 3 fields | <100 | 1-5 |
| `compose_post_action_reply` | Composer (persona) | `post_action_reply_composer` | JSON message | 50-120 | 1-5 |
| `compose_conversational_reply` | Composer (persona) | `kavi_conversation` | JSON message | 50-120 | 5-15 |
| `classify_coordination_intent` | Classifier | `coordination_intent_classifier` | JSON 5 fields | <80 | 1-5 |
| `compose_coordination_ack` | Composer (persona) | `kavi_conversation` | JSON message | 50-120 | 1-5 |
| `compose_coordination_addressee_message` | Composer (persona) | `coordination_addressee_message_composer` | JSON 2 fields | 100-200 | 1-5 |
| `parse_coordination_reply` | Classifier | `coordination_reply_parser` | JSON 6 fields | 50-150 | 1-5 |
| `compose_coordination_outcome` | Composer (persona) | `coordination_outcome_composer` | JSON message | 50-120 | 1-5 |

`persona_prompts.py` defines `PERSONA_REFUSAL_LAYER` only — it's a constant text block appended to system prompts in 12 of the 18 call sites. No standalone API calls.

## Per-call recommendations

| Method | Current | Recommendation | Why |
| --- | --- | --- | --- |
| `run_email_to_tasks` | Sonnet | **Keep Sonnet** | Owner attribution, multi-rule judgment, package extraction depend on Sonnet quality. |
| `classify_correction` | Sonnet | **Downshift to Haiku** | Tiny output, 2-field JSON, <15/day. |
| `compose_periodic_summary` | Sonnet | Keep Sonnet | Persona voice; only 2 calls/day; cache hit on the prefix. |
| `check_semantic_duplicate` | Sonnet | **Cache more aggressively** | Runs 30-80x/day; the inline rule is short but stable. Verify cache hit rate; if low, move rule to a skill file. |
| `classify_pause_intent` | Sonnet | Downshift to Haiku | Rare. |
| `compose_weekly_self_check` | Sonnet | Keep Sonnet | Persona voice; 1/wk. |
| `classify_self_check_reply` | Sonnet | Downshift to Haiku | Tiny output. |
| `compose_qa_question` | Sonnet | Keep Sonnet | Persona voice. |
| `classify_qa_reply` | Sonnet | **Downshift to Haiku** | <50-150 output, runs every conversational inbound. Highest-leverage downshift. |
| `classify_action_intent` | Sonnet | **Downshift to Haiku** | 4-field JSON, <50 output, runs every inbound. Second-highest leverage. |
| `match_target_to_open_task` | Sonnet | Keep Sonnet | Substring/paraphrase reasoning over up to 30 candidate titles; quality matters because it gates real MS To Do mark-done. |
| `compose_post_action_reply` | Sonnet | Keep Sonnet | Persona voice. |
| `compose_conversational_reply` | Sonnet | Keep Sonnet | Persona voice; load-bearing. |
| `classify_coordination_intent` | Sonnet | **Downshift to Haiku (with eval gate)** | 5-field JSON, structured. Worth trialing on a held-out set. |
| `compose_coordination_ack` | Sonnet | Keep Sonnet | Persona voice. |
| `compose_coordination_addressee_message` | Sonnet | Keep Sonnet | Persona voice + attribution rule. Highest-stakes outbound. |
| `parse_coordination_reply` | Sonnet | Downshift to Haiku (with eval gate) | Structured branch parse. |
| `compose_coordination_outcome` | Sonnet | Keep Sonnet | Persona voice. |

## Highest-leverage downshift candidates

These produce <50 tokens, run >5x/day, are pure JSON classifiers, and the failure mode (parse error → fallback) is already wired:

1. **`classify_action_intent`** — every inbound iMessage runs this. If 10 calls/day at ~3K input + 50 output = 30K + 0.5K tokens. Sonnet: ~9.1¢/day. Haiku: ~0.8¢/day. Saves ~$2.50/mo.
2. **`classify_qa_reply`** — every conversational inbound. Similar volume; similar savings.
3. **`classify_correction`** — same shape, lower volume. ~$1/mo savings.
4. **`check_semantic_duplicate`** — runs every task create. Volume is high; current call is small (<400 input tokens) but 30-80 calls/day adds up. Sonnet: ~$3-8/mo. Haiku: ~$0.30-0.80/mo. Saves ~$3-7/mo.

## Estimated savings if all recommendations applied

Rough math (dollars per month) using 30-day average:

- Classifier downshifts (correction + qa_reply + action_intent + self_check_reply + pause_intent + coordination_intent + coordination_reply): ~$8-12/mo saved.
- Semantic duplicate (cache more aggressively or downshift): ~$3-7/mo saved.
- Composers stay on Sonnet (no savings, no quality loss).

**Total estimated savings: ~$11-19/mo.** Current monthly cap is $30. Today's monthly run rate is well under cap (per `compute_monthly_spend_usd`), so this is leverage on quality risk + headroom rather than urgent cost cutting.

## Recommended sequencing

1. Add per-call-type model routing to `ClaudeClient` (config map: `{call_type: model}`, default to `claude.model`).
2. Flip `classify_action_intent` and `classify_qa_reply` first — highest volume, simplest output shape, full eval surfaces already exist (`eval_persona_action_intent_jsonl`, `eval-persona-outbound-judgments.jsonl`).
3. Run 7 days; compare classifier accuracy vs Sonnet baseline using the existing eval ladder.
4. If accuracy holds, downshift the rest of the classifier list. Composers stay on Sonnet.

**Do not swap models in this audit.** Per item #D scope: report only.
