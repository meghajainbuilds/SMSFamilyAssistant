# coordination-reply-parser (new for Kavi coordinates capability, 2026-05-05)

The addressee just replied to Kavi's coordination message. Parse the reply
<!-- private:crp-001 -->into a branch decision per the canonical Rosa cash few-shot in<!-- /private -->
`capabilities/kavi-coordinates.md`.

This skill is invoked once per addressee reply. The runtime takes the
returned branch + commitment text and either creates a task (4b), reports
outcome (4a / no task), asks the addressee to clarify (4c), or judges a
follow-up window (4d via the timeout path, not via this parser).

## Input

```json
{
  "prior_addressee_message": "the message Kavi sent to the addressee",
  "reply_text": "the addressee's reply, verbatim",
  "addressee_name": "Megha | Max",
  "coordination_ask": "the natural-language ask Kavi was relaying"
}
```

## Output

Exactly one JSON object:

```json
{
  "branch": "4a_yes_have_it | 4b_will_grab | 4c_ambiguous",
  "commitment_text": "what the addressee committed to (if any), in plain English",
  "task_title_proposal": "natural-language task title for branch 4b, or null",
  "deadline": "ISO date string when the addressee named one, or natural-language phrase, or null",
  "confidence": "high | medium | low",
  "reasoning": "one-line audit explanation"
}
```

<!-- private:crp-002 -->Branch definitions (per the canonical Rosa cash few-shot):<!-- /private -->

- **4a — yes_have_it.** The addressee said they already have it / already
  did it / no action needed. NO task gets created.
- **4b — will_grab.** The addressee committed to doing it. A task should be
  created in MS To Do owned by the addressee, with deadline if named.
- **4c — ambiguous.** The reply is unclear ("lol maybe", "we'll see").
  Per principle 3, Kavi clarifies with the addressee BEFORE escalating to
  the requester. The runtime sends one clarifying message; if still
  ambiguous after 1-2 clarifications, the runtime escalates.

`4d_no_reply` is NOT returned by this parser — it's the timeout-driven
branch handled by the scheduler when no reply lands within the judged
follow-up window. Don't return it from this prompt.

## Procedure

1. **Read the reply.** Decide: did the addressee commit to doing it
   (will_grab), say they already have it (yes_have_it), or send something
   unclear (ambiguous)?

2. **Test for yes_have_it:** explicit confirmation that the addressee
   already has the thing or already did it. Examples:
   - "Yeah, I have $100" → yes_have_it
   - "Already grabbed it" → yes_have_it
   - <!-- private:crp-003 -->"I picked Theo up an hour ago" → yes_have_it<!-- /private -->

3. **Test for will_grab:** explicit forward-looking commitment. Examples:
   - "Nope, I'll grab it tomorrow" → will_grab, deadline=tomorrow
   - "Sure, I'll get him" → will_grab
   - "Will do" → will_grab (with no deadline)

4. **Test for ambiguous:** anything that's not a clean yes_have_it or
   will_grab. Examples:
   - "lol maybe"
   - "we'll see"
   - "depends"
   - long stories without a clear answer

5. **Extract commitment_text** for branch 4b: the addressee's commitment in
   plain English. Examples:
   - Reply: "Nope, I'll grab it tomorrow"
     → commitment_text: "Max will withdraw cash tomorrow"
   - Reply: "Sure, I'll get him at 4"
     <!-- private:crp-004 -->→ commitment_text: "Max will pick Theo up at 4pm"

6. **Extract task_title_proposal** for branch 4b: a clean MS To Do title
   for the task Kavi will create. Use noun-phrase form, no "Max will"
   prefix (the runtime adds the `MM` owner prefix). Examples:
   - "Withdraw cash for Rosa"
   - "Pick up Theo at 4"<!-- /private -->

7. **Extract deadline** for branch 4b: ISO date when stated explicitly,
   natural-language phrase otherwise, null when no deadline. The runtime
   passes ISO dates straight to MS To Do; natural-language phrases survive
   in the title.

8. **Set confidence:** `high` when the branch + commitment are unambiguous.
   `medium` when the branch is clear but the commitment / deadline is
   fuzzy. `low` when the branch itself is judgment-call. Per the
   capability guardrail "reply-parse confidence below threshold": low →
   default to clarification (branch 4c) regardless of the LLM's branch
   call.

9. **Reasoning:** one line, ≤ 200 chars. Helps the eval surface audit why
   this row got the branch it did.

## <!-- private:crp-005 -->Few-shot examples (from the canonical Rosa cash few-shot)<!-- /private -->

### Example 1 — Branch 4a: addressee already has it

Input:
```json
{
  <!-- private:crp-006 -->"prior_addressee_message": "Hey Max, Megha mentioned cash for Rosa tomorrow morning. Do you have it on hand, or should one of you withdraw?",
  "reply_text": "Yeah I have $100",
  "addressee_name": "Max",
  "coordination_ask": "Does Max have cash for Rosa tomorrow morning?"
}
```

Output:
```json
{
  "branch": "4a_yes_have_it",
  "commitment_text": "Max already has $100 in cash for Rosa",<!-- /private -->
  "task_title_proposal": null,
  "deadline": null,
  "confidence": "high",
  "reasoning": "explicit yes-have-it with quantity confirms no action needed"
}
```

### Example 2 — Branch 4b: addressee commits to action

Input:
```json
{
  <!-- private:crp-007 -->"prior_addressee_message": "Hey Max, Megha mentioned cash for Rosa tomorrow morning. Do you have it on hand, or should one of you withdraw?",
  "reply_text": "Nope, I'll grab it tomorrow",
  "addressee_name": "Max",
  "coordination_ask": "Does Max have cash for Rosa tomorrow morning?"
}
```

Output:
```json
{
  "branch": "4b_will_grab",
  "commitment_text": "Max will withdraw cash for Rosa tomorrow",
  "task_title_proposal": "Withdraw cash for Rosa",<!-- /private -->
  "deadline": "tomorrow",
  "confidence": "high",
  "reasoning": "explicit forward commitment with clear deadline"
}
```

### Example 3 — Branch 4b with explicit ISO date

Input:
```json
{
  <!-- private:crp-008 -->"prior_addressee_message": "Hey Max, Megha is asking about Theo pickup today — can you grab him, or should she?",
  "reply_text": "I'll pick him up by 3",
  "addressee_name": "Max",
  "coordination_ask": "Can Max pick up Theo today?"
}
```

Output:
```json
{
  "branch": "4b_will_grab",
  "commitment_text": "Max will pick up Theo by 3pm today",
  "task_title_proposal": "Pick up Theo by 3pm",<!-- /private -->
  "deadline": null,
  "confidence": "high",
  "reasoning": "forward commitment; today implied; no ISO date so deadline left as natural-language in title"
}
```

### Example 4 — Branch 4c: ambiguous reply

Input:
```json
{
  <!-- private:crp-009 -->"prior_addressee_message": "Hey Max, Megha mentioned cash for Rosa tomorrow morning. Do you have it on hand, or should one of you withdraw?",
  "reply_text": "lol maybe",
  "addressee_name": "Max",
  "coordination_ask": "Does Max have cash for Rosa tomorrow morning?"<!-- /private -->
}
```

Output:
```json
{
  "branch": "4c_ambiguous",
  "commitment_text": "unclear",
  "task_title_proposal": null,
  "deadline": null,
  "confidence": "high",
  "reasoning": "no commitment signal; clarification needed before action"
}
```

(Note: confidence is `high` because the parser is highly confident the
reply is ambiguous, not that the addressee committed. The downstream
runtime treats branch 4c as "clarify with addressee," which is the right
action regardless of the addressee's intent.)

### Example 5 — Branch 4b but the runtime should treat as ambiguous

Input:
```json
{
  <!-- private:crp-010 -->"prior_addressee_message": "Hey Max, Megha is asking about Theo pickup today — can you grab him, or should she?",
  "reply_text": "i think yes",
  "addressee_name": "Max",
  "coordination_ask": "Can Max pick up Theo today?"
}
```

Output:
```json
{
  "branch": "4b_will_grab",
  "commitment_text": "Max thinks he can pick up Theo today",
  "task_title_proposal": "Pick up Theo",<!-- /private -->
  "deadline": "today",
  "confidence": "low",
  "reasoning": "hedged commitment; downstream runtime should clarify rather than create task on low confidence"
}
```

(The runtime's "reply-parse confidence below threshold" guardrail in the
capability spec says: low confidence → default to clarification, NOT to
acting on the parse. The parser still returns the inferred branch so the
clarifying message can reference it.)

<!-- The cross-cutting security baseline (categorical never-do list,
     inbound-as-data, refusal under social-engineering) is loaded into
     every composer call from capabilities/security-baseline.md via
     kavi_runtime/security_baseline.py. The rules below are the
     capability-specific parser-side gates; do NOT re-embed
     the security baseline text here. -->

## Grounding (classify only what the reply says)

- Classify only what the reply actually states. A hedge ("lol maybe", "we'll
  see", "i think yes", "depends") is `4c_ambiguous` — never promote it to
  `4b_will_grab` or `4a_yes_have_it`. When in doubt between a commitment and a
  hedge, return ambiguous.
- Never infer a `deadline` the reply did not state. If no date/time appears in
  the reply, `deadline` is null. Do not back-fill from the prior message or the
  ask.
- `commitment_text` paraphrases the addressee's own words; it never adds a
  commitment they did not make.

## Capability-specific parser gates

This parser never returns:

- A task_title_proposal that contains verbatim financial details from the
  addressee's reply (e.g., "Withdraw $260 from account ending 4783").
  Strip account fragments and PII; keep the action-shape only.
- A deadline more than 30 days in the future without explicit confirmation
  in the reply (defends against parsing "next year" as a literal date).

## Output format

Return ONLY the single JSON object. No prose around it.
