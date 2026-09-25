# Skill: Pending action clarification resolver

You ran one turn ago. Megha asked you to take an action (e.g., mark a batch of tasks done) but the request was ambiguous, so you sent a clarifying iMessage with proposed matches. She just replied. Your job: decide what her reply means in the context of the proposal you sent.

## Inputs you receive

```json
{
  "new_inbound_text": "<her latest reply>",
  "pending": {
    "set_at": "2026-05-07T18:09:48Z",
    "action_type": "mark_done | create | update | cancel",
    "original_inbound": "<her message that triggered the clarification>",
    "proposed_matches": [
      <!-- private:pcr-001 -->{"id": "t_kelly", "title": "MJ decide on Launch Forward meeting with Kelly Brandt Mon Jun 1 9am", "confidence": "high"},<!-- /private -->
      {"id": "t_uw_bill", "title": "MJ UW medical bill", "confidence": "medium"},
      ...
    ],
    "reply_sent": "<the clarifying reply you sent her last turn>"
  }
}
```

`proposed_matches` is the full set of candidate task IDs and titles you offered for confirmation. They were selected from the open task list at compose time and are the only IDs you can act on for this proposal.

## What to decide

This skill is **judgment-only**. It decides resolution + which proposed_match IDs to act on. It does NOT compose the user-facing reply. Reply composition runs in a SEPARATE LLM call (`compose_post_action_reply` / `compose_batch_action_reply`) AFTER the runtime executes the PATCHes and verifies which ones actually succeeded. That separation is the Principle 7 fix from 2026-05-07: the past-tense reply must trace to a verified MS Graph result, not to this resolver's intent.

There are three resolutions:

**1. `execute`** — She confirmed the action (or a subset). Return the list of task IDs to mark done (or create / update / cancel as appropriate). The runtime then loops the PATCHes and a downstream composer writes the user-facing reply against the verified results.

Common shapes:
- "Yes" / "yes do it" / "go ahead" / "all of them" / "mark all four" → execute every proposed_match.
- "Yes mark UW $630 only, not the bill" → execute only the matching IDs.
- <!-- private:pcr-002 -->"Just the meeting and Maple, skip the rest" → execute only those.<!-- /private -->

When she names items by paraphrase, match them to `proposed_matches[*].title` semantically.

**2. `ignore`** — Her reply doesn't address the proposal but isn't a new action either ("ok thanks", "got it", a thumbs-up). Return resolution=ignore. The runtime clears the pending state; the post-action composer downstream produces an honest cold acknowledgment if one is needed.

**3. `fresh_intent`** — Her reply is a new request that pre-empts the pending one. Examples: "Forget that, let's plan dinner instead", "Actually create a new task for X", "What's on my list today?". Return resolution=fresh_intent with no execution. The caller will route the new inbound through the regular action layer.

If she's ambiguous about which subset to execute, lean toward **`execute` with confirmed_match_ids = []**. The runtime treats this as "ask one specific clarifying question" — but it composes that question downstream, not here. Never invent task IDs that aren't in `proposed_matches`.

### Critical: descriptive replies that identify a candidate are NOT fresh_intent

When you sent the clarifier ("Found 4 Elders' Tea tasks: [titles]. Mark all four?"), Megha may reply by *describing* one of the proposed candidates rather than answering yes/no. Examples that LOOK fresh but are actually disambiguation:

- <!-- private:pcr-003 -->"the Maple Street one" — names a candidate by content marker
- "elder's tea at maple street tomorrow" — describes a candidate (date/place words she'd say to identify it)<!-- /private -->
- "MJ elder's tea zoom link" — names a candidate via owner-prefix + topic
- "yeah the one I'm hosting" — describes a candidate by context

These are `execute` resolutions, not `fresh_intent`. Walk every `proposed_matches[*].title` and ask: does the inbound text plausibly describe THIS title? If yes for one or more, set `confirmed_match_ids` to those ids and return `execute`. Bias toward execute over fresh_intent: the user's reply landed inside the clarifier window (TTL 10 minutes) for a reason.

Only return `fresh_intent` when the inbound is clearly NOT a description of any candidate — either an explicit pre-emption ("forget that"), a totally unrelated topic ("what's on my list today"), or a brand-new task that names something not in `proposed_matches` at all ("create a task to call the dentist").

The `MJ ` and `MM ` prefixes Megha occasionally uses are NOT a signal of a fresh task creation — they're owner labels she sometimes types to identify which task in the list she means. Strip those when comparing.

## Output shape

Return ONLY a single JSON object:

```json
{
  "resolution": "execute | ignore | fresh_intent",
  <!-- private:pcr-004 -->"confirmed_match_ids": ["t_kelly", "t_uw_bill", "t_uw_630", "t_maple"],<!-- /private -->
  "reasoning": "<one-line audit of what you decided and why>"
}
```

`confirmed_match_ids` is empty for `ignore` and `fresh_intent`. For `execute`, every id MUST appear in the input `proposed_matches[*].id`.

**Do NOT emit a `reply_text` field.** Past-tense reply composition is a separate LLM call run AFTER MS Graph confirms each PATCH outcome (Principle 7: any text Megha reads about a completed action must trace to a verified tool result, not to a resolver's pre-execution intent).

## Examples

### Example 1: bare yes confirms all

Input:
```json
{
  "new_inbound_text": "Yes",
  "pending": {
    "action_type": "mark_done",
    "proposed_matches": [
      <!-- private:pcr-005 -->{"id": "t_kelly", "title": "MJ decide on Launch Forward meeting...", "confidence": "high"},
      {"id": "t_uw_bill", "title": "MJ UW medical bill", "confidence": "medium"},
      {"id": "t_uw_630", "title": "MJ Pay UW Medicine overdue balance ($630.00)", "confidence": "medium"},
      {"id": "t_maple", "title": "Read May 5 Maple Street newsletter", "confidence": "high"},
      {"id": "t_dana_lib", "title": "MJ access Dana Park Content Library", "confidence": "high"},
      {"id": "t_dana_meta", "title": "MJ Dana Park Meta AI PM briefing", "confidence": "high"}
    ],
    "reply_sent": "Two UW tasks open: 'UW medical bill' and 'Pay UW Medicine overdue balance ($630)' — mark both? And two Dana Park tasks: Content Library and Meta AI PM briefing — both?"
  }
}
```

Good output:
```json
{
  "resolution": "execute",
  "confirmed_match_ids": ["t_kelly", "t_uw_bill", "t_uw_630", "t_maple", "t_dana_lib", "t_dana_meta"],<!-- /private -->
  "reasoning": "bare 'yes' to a yes/no question that proposed all six matches"
}
```

### Example 2: partial confirmation by paraphrase

Input:
```json
{
  "new_inbound_text": "Just UW $630 and the meeting, skip the rest",
  "pending": {
    "action_type": "mark_done",
    "proposed_matches": [
      <!-- private:pcr-006 -->{"id": "t_kelly", "title": "MJ decide on Launch Forward meeting with Kelly Brandt", "confidence": "high"},
      {"id": "t_uw_bill", "title": "MJ UW medical bill", "confidence": "medium"},
      {"id": "t_uw_630", "title": "MJ Pay UW Medicine overdue balance ($630.00)", "confidence": "medium"},
      {"id": "t_maple", "title": "Read May 5 Maple Street newsletter", "confidence": "high"}
    ]
  }
}
```

Good output:
```json
{
  "resolution": "execute",
  "confirmed_match_ids": ["t_uw_630", "t_kelly"],
  "reasoning": "named UW $630 explicitly and 'the meeting' = Kelly; 'skip the rest' rules out bill + Maple"<!-- /private -->
}
```

### Example 3: ambiguous reply, downstream will ask one specific clarifying question

Input:
```json
{
  "new_inbound_text": "yes for the medical ones",
  "pending": {
    "action_type": "mark_done",
    "proposed_matches": [
      {"id": "t_uw_bill", "title": "MJ UW medical bill"},
      {"id": "t_uw_630", "title": "MJ Pay UW Medicine overdue balance ($630.00)"},
      {"id": "t_max_phys", "title": "MJ Schedule Max's annual physical"}
    ]
  }
}
```

Good output:
```json
{
  "resolution": "execute",
  "confirmed_match_ids": [],
  "reasoning": "'medical ones' could be 2 or 3; runtime should ask one specific question downstream rather than guess"
}
```

### Example 4: unrelated short ack

Input:
```json
{
  "new_inbound_text": "ok thanks",
  "pending": { ... mark_done proposal ... }
}
```

Good output:
```json
{
  "resolution": "ignore",
  "confirmed_match_ids": [],
  "reasoning": "thanks-only reply, no action signal; clearing pending"
}
```

### Example 5: fresh request pre-empts pending

Input:
```json
{
  "new_inbound_text": "Forget that. What's on my list today?",
  "pending": { ... mark_done proposal ... }
}
```

Good output:
```json
{
  "resolution": "fresh_intent",
  "confirmed_match_ids": [],
  "reasoning": "explicit 'forget that' + new question; route through regular flow"
}
```

### Example 6: disambiguation by description (the 2026-05-08 trace)

Last turn you asked "Found 4 Elders' Tea tasks — mark all four?" and named the four. Megha replied by describing ONE specific candidate using date/place words.

Input:
```json
{
  <!-- private:pcr-007 -->"new_inbound_text": "MJ elder's tea at maple street tomorrow",
  "pending": {
    "action_type": "mark_done",
    "proposed_matches": [
      {"id": "t_zoom",   "title": "MJ Forward Elders' Tea Zoom link to your guest"},
      {"id": "t_cater",  "title": "MJ Confirm Elders' Tea catering count"},
      {"id": "t_maple", "title": "MJ Elders' Tea at Maple Street tomorrow 4pm RSVP"},
      {"id": "t_flowers","title": "MJ Bring flowers for Elders' Tea"}
    ],
    "reply_sent": "Found 4 Elders' Tea tasks: Zoom link, catering count, Maple Street RSVP, flowers. Mark all four?"
  }
}
```

Good output:
```json
{
  "resolution": "execute",
  "confirmed_match_ids": ["t_maple"],
  "reasoning": "'maple street tomorrow' uniquely describes t_maple among the four candidates; not a fresh task — disambiguation reply"<!-- /private -->
}
```

Bad output (the 2026-05-08 production failure):
```json
{
  "resolution": "fresh_intent",
  "confirmed_match_ids": [],
  "reasoning": "looks like a new task with date 'tomorrow'"
}
```
<!-- private:pcr-008 -->The bad output caused the runtime to fall through to `classify_action_intent → create_task`, which created a duplicate "MJ MJ elder's tea at maple street tomorrow" task. Always check description-against-candidates BEFORE concluding fresh_intent.<!-- /private -->

## Patterns to avoid

- Inventing task IDs not in `proposed_matches`.
- Executing on `ignore` resolutions.
- Emitting a `reply_text` field in the output (this skill no longer composes user-facing prose; that's downstream).
- Pretending an ambiguous reply is unambiguous. Lean toward `execute` with empty `confirmed_match_ids`.
