# action-intent-classifier (new for Stage 1 action layer, 2026-05-05)

<!-- private:aic-001 -->Parses a free-text iMessage from Megha into a structured action intent. Used by `imessage_received` AFTER the correction classifier returns `not_correction` and BEFORE the conversational-reply path. The point: catch imperative requests like "Mark Oak Circle as done" and route them to a real MS To Do API call instead of a chatty "On it" that does nothing.<!-- /private -->

The runtime ACTS on `mark_done` and `create` with `confidence=high`. `update` and `cancel` are detected here for forward compatibility but the runtime falls through to the conversational reply for them today.

## Input

```json
{
  "free_text": "string — the verbatim iMessage from Megha",
  "recent_kavi_messages": [
    {"sent_at": "ISO datetime", "body": "Kavi's recent iMessage to Megha"}
  ]
}
```

`recent_kavi_messages` is optional context. Megha's imperative often references a task Kavi just surfaced ("yes, mark that one done") — recent messages help resolve `target_text`.

## Output

Exactly one JSON object:

```json
{
  "has_action": true,
  "action_type": "mark_done | create | update | cancel | null",
  "target_text": "string — the natural-language task title Megha referenced, verbatim or close, with surrounding filler stripped",
  "confidence": "high | medium | low"
}
```

If no action is implied, return `{"has_action": false, "action_type": null, "target_text": null, "confidence": "high"}`.

`confidence` is the classifier's certainty about BOTH (a) action_type and (b) target_text together. Use `high` only when both are unambiguous from the message alone (or trivially from recent_kavi_messages). When the action verb is clear but the target is fuzzy ("mark it done" without a recent referent), return `medium`. When the message is plausibly imperative but could equally be conversational, return `low`.

## Procedure

1. **Read the free text.** Decide: is Megha asking Kavi to TAKE AN ACTION on a task, or is she chatting / asking a question / thanking / reacting?

2. **If action, classify type:**
   - "Mark X done" / "X is done" / "I finished X" / "completed X" → `mark_done`
   - "Add X" / "Create a task for X" → `create`
   - "Rename X to Y" / "Change owner on X to Max" → `update`
   - "Cancel X" / "Drop X" / "Delete X" → `cancel`

3. **Extract target_text.** The natural-language task title Megha referenced. Strip filler ("the", "task", "please", "as"). Keep the task body. Examples:
<!-- private:aic-002 -->   - "Mark the Oak Circle volunteering task as done" → `"Oak Circle volunteering"`
   - "Cancel Joan Miller" → `"Joan Miller"`<!-- /private -->
   - "Mark Boonli payment done please" → `"Boonli payment"`

4. **Set confidence.** `high` only when verb + target are both unambiguous. If Megha said "mark that done" referencing a recent Kavi message and the recent message clearly named one task, that's still `high`. If she said "mark it done" with no clear referent, `medium`. Anything that could plausibly be conversational (e.g. "ok, done" as an acknowledgment of Kavi's prior message), `low` or `has_action=false`.

5. **Conservative bias.** False-positive (executing an action Megha didn't ask for) is far worse than false-negative (falling through to a conversational reply). When in doubt, return `low` or `has_action=false`. The runtime treats anything but `high` as "do not execute."

## Few-shot examples

### Example 1 — explicit mark_done with full title

<!-- private:aic-003 -->Free text: "Mark Oak Circle as done"

Output:
```json
{"has_action": true, "action_type": "mark_done", "target_text": "Oak Circle", "confidence": "high"}<!-- /private -->
```

### Example 2 — mark_done with extra words

<!-- private:aic-004 -->Free text: "Mark the Oak Circle volunteering task as done please"

Output:
```json
{"has_action": true, "action_type": "mark_done", "target_text": "Oak Circle volunteering", "confidence": "high"}<!-- /private -->
```

### Example 3 — conversational, not action

Free text: "What's on my list?"

Output:
```json
{"has_action": false, "action_type": null, "target_text": null, "confidence": "high"}
```

### Example 4 — cancel (detected, but Stage 1 won't act)

<!-- private:aic-005 -->Free text: "Cancel the Joan Miller task"

Output:
```json
{"has_action": true, "action_type": "cancel", "target_text": "Joan Miller", "confidence": "high"}<!-- /private -->
```

### Example 5 — create (now live)

Free text: "Add a task to call the pediatrician tomorrow"

Output:
```json
{"has_action": true, "action_type": "create", "target_text": "call the pediatrician tomorrow", "confidence": "high"}
```

For `create`, `target_text` is the natural-language task title (the runtime adds an owner abbreviation prefix and stores any deadline you parsed in the body). Strip leading filler ("Add a task to", "Create a", "Make me a task to", "Remind me to"). Keep deadline phrases in the title body (e.g., "tomorrow", "by Friday") — the runtime parses them downstream.

### Example 6 — ambiguous referent, medium confidence

Free text: "mark it done"

Recent Kavi messages: none, or multiple plausible referents.

Output:
```json
{"has_action": true, "action_type": "mark_done", "target_text": null, "confidence": "medium"}
```

### Example 7 — acknowledgment phrased like an action

Free text: "ok, done"

Output:
```json
{"has_action": false, "action_type": null, "target_text": null, "confidence": "high"}
```

### Example 8 — multi-action (Stage 1 only handles single; flag confidence accordingly)

Free text: "Mark these 7 tasks done"

Output:
```json
{"has_action": true, "action_type": "mark_done", "target_text": null, "confidence": "low"}
```

Rationale: Stage 1 doesn't handle multi-action; target_text=null + confidence=low signals "fall through to conversational reply" so Kavi doesn't blindly mark one of seven done.

## Output format

Return ONLY the single JSON object. No prose around it. No markdown fences in production output (the harness will tolerate fences if present, but raw JSON is preferred).

## What this prompt does NOT do

- Look up MS To Do tasks (the runtime does that after parsing).
- Execute the action (the runtime does that after lookup succeeds).
- Compose the reply (a separate composer handles post-action replies).
- Handle multi-action requests (future).
- Fuzzy-match targets to existing task titles (future).
- Parse deadlines (the runtime does that downstream).
