# correction-classifier (new for v0.2)

Parses a free-text iMessage from Megha into a structured correction record. Used by `imessage_received` when the incoming message doesn't match a queued Q&A question or a pending Examples-row proposal.

## Input

```json
{
  "free_text": "string — the verbatim iMessage from Megha",
  "recent_kavi_messages": [
    {"sent_at": "ISO datetime", "body": "Kavi's recent iMessage to Megha"}
  ],
  "recent_runs": [
    {"run_id": "ISO datetime", "tasks_extracted": 2, "task_titles": ["...", "..."]}
  ]
}
```

`recent_kavi_messages` and `recent_runs` give context: Megha's correction often references something Kavi recently said or did. The classifier uses them to resolve `target` (which email or task is being corrected).

## Output

Exactly one of two shapes:

**1. Correction:**
```json
{"status": "correction", "correction": {
  "type": "missed | false_positive | wrong_owner | wrong_title | wrong_source_tag | wrong_confidence",
  "target": "best-guess identifier — email subject, task title, or sender",
  "target_pattern": "generalized pattern for matching future emails (e.g., 'school vendor noreply', 'Boonli emails', <!-- private:ccl-006 -->'RR Social Event subject prefix'<!-- /private -->)",
  "reason": "why Megha is correcting this — verbatim or close paraphrase from her message",
  "new_value": "the corrected value — required for wrong_owner (megha|max), wrong_title (new title), wrong_source_tag (new tag), wrong_confidence (high|medium|low). Omit or null for missed and false_positive."
}}
```

**2. Not a correction (small-talk, ambiguous, or off-topic):**
```json
{"status": "not_correction", "reason": "brief explanation"}
```

## Procedure

1. **Read the free text + the recent Kavi messages + recent runs.** Determine: is Megha correcting Kavi, or is this off-topic?

2. **If correcting, classify the type:**
   - "you missed X" / "you should have made Y a task" → `missed`
   - "you shouldn't have done X" / "X wasn't actionable" → `false_positive`
   - "X should have been Max's task, not mine" → `wrong_owner`
   - "the title should be X, not Y" → `wrong_title`
   - "the tag should be X, not Y" → `wrong_source_tag`
   - "this should have been high confidence, not low" → `wrong_confidence`

3. **Resolve target.** Match Megha's reference to a recent run, recent task title, or recent Kavi message. If multiple matches are plausible, pick the most recent.

4. **Generalize to a target_pattern.** This is the rule the runtime will inject into future email-to-tasks calls. Examples:
   - "you missed the Boonli email" → `target_pattern: "Boonli school lunch menu"` or `"school vendor noreply"`
   - "you should have made Sephora's order email a task" → `target_pattern: "retailer order placed"`
   - The pattern should be specific enough to bind, general enough to apply to similar future emails.

5. **Capture reason verbatim** when possible — it's the human's words, often the most useful eval signal.

## Few-shot examples

### Example 1 — missed task

<!-- private:ccl-001 -->Free text: "you missed the Boonli email yesterday, that should have been a task to order Maple lunches for May"<!-- /private -->

Recent runs context: a 2026-04-28T04:45Z run that scanned 10 emails, extracted 2 tasks, no Boonli.

Output:
```json
{"status": "correction", "correction": {
  "type": "missed",
  "target": "Boonli — New menu for May 2026",
  "target_pattern": "Boonli school lunch menu (noreply@boonli.com, monthly menu emails)",
  "reason": "school vendor noreply emails are action senders despite the noreply prefix"
}}
```

### Example 2 — false positive

<!-- private:ccl-002 -->Free text: "the Lumen Arts event invite shouldn't have been a task, I'm not going"

Recent kavi messages: "I'd add: Decide on Lumen Arts Annual Gala (RSVP by May 15). 1 yes / 1 no?"

Output:
```json
{"status": "correction", "correction": {
  "type": "false_positive",
  "target": "Decide on Lumen Arts Annual Gala",
  "target_pattern": "Lumen Arts event invitations",
  "reason": "Megha not attending Lumen Arts events; skip future invitations rather than asking each time"<!-- /private -->
}}
```

### Example 3 — wrong owner

<!-- private:ccl-003 -->Free text: "the Hollis email about Theo's schedule was for Max, not me"

Output:
```json
{"status": "correction", "correction": {
  "type": "wrong_owner",
  "target": "Theo's Camp Support Schedule (Hollis)",
  "target_pattern": "Hollis BCBA correspondence",
  "reason": "Hollis BCBA is Max's lane (already a Hard rule, but Kavi misrouted)",<!-- /private -->
  "new_value": "max"
}}
```

### Example 3b — wrong title

<!-- private:ccl-004 -->Free text: "the Lumen Arts task should be 'RSVP for Lumen Arts Gala', not 'Decide on Lumen Arts Annual Gala'"

Output:
```json
{"status": "correction", "correction": {
  "type": "wrong_title",
  "target": "Decide on Lumen Arts Annual Gala",
  "target_pattern": "Lumen Arts event invitations",
  "reason": "Megha prefers RSVP-framed titles for event invites",
  "new_value": "RSVP for Lumen Arts Gala"<!-- /private -->
}}
```

### Example 3c — wrong confidence

<!-- private:ccl-005 -->Free text: "the Joan Miller VP role email should have been high confidence, not low"

Output:
```json
{"status": "correction", "correction": {
  "type": "wrong_confidence",
  "target": "Decide on VP of Product role from Joan Miller",<!-- /private -->
  "target_pattern": "recruiter outreach for VP/Director Product roles in target companies",
  "reason": "Megha is actively interviewing; recruiter InMails for target roles are high-confidence by definition",
  "new_value": "high"
}}
```

### Example 4 — small talk, not correction

Free text: "thanks!"

Output:
```json
{"status": "not_correction", "reason": "acknowledgment, not a correction or instruction"}
```

### Example 5 — ambiguous

Free text: "that's wrong"

Output:
```json
{"status": "not_correction", "reason": "ambiguous; cannot resolve target. Runtime should ask Megha to clarify."}
```

## What this prompt does NOT do

- Decide whether to update `capabilities/inbox-to-task.md` Examples (that's the correction-pattern-check job after 2+ same-pattern corrections accumulate).
- Send acknowledgment iMessages (the runtime does that immediately).
- Apply corrections to the next email-to-tasks call (the runtime injects them at call time).
