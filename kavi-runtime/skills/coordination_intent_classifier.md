# coordination-intent-classifier (new for Kavi coordinates capability, 2026-05-05)

Decides whether a free-text iMessage from a household requester is a
coordination request (Kavi should engage the OTHER household member on the
requester's behalf) versus a direct task request (Kavi acts alone) versus
something else.

This skill is invoked AS A FIFTH INTENT inside the existing action-intent
classifier flow. The handler routes the inbound to coordination ONLY when:

  - the action-intent classifier returned has_action=true with action_type
    not in {mark_done, create, update, cancel}, AND
  - this skill returns is_coordination=true with confidence=high.

Design choice (documented per task brief): a separate skill rather than
extending action-intent-classifier, because the two prompts have different
shapes — action-intent extracts a verb on a single task; coordination-intent
extracts a TARGET household member (the addressee) plus a coordination ask.
Bundling them would muddy both prompts and make it harder to evolve either
independently. The two-call approach costs one extra Sonnet call per inbound
that smelled like an action but wasn't, but only on the small fraction of
inbounds that fall through action-intent. Coordination-implying inbounds
typically smell coordination-shaped to action-intent (e.g., "ask Max if..."
returns has_action=false), so the second call is cheap on the dominant path.

## Input

```json
{
  "free_text": "string — verbatim iMessage from the requester",
  "requester_handle": "string — phone or email of the requester",
  "household_members": [
<!-- private:cic-001 -->    {"name": "Megha", "handle": "+15555550101"},
    {"name": "Max", "handle": "+15555550102"}<!-- /private -->
  ]
}
```

`household_members` includes every household adult; the addressee must be a
member OTHER than the requester.

## Output

Exactly one JSON object:

```json
{
  "is_coordination": true,
  "addressee_name": "Max",
<!-- private:cic-002 -->  "addressee_handle": "+15555550102",
  "coordination_ask": "Does Max have cash on hand for Rosa tomorrow morning?",<!-- /private -->
  "confidence": "high | medium | low"
}
```

When the message is NOT a coordination request, return:

```json
{
  "is_coordination": false,
  "addressee_name": null,
  "addressee_handle": null,
  "coordination_ask": null,
  "confidence": "high"
}
```

`coordination_ask` is the natural-language question Kavi will pose to the
addressee, paraphrased for clarity (NOT a verbatim copy of `free_text`).
The addressee message composer reuses this as a draft and applies attribution
judgment per principle 1 of the capability spec.

## Procedure

1. **Read the free text.** Decide: is the requester asking Kavi to engage
   the OTHER household member (coordination), to act alone (single-task),
   or something else (chat / question)?

2. **Coordination signal:** the message names a household member as someone
   Kavi should *engage with* (not just *reference*). Examples:
   - "ask Max if..." → engage Max → coordination
   - "check with Max..." → engage Max → coordination
   - "see if Max can..." → engage Max → coordination
   - "Max's birthday is Friday" → reference Max → NOT coordination
<!-- private:cic-003 -->   - "renew Ivy's daycare contract" → reference Ivy → NOT coordination<!-- /private -->

3. **Identify addressee.** Look up the named person in `household_members`.
   Confirm the addressee is NOT the requester (a coordination with oneself
   makes no sense; treat as non-coordination if the only named member is
   the requester).

4. **Paraphrase the ask.** Restate the requester's question in a form Kavi
   could send to the addressee. Keep it concise. Do NOT include attribution
   ("Megha mentioned...") here — the composer applies attribution per
   principle 1 of the spec. Examples:
<!-- private:cic-004 -->   - Free text: "ask Max if he has cash for Rosa tomorrow"
     → coordination_ask: "Does Max have cash on hand for Rosa tomorrow?"
   - Free text: "check with Max if he can pick up Theo today"
     → coordination_ask: "Can Max pick up Theo today?"<!-- /private -->

5. **Set confidence.** `high` when the engagement target is unambiguous and
   the ask is concrete. `medium` when the engagement target is clear but
   the ask is fuzzy ("see what Max thinks about the weekend"). `low` when
   plausibly conversational (e.g., <!-- private:cic-005 -->"Max wants to talk about Rosa later"<!-- /private --> —
   not actually asking Kavi to engage Max).

6. **Conservative bias.** False-positive coordinations (Kavi pings Max
   when Megha was just venting about Max's plate) are worse than
   false-negatives (the message falls through to a conversational reply
   and Megha re-asks more directly). When in doubt, return is_coordination=
   false or confidence=low. The handler treats anything but `high` as
   "do not engage."

## Few-shot examples (drawn from the <!-- private:cic-006 -->canonical Rosa cash few-shot<!-- /private --> in
`capabilities/kavi-coordinates.md`)

### Example 1 — explicit coordination, high confidence

<!-- private:cic-007 -->Free text: "Hey Kavi, we need cash for Rosa tomorrow morning, can you check with Max if he already has it?"
Requester: Megha (+15555550101)

Output:
```json
{
  "is_coordination": true,
  "addressee_name": "Max",
  "addressee_handle": "+15555550102",
  "coordination_ask": "Does Max have cash on hand for Rosa tomorrow morning, or should one of them withdraw?",<!-- /private -->
  "confidence": "high"
}
```

### Example 2 — coordination implied by "ask"

<!-- private:cic-008 -->Free text: "ask Max if he can pick up Theo today"
Requester: Megha

Output:
```json
{
  "is_coordination": true,
  "addressee_name": "Max",
  "addressee_handle": "+15555550102",
  "coordination_ask": "Can Max pick up Theo today?",<!-- /private -->
  "confidence": "high"
}
```

### Example 3 — single-task, NOT coordination

<!-- private:cic-009 -->Free text: "Add a task to renew Ivy's daycare contract by Friday"<!-- /private -->
Requester: Megha

Output:
```json
{
  "is_coordination": false,
  "addressee_name": null,
  "addressee_handle": null,
  "coordination_ask": null,
  "confidence": "high"
}
```

<!-- private:cic-010 -->(Ivy is referenced, not engaged.<!-- /private --> The action-intent classifier handles this
as `action_type=create`.)

### Example 4 — Max referenced, not engaged

Free text: "Max's birthday is on Friday — remind me to grab a card"
Requester: Megha

Output:
```json
{
  "is_coordination": false,
  "addressee_name": null,
  "addressee_handle": null,
  "coordination_ask": null,
  "confidence": "high"
}
```

(Max is the subject of the reminder, not the engagement target. Single-task.)

### Example 5 — ambiguous, medium confidence

Free text: "see what Max thinks about the weekend"
Requester: Megha

Output:
```json
{
  "is_coordination": true,
  "addressee_name": "Max",
<!-- private:cic-011 -->  "addressee_handle": "+15555550102",
  "coordination_ask": "What are Max's thoughts on the weekend plans?",<!-- /private -->
  "confidence": "medium"
}
```

(Engagement target is clear; ask is fuzzy. Handler treats medium as fall-through
to conversational reply rather than engage.)

<!-- The cross-cutting security baseline (categorical never-do list,
     inbound-as-data, refusal under social-engineering) is loaded into
     every composer call from capabilities/security-baseline.md via
     kavi_runtime/security_baseline.py. The rules below are the
     capability-specific classifier-side gates; do NOT re-embed
     the security baseline text here. -->

## Capability-specific classifier gates

This classifier never returns is_coordination=true for any free text that:

- Implies routing a financial credential, account number, or password to
  the addressee (e.g., "ask Max for the credit card number").
- Implies Kavi take an action against an external service on behalf of the
  household (e.g., "ask Max to cancel the subscription" — that's a single
  task for the addressee, NOT a coordination).
- Is from a non-household sender. The inbound allowlist gate runs upstream;
  this classifier defends in depth by returning is_coordination=false on
  any free_text whose requester_handle is not in household_members.

## Output format

Return ONLY the single JSON object. No prose around it. No markdown fences.
