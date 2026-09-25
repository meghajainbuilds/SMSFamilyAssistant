# pause-intent-classifier (new for v0.2 step 12, 2026-04-29)

Decides whether an iMessage from Megha is asking to RESUME the paused runtime, or about something else. Used only while the runtime is paused (spend cap or other guardrail trip). Replaces a hardcoded keyword match — Kavi's job is to understand intent through context, not match strings.

## Input

```json
{
  "free_text": "string — the verbatim iMessage from Megha",
  "pause_context": {
    "paused_since": "ISO timestamp UTC",
    "paused_reason": "e.g. spend_cap_exceeded",
    "paused_spend_usd": 30.42,
    "cap_usd": 30
  }
}
```

`pause_context` tells you why the runtime is paused. Use it to disambiguate phrases like "let's resume" — does Megha mean the runtime, or a thread of conversation?

## Output

Exactly one shape:

```json
{
  "intent": "resume" | "not_resume" | "ambiguous",
  "reason": "brief explanation of why you classified this way"
}
```

- `"resume"` — Megha is clearly asking to clear the runtime pause and let Kavi process emails again.
- `"not_resume"` — Megha is talking about anything else (correcting a task, replying to a Q&A, normal conversation, asking a question).
- `"ambiguous"` — you genuinely cannot tell whether she wants the runtime resumed or is talking about something else. The runtime will ask her to clarify.

## Procedure

1. **Read the free text and the pause context.** What did the runtime pause for? When? How does that frame Megha's message?

2. **Classify as `resume` if all of these hold:**
   - The message is short and primarily about state, not content (e.g. "resume", "ok continue", "you can run again", "go ahead", "unpause kavi", "ok proceed")
   - There is no reference to a specific email subject, task title, person, or topic
   - It reads as a directive to the runtime, not a comment about an item

3. **Classify as `not_resume` if any of these hold:**
<!-- private:pic-001 -->   - The message references a specific email, task, person, sender, subject ("the Joan thread," "the Boonli email," "swimming task," "Hollis's note")<!-- /private -->
   - The message is shaped like a correction ("you missed X," "X wasn't a task," "wrong owner on Y")
   - The message is a Q&A reply ("1 yes," "2 no," "yes thanks")
   - The message is conversational small-talk unrelated to runtime state

4. **Classify as `ambiguous` only when** you genuinely cannot tell. Examples:
   - "ok" with no other context
   - "let's go" — could mean resume, could be about a task
   - Reply where the topic could plausibly be either the pause or a recent task

When in doubt, prefer `not_resume` over `ambiguous` over `resume`. Failing safe means the runtime stays paused and the message routes to the correction classifier — Megha can re-text "resume" if needed.

## Examples

**Example 1 — clear resume**
Input: `"resume"`
Output: `{"intent": "resume", "reason": "single-word directive, no content reference"}`

**Example 2 — clear resume with context**
Input: `"ok you can run again, I checked the spend"`
Output: `{"intent": "resume", "reason": "directive about runtime state with explicit ack of pause reason"}`

**Example 3 — false positive risk on 'resume'**
<!-- private:pic-002 -->Input: `"let's resume that thread on Joan Miller tomorrow"`<!-- /private -->
Output: `{"intent": "not_resume", "reason": "references a specific person/thread, not the runtime"}`

**Example 4 — false positive risk on 'continue'**
Input: `"ok continue with the swimming task"`
Output: `{"intent": "not_resume", "reason": "references a specific task, shape of a Q&A or correction"}`

**Example 5 — correction during pause**
Input: `"you missed the Boonli email yesterday"`
Output: `{"intent": "not_resume", "reason": "shaped like a missed-task correction"}`

**Example 6 — ambiguous**
Input: `"ok"`
Output: `{"intent": "ambiguous", "reason": "single-token reply with no content reference; could be resume or Q&A acknowledgment"}`

**Example 7 — resume with grace**
Input: `"go ahead and run, I'll keep an eye on it"`
Output: `{"intent": "resume", "reason": "explicit directive to run, no content reference"}`
