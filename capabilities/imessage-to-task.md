---
name: imessage-to-task
status: shipped (create-task verb v0)
capability_type:
  - judgment
  - agentic
last_updated: 2026-05-05
---

# iMessage to task

> **Status note:** stub created 2026-05-05 alongside the live ship of the create-task verb. Spec body intentionally minimal — this doc exists today to host acceptance criteria for the verb. The full Behavior / Metrics / Architecture sections will be filled in when the broader capability is interview-driven into shape.

## TL;DR

When Megha or Max sends Kavi a free-text iMessage that names a task to add ("Add a task to call the pediatrician tomorrow"), Kavi creates the task in the McMullen-Jain Shared MS To Do list with the right owner prefix, dedupes against duplicate webhooks on the inbound message id, and replies confirming the task within ~10 seconds. The verb is one slice of the broader iMessage to task capability; mark-done was the first verb (2026-05-05 morning), create-task is the second (this build, 2026-05-05 afternoon).

## Open questions

- Owner inference beyond sender default: when Megha texts "Add a task for Max to grab milk," should Kavi switch the owner to MM despite the sender being Megha? Today owner = sender's default; downstream LLM-driven owner inference is queued.
- Natural-language deadline parsing ("tomorrow," "by Friday," "next Tuesday"). Today only `YYYY-MM-DD` substrings are extracted to the structured `dueDateTime` field; natural phrases stay in the title body so Megha still sees them, but the structured deadline is empty. Worth a follow-up after we see real usage.
- Multi-action requests in one inbound ("Add X and mark Y done") — out of scope for v0; classifier returns low confidence and falls through.
- Cross-talk with `kavi-coordinates`: when an inbound is "ask Max if X" rather than "create a task for X," routing belongs to `kavi-coordinates`, not here. The action layer router decides.

## Acceptance criteria — mark-done verb (live as of 2026-05-05 evening)

(g) **Live execution on high-confidence match.** An inbound iMessage classified as `mark_done` with high intent confidence AND an LLM-judged high-confidence title match against the open shared list executes a PATCH on the task (status=completed) within 10 seconds and replies with a past-tense confirmation ("Marked done — '<title>' is closed out."). No dry-run gate.

(h) **Ambiguous match triggers clarification, not action.** When the title matcher returns medium or low confidence, or when classifier confidence on the action verb itself is medium or low, no PATCH fires. Kavi replies with a clarifying question listing up to three plausible candidate task titles ("Couldn't pin that down on '<reference>'. Did you mean: (a) ..., (b) ..., (c) ...?"). Caught case is `MJ Pay UW Medicine overdue balance ($630.00)` versus an inbound paraphrase of `UW Medicine balance ($630)`: under exact-string matching, this fell through; under LLM matching, it executes cleanly.

(i) **Action layer always replies; conversational composer is bypassed.** When the action-intent classifier returns `has_action=true` with any `action_type` in `{mark_done, create, update, cancel}`, the iMessage reply ALWAYS comes from the action layer — never from `compose_conversational_reply`. The action layer returns one of: `{action_executed, result=success}`, `{action_executed, result=already_completed}` (honest no-op), `{action_clarifying, reason in {ambiguous_match, no_match, intent_confidence_*}}`, or `{action_detect_only, reason=verb_not_wired}` for `update`/`cancel`. The conversational composer only runs when `has_action=false`. This serialization fix closed the trust gap caused by the prior partial bypass: the create-task happy path returned a reply, the mark-done fall-through returned None, and the conversational composer fabricated completion claims for inbounds it should never have seen.

(j) **Already-completed pre-check.** A high-confidence match on a task whose status is already `completed` skips the PATCH (which would 200 but be a no-op masquerading as a fresh completion) and replies "Already done — '<title>' was already marked complete. No change."

## Acceptance criteria — create-task verb

These are the criteria Megha can use to call the verb shipped or broken. Numbered to cross-reference the implementation in `kavi-runtime/kavi_runtime/handlers.py::_handle_create_task_verb` and the eval surface at `evals/kavi-persona/eval-persona-action-intent.jsonl`.

(a) **Latency to task creation.** An inbound iMessage classified as `create_task` with high confidence creates a task in the McMullen-Jain Shared MS To Do list within 10 seconds of arrival.

(b) **Owner prefix.** The created task title is the parsed action phrase prefixed with `MJ` when the inbound is from Megha, `MM` when from Max. Sender match is by phone or by Megha's known email; unknown sender falls back to MJ (documented in `_owner_prefix_from_sender`). Format: `<owner_prefix> <title>` — same convention the email-to-task path uses.

(c) **Deadline.** When the inbound contains a `YYYY-MM-DD` substring, the date is set as the structured `dueDateTime` on the task. Otherwise the structured deadline is left empty; the natural-language phrase ("tomorrow," "by Friday") survives in the title body so Megha still sees it on the task. Natural-language parsing is queued as a follow-up.

(d) **Dedup on duplicate webhooks.** A duplicate webhook fire on the same `source_imessage_id` does NOT create a duplicate task. Enforced via the linkedResource `externalId` on the created task (`GraphClient.find_todo_task_by_source_email`); the second arrival short-circuits before the POST and returns the existing task id.

(e) **Post-action reply.** Within 10 seconds of the task creation, Kavi replies to the sender confirming the task was created (e.g., "Added 'call the pediatrician 2026-05-12' to the shared list."). Past-tense framing only — the verb composes via `post_action_reply_composer.md` with a deterministic cold fallback when the LLM call returns empty/over-length.

(f) **Eval row.** A row is appended to `evals/kavi-persona/eval-persona-action-intent.jsonl` with `executed=true` on a fresh create. On a dedup hit, `executed=false` with `decision=executed_dedup_hit`. On a fall-through (low/medium confidence, empty title), `executed=false` with the appropriate `fall_through_*` decision.

## Action-claim correspondence (cross-reference)

The action layer ships its own past-tense reply (`compose_post_action_reply`) ONLY after the MS Graph PATCH/POST verifies success — same contract Principle 7 of `capabilities/kavi-persona.md` declares. The bypass invariant ((i) above) is the structural defense: the conversational composer never sees an action-implying inbound, so it never has reason to produce action verbs without a tool result. The bypass + the action-claim contract are paired: removing either one re-opens the hallucination class that surfaced 2026-05-05 (mark-done fall-throughs) and 2026-05-07 (pending-clarification multi-mark).

Action verbs that require grounding: the canonical list — `sent, marked, added, dropped, deleted, removed, filed, done, resending, resent, delivered, scheduled, queued, completed, closed`. The post-action composer's groundedness rule (in `kavi-runtime/skills/post_action_reply_composer.md`) requires every verb in its output to trace to an `actions_executed[]` entry with `result=success`. G-A1 (`kavi_runtime/structural_checks.py::passes_g_a1`) is the runtime gate that catches a regression in either composer or runtime path.

## Acceptance criteria — security baseline (cross-verb)

Both verbs (mark-done and create-task) inherit the security baseline. These criteria sit alongside the per-verb ones above and apply to every iMessage to task path.

(k) **Inbound sender allowlist.** When an inbound iMessage classified as action-implying is received from a sender NOT in `household.md`, the message is discarded before classification reaches the action layer or the persona composer. The discard is logged to the existing inbound JSONL with `discarded_unknown_sender: true`. No task is created, no reply is sent, no LLM tokens are spent on the payload. This is the inbound half of the iMessage allowlist gate documented in `capabilities/realtime-kavi.md` Guardrails.

(l) **Outbound content scanner on replies.** Outbound replies (post-action confirmations, clarifying questions, ack messages) pass the outbound content scanner before BlueBubbles SEND. A scanner block results in no SEND, a logged event in `outbound_blocked.jsonl`, and an alert to Megha. This is the outbound half of the same gate; the scanner is owned by Realtime Kavi and called from this capability's reply path.

## Source of truth for `create_task` intent

What counts as a `create_task` request is defined in the action-intent classifier skill prompt: `kavi-runtime/skills/action_intent_classifier.md`. That file owns the definition of "imperative action vs conversational" and the few-shot examples for each `action_type`. Update there, not here, when the trigger surface needs tuning.

## Cross-references

<!-- private:imt-001 -->- `capabilities/kavi-coordinates.md` (Cap 2) depends on this verb. Branch 4b of the cash-for-Rosa few-shot ("create a task for Max to bring cash by Sunday") routes through `_handle_create_task_verb` once `kavi-coordinates` is wired live.<!-- /private -->
- `capabilities/inbox-to-task.md` is the email-driven sibling capability. It shares the title rendering convention (`<owner_prefix> <title>`) and the `linkedResources.externalId` dedup pattern but uses `email_id` as the dedup key instead of `source_imessage_id`.
- `kavi-runtime/skills/post_action_reply_composer.md` owns the reply voice for both verbs (`mark_done` and `create_task`).

## Changelog

- **2026-05-06 — Outbound content scanner now wired into the create-task verb's MS To Do write.** Audit follow-up: `graph_client.create_task_in_shared_list` (the verb's write site) now runs the rendered title + body through the regex scanner BEFORE the Graph POST. A card-shaped, SSN-shaped, routing-shaped, or account-number-shaped numeric in the iMessage that triggered the create raises `OutboundContentBlocked`. The verb's existing exception path catches it; the post-action reply ships an honest "couldn't add — hit an error" instead of a phantom success. The block is audit-logged to `outbound_blocked.jsonl` with `surface=todo_body` and the source `imessage_id` extras. **How I'll know I was wrong:** a benign 9-digit string in a legitimate task title (e.g., a shipment tracking number) trips routing_number detection and Max sees the verb as broken. Mitigation: tune the regex set rather than weaken the gate; tracking numbers are typically 12+ digits or alphanumeric so the false-positive surface should be small. **Muscle:** Risk.
- **2026-05-05 — Security baseline acceptance criteria (k) and (l) added.** Two new criteria cross-cutting both verbs: (k) inbound sender allowlist (discard iMessages from senders not in `household.md` before classification — no task, no reply, no LLM tokens spent on the payload); (l) outbound content scanner on replies (post-action confirmations and clarifications run through the outbound content scanner before BlueBubbles SEND; a block results in no SEND + logged event in `outbound_blocked.jsonl` + alert to Megha). Both inherit from the broader security baseline added to Realtime Kavi Guardrails today. Spec-only landing; the runtime gate code is queued under Realtime Kavi's Hardening implementation roadmap. Driver: as Max's inbox gets added (multi-account onboarding), and as the action layer expands to create-task / update / cancel, the cost of a wrong-recipient SEND or a leaked sensitive-pattern reply scales linearly with capability surface area. Bake the security gates into the spec before that surface widens. **How I'll know I was wrong:** the inbound sender allowlist locks out a household member whose handle changed (Max swaps phones or SIM, his iMessage handle changes, all his action-implying iMessages get silently discarded for hours before Megha notices); OR the outbound scanner blocks a legitimate confirmation reply that happens to contain a number resembling a credit card prefix, and Megha experiences the verb as broken. **Muscle:** Risk + Governance.
- **2026-05-05 evening — three fixes to close the trust gap.** Megha approved live; shipped together as one dispatch.
  - **Fix 1: mark-done flipped from dry-run to live execution.** `action_layer.dry_run` flipped `true → false` in `kavi-runtime/config.yaml`; the dry-run code path in the mark-done branch removed. Already-completed pre-check stays — high-confidence matches against `status=completed` tasks skip the PATCH and reply with an honest "already done." **How I'll know I was wrong:** a `executed_failure` row spike on real mark-done attempts in `eval-persona-action-intent.jsonl`, OR a "marked done" reply ships against a task whose state didn't actually change.
  - **Fix 2: exact-string title match replaced with LLM judgment.** `find_task_by_exact_title` (string equality, normalized) was missing real paraphrases — Megha said "UW Medicine balance ($630)" against a stored title of "MJ Pay UW Medicine overdue balance ($630.00)" and the lookup returned None. Replaced with `ClaudeClient.match_target_to_open_task`: a Sonnet 4.6 call passing `target_text` plus the top 30 open tasks (id + title + status + recent_activity), returning `{match_id, confidence, reasoning}`. Skill prose at `kavi-runtime/skills/action_target_matcher.md`. Cached on the rubric prefix; tasks list is dynamic per call. Conservative bias: medium confidence does NOT execute, it triggers a clarifying question. **How I'll know I was wrong:** a wrong-task mark-done shows up in eval (high-match → wrong title), OR clarification rate climbs so high that Megha experiences mark-done as broken.
  - **Fix 3: action layer always replies; conversational composer bypassed.** Prior behavior: the mark-done fall-through path returned None, letting `compose_conversational_reply` see action-implying inbounds and fabricate "marked it done" claims. New behavior: when `has_action=true` with any `action_type` in `{mark_done, create, update, cancel}`, the action layer returns a non-None dict and ships its own reply. Detect-only verbs (`update`, `cancel`) reply with "I see you want to <verb> '<X>' — that verb isn't wired yet." Ambiguous and no-match cases ship a clarifying question. Empty-target / low-confidence intent cases ship a clarifying question too. Conversational composer only runs when `has_action=false`. **How I'll know I was wrong:** any "marked done" reply that doesn't have a corresponding `executed_success` row in `eval-persona-action-intent.jsonl`, OR any `compose_conversational_reply` call paired with a `has_action=true` row in the same triggered_by chain.
- **2026-05-05 — create-task verb shipped live.** Wired `_handle_create_task_verb` into the action layer; live execution (no dry-run gate, per Megha's approval). Owner prefix derived from sender (MJ default for Megha, MM for Max). Deadline parsed only from `YYYY-MM-DD` substrings; natural-language phrases left in title. Dedup via `linkedResources.externalId = source_imessage_id`. **How I'll know I was wrong:** an `executed_failure` row appears in `eval-persona-action-intent.jsonl`, OR a duplicate webhook produces two task rows in the shared list, OR the post-action reply fires before the task actually appears.
