---
name: email-to-tasks
description: "Use when the orchestrator has ONE email payload and needs to decide — should this become a task or be skipped. Judgment-first: applies Hard rules from capabilities/inbox-to-task.md as deterministic short-circuits, then uses LLM reasoning over Examples & Q&A learned patterns from the same file. Reads household.md for roster/identity. Returns either a task (with confidence high/medium/low — low signals \"queue iMessage Q&A to confirm\") or a skipped record (hard-skip rule OR LLM judged not actionable). Single email per call; the orchestrator loops. Do NOT use for writing to MS To Do (that is task-writer-mstodo), for scanning multiple emails, or for thread/history analysis (v0 reads the latest message only)."
allowed-tools: null
---
# email-to-tasks

Per-email judgment skill. Given one email plus household identity (`household.md`) and capability behavior (`capabilities/inbox-to-task.md` Behavior section), decide whether to create a task and (if so) extract title + owner + confidence. Architecture: **judgment-first.** Hard rules short-circuit reasoning; everything else is LLM judgment using Examples & Q&A learned patterns as few-shot context.

The orchestrator (`/inbox-scan`) calls this skill per email and routes the output: `task` → `task-writer-mstodo` (or pending file if preview gate is on); `skipped` → log only. Tasks with `confidence: low` are flagged for iMessage Q&A confirmation in Step 1+ of Chief of Staff onboarding.

## Input

One normalized email object from the orchestrator:

```json
{
  "id": "graph message id",
  "subject": "string",
  "from_name": "string",
  "from_address": "email@domain",
  "to": ["email@domain", "..."],
  "received": "ISO datetime (e.g. 2026-04-21T23:02:08Z)",
  "body_text": "plain text body, truncated to ~2000 chars if long",
  "thread_state": {
    "user_replies": [
      {"sent_at": "ISO datetime", "body_preview": "≤300 chars from Megha's reply", "to": ["recipient@domain"]}
    ],
    "thread_message_count": 1
  }
}
```

`thread_state.user_replies` is the list of sent messages from Megha in the same conversation that were sent AFTER `received`. Empty array = first-touch email, no reply yet. Used in step 7 to avoid creating stale tasks (the 2026-04-27 WeekendClub false-positive failure mode). `thread_message_count` is total messages in the conversation across inbox+sent.

## Output

Exactly one of two shapes:

**1. Task — LLM judged actionable:**
```json
{"status": "task", "task": {
  "title": "string — one line, scannable, deadline embedded in parens if email implies one",
  "due": "YYYY-MM-DD | null — set ONLY for hard action deadlines, not literal email dates",
  "owner": "megha | max | unassigned",
  "owner_reason": "one-line justification: cite the Example pattern, the Q&A learned entry, or the LLM's reasoning",
  "source_email_id": "...",
  "source_subject": "...",
  "confidence": "high | medium | low"
}}
```

**Title format (updated 2026-04-29 evening):** one line, ≤80 chars, captures the actual ask. The runtime renders titles as `<owner_abbrev> <title>` (e.g., `MJ Decide on city council town hall (Mon May 4, 6pm)`). Low-confidence tasks get `[?]` prepended. **No bracketed source-tag prefix any more** — the title body should make the sender obvious without needing a tag. If the email implies a deadline, embed it in parens at the end of the title body; otherwise omit.

**Due-date semantics (updated 2026-04-29 evening):** `due` is null by default. Set it ONLY when the email explicitly states a hard action deadline AND the task is to act by that date. Do NOT set `due` for literal email dates that aren't deadlines.

**`source_tag` is deprecated** as of 2026-04-29 evening. The runtime no longer renders it. Skill output should omit it.

`confidence: low` is the orchestrator's signal to queue an iMessage Q&A asking Megha to confirm.

**2. Skipped — Hard skip rule fired OR LLM judged not actionable:**
```json
{"status": "skipped",
  "reason": "brief description (e.g., 'Hard skip rule: parent coffee invitation' OR 'LLM judged: marketing promo, no specific ask')",
  "email_id": "...",
  "subject": "..."}
```

## Procedure

1. **Read both files in full.** Two sources, two distinct concerns:
  - **`household.md`** — identity context: roster, email identities, iMessage handles, single-owner accountability principle. Used for "who is the email addressed to / who handles what role." Thin by design.
  - **`capabilities/inbox-to-task.md`** Behavior section — capability behavior:
    - **Hard rules (always-true short-circuits)** — apply BEFORE any LLM judgment
    - **Examples & known patterns** — illustrative cases the LLM reasons from
    - **Q&A learned patterns** — patterns Megha confirmed in past iMessage Q&A; treat as authoritative training data

2. **Apply Hard rules first.** Walk the Hard rules subsection in `capabilities/inbox-to-task.md`:
  - **Mailbox-domain hard rules** (BCBA → Max; Medical → Megha) — if a hard rule fires, the LLM must NOT override based on email content. Extract title, set due if explicit, return `task` with `confidence: high` and `owner_reason` citing the hard rule.
  - **Maple Street templating rules** — match in the order listed. First **Skip** match → return `skipped` with the rule cited. First **Create** match → render the Title template (substitute `{received_date_short}` with month + day like `Apr 21`, no year, no leading zero on day), use the rule's Owner, extract `due` if present, return `task` with `confidence: high`.

3. **If no Hard rule fires: use LLM judgment.** Reason from the Examples table and Q&A learned patterns subsections of `capabilities/inbox-to-task.md`, using identity context from `household.md` for owner inference:
  - **Is this email actionable?** Specific request, deadline, decision needed, form to fill, RSVP, payment, follow-up. NOT actionable: pure confirmations, marketing, autoresponders, FYI-only, newsletters not already templated.
  - **If yes:** extract title (imperative phrasing where possible, ≤80 chars), infer owner from the Examples + Q&A learned patterns, score confidence per criteria below.
  - **If no:** return `skipped` with reason explaining the judgment ("LLM judged: marketing promo, no specific ask").

4. **Owner inference (when no hard rule fired):**
  - **Strong example match** (e.g., taxes email → Max per Examples table) → use that owner; confidence high or medium.
  - **Q&A learned pattern match** → use that owner; confidence high (Megha confirmed it).
  - **Reasoning by analogy** (no direct example, but a similar pattern exists) → make a call; confidence medium or low.
  - **Genuinely ambiguous** → make a best-guess and use `confidence: low` (orchestrator queues Q&A); `owner_reason` should explain "best guess based on X; recommend Q&A confirmation."

5. **Title extraction (updated 2026-04-29 evening):**
  - Start from the email's primary ask (imperative when possible, e.g., "Pay Northgate Medical past-due balance").
  - One line, ≤80 chars.
  - For invitations/RSVPs: use "Decide on [event name]" pattern.
  - For meeting recaps: extract the literal assignment ("Get back to Kate by Tuesday").
  - **If the email implies a deadline, embed it in parens at the end:** `Decide on city council town hall (Mon May 4, 6pm)`, `RSVP for Lumen Arts Gala (by May 15)`, `Pay Bayview tennis fee ($120 by Fri)`. Otherwise omit the parens.
  - **No source-tag bracket.** The runtime drops it. The title body should make the sender/topic obvious on its own.

6. **Due-date extraction (updated 2026-04-29 evening):**
  - **Default to `due: null`.** Most emails don't have a true action deadline.
  - Set `due` to a YYYY-MM-DD only when the email **explicitly states a hard deadline AND the task is to act by that date** (RSVP by May 15 → `due: 2026-05-15`; payment due April 30 → `due: 2026-04-30`).
  - Do NOT set `due` for literal email dates that aren't deadlines (a delivery email dated Apr 30 is not "do this Apr 30").
  - The deadline ALSO appears in the title parens per step 5; `due` is for the MS To Do `dueDateTime` field.
  - Infer year from `received` if the date is ambiguous.

7. **Source tag derivation — DEPRECATED 2026-04-29 evening.** Skill output should omit `source_tag`. Runtime no longer renders it. Skip to step 8.

   *(Original spec, deprecated):*

  Walk in order; first match wins:
  - **Hard rule fired** — tag is determined by the rule's domain:
    - Maple templating rules → `Maple`
    - Hollis BCBA → `BCBA`
    - Medical (mychart, billing, Northgate Medical, etc.) → `Medical`
  - **Examples / Q&A learned pattern matched** — derive tag from the pattern's category:
    - `{RR Social Event}` subject prefix → `RR-Social`
    - `RRteachers@maplestreetschool.org` sender → `RR-Teachers`
    - `{All School Email}` non-templated → `Maple`
    - Evergreen Health Plan correspondence → `Evergreen`
    - Retailer order/delivery → use the retailer name (`Sephora`, `Amazon`)
    - LinkedIn jobs → `LinkedIn` (skip path; tag still set for log clarity)
  - **Novel sender, no pattern match** — derive from sender display name:
    - Take `from_name` (or fall back to the part of `from_address` before the `@`).
    - Strip "Newsletter", "Support", "Team", "noreply", "no-reply" suffixes.
    - Take the first 1–2 capitalized words. Pascal-case if multi-word, hyphenate only if naturally hyphenated.
    - Examples: `Tom Baker` → `TomBaker` → trim to ≤12 chars (already ok). `Lumen Arts events` → `LumenArts`. `Wispr Support` → `Wispr`. `events@brightpathsearch.com` (no display name) → `Brightpathsearch` (domain root, capitalized).
  - **Constraints:** ≤12 chars, no spaces, no brackets, no slashes. Hyphens allowed. Letters and digits only otherwise.
  - **Megha's manual edits in MS To Do are the canonical correction signal** — when she renames `[TomBaker]` to `[WeekendClub]`, that's the truth. Capturing those edits back into a learned-tags corpus is v0.2 work; today the LLM just makes the best derivation possible.

8. **Thread-state reasoning (v0.1 — added 2026-04-28).** After steps 3–7 produce a candidate task, BEFORE returning, check `thread_state.user_replies`. This step prevents the stale-task false positive surfaced in the 2026-04-27 WeekendClub failure (Megha had replied to Tom Baker 3h before the scan; Run 1 still extracted "Send Tom Baker times" as a task because the skill saw only the latest inbound message).

   Reasoning, in order:

   - **No replies in thread (`user_replies: []`)** → no change. Return the candidate task as-is.

   - **One or more replies AFTER the inbound message** → reason about whether each reply addressed the original ask:
     - **Reply addressed the ask** (e.g., inbound asks "send me times"; reply contains a list of times or a calendar link or an explicit "Yes, here's…") → return `skipped` with reason: `"Thread state: Megha replied at <sent_at> and the reply appears to address the ask — task would be stale. Reply preview: '<first 100 chars>'."` `email_id` and `subject` per the standard skipped shape.
     - **Reply was a holding response** (e.g., "I'll get back to you tomorrow", "Let me think about this", "Will check with Max") → KEEP the task but update title to reflect the commitment Megha made (e.g., "Get back to Tom Baker with WeekendClub times (committed Tue)") and downgrade `confidence` to `medium` if it was `high`. `owner_reason` should note the holding reply.
     - **Reply was off-topic or about a different point** (the inbound has multiple asks; reply only addressed one) → KEEP the task, narrow the title to the unaddressed portion. Confidence stays as derived in step 4.
     - **Ambiguous** → KEEP the task at `confidence: low` (triggers iMessage Q&A). `owner_reason`: `"LLM judgment: Megha replied at <sent_at> but it's unclear whether the reply addressed the ask. Recommend Q&A confirmation."`

   - **Conservatism principle:** when in doubt, KEEP the task at `low` confidence. A false-positive task in the [?] queue is recoverable; a missed task is not. The Q&A loop catches the false positive cheaply.

   - **Cost note:** thread reasoning adds ~50 tokens of context per email. Negligible against the per-email base cost.

## Confidence scoring

| Level | When |
| --- | --- |
| **high** | Hard rule fired (the highest-confidence path) OR very close match to an Example or Q&A learned pattern AND title/due extraction was unambiguous. |
| **medium** | Reasoning by analogy from a related pattern; title or due required interpretation. |
| **low** | Novel pattern or borderline judgment. **Triggers ****`[?]`**** prefix in MS To Do AND queues iMessage Q&A to Megha.** Megha's answer becomes a new Q&A learned entry. |

## Few-shot examples

### Example 1 — Medical bill (Hard rule, Megha, high)

Input:
```json
{"subject": "Action Needed: Your balance is now past due",
 "from_address": "mychart.donotreply@northgatemedical.org",
 "to": ["megha@example.com"],
 "received": "2026-04-23T01:33:17Z",
 "body_text": "Northgate Medical\nAccount ending in 5190\nHi Megha,\nYou have an outstanding balance from your previous statement which is now past due.\nAmount Due\n$40.92\nMake a payment..."}
```

Output (Hard rule fired — medical bill → Megha, deterministic):
```json
{"status": "task", "task": {
  "title": "Pay Northgate Medical past-due balance ($40.92)",
  "due": null,
  "owner": "megha",
  "owner_reason": "Hard rule fired: medical provider sender + billing signal → Megha (no LLM judgment override permitted)",
  "source_email_id": "...",
  "source_subject": "Action Needed: Your balance is now past due",
  "source_tag": "Medical",
  "confidence": "high"
}}
```

### Example 2 — BCBA invoice (Hard rule, Max, high)

Input:
```json
{"subject": "Re: Theo's Camp Support Schedule",
 "from_address": "clairemorgan@hollisbehavioralconsulting.com",
 "to": ["megha@example.com", "max@example.com"],
 "received": "2026-04-16T15:28:57Z",
 "body_text": "Hi Megha and Max,\nI wanted to follow up regarding your invoice to see if you have any questions or need any support..."}
```

Output (Hard rule fired — Hollis domain → Max, deterministic):
```json
{"status": "task", "task": {
  "title": "Respond to Claire re: Theo BCBA camp invoice/payment plan",
  "due": null,
  "owner": "max",
  "owner_reason": "Hard rule fired: sender @hollisbehavioralconsulting.com → Max (Hollis BCBA/BT services lane)",
  "source_email_id": "...",
  "source_subject": "Re: Theo's Camp Support Schedule",
  "source_tag": "BCBA",
  "confidence": "medium"
}}
```

### Example 3 — Maple newsletter (Hard templating rule, Megha, high)

Input:
```json
{"subject": "{All School Email} {School News} News Blast for April 21, 2026",
 "from_address": "office@maplestreetschool.org",
 "to": ["schoolnews@maplestreetschool.org"],
 "received": "2026-04-21T23:02:08Z",
 "body_text": "Hello Maple Street School families..."}
```

Output (Hard rule fired — Maple templating):
```json
{"status": "task", "task": {
  "title": "Maple newsletter — Apr 21",
  "due": null,
  "owner": "megha",
  "owner_reason": "Hard rule fired: Maple templating — office@maplestreetschool.org News Blast → Megha (title templated)",
  "source_email_id": "...",
  "source_subject": "{All School Email} {School News} News Blast for April 21, 2026",
  "source_tag": "Maple",
  "confidence": "high"
}}
```

### Example 4 — RR parent coffee (Hard skip rule)

Input:
```json
{"subject": "{RR Social Event} RR parent coffee this Friday!",
 "from_address": "laura.hayes@gmail.com",
 "to": ["socialsrr@maplestreetschool.org"],
 "received": "2026-04-22T18:55:22Z",
 "body_text": "Welcome back from Spring Break, RR fam!\nWould love to see all your lovely faces at the After-Drop-Off-Coffee-Thing this Friday..."}
```

Output:
```json
{"status": "skipped",
 "reason": "Hard skip rule: subject contains 'parent coffee' (Maple social, deterministic skip)",
 "email_id": "...",
 "subject": "{RR Social Event} RR parent coffee this Friday!"}
```

### Example 5 — Wispr support reply (LLM judgment, novel pattern, low confidence)

Input:
```json
{"subject": "Re: Wispr promo code not working",
 "from_address": "support@wispr.ai",
 "to": ["megha@example.com"],
 "received": "2026-04-22T22:53:58Z",
 "body_text": "Hi Megha,\nThe promo code is valid and it's restricted to `firsttimetransaction: false`... you'll need to go through the checkout..."}
```

Output (no Hard rule; LLM judges actionable but pattern is novel — flag for Q&A):
```json
{"status": "task", "task": {
  "title": "Try Wispr promo code via checkout (per support reply)",
  "due": null,
  "owner": "megha",
  "owner_reason": "LLM judgment: novel sender pattern, but content is a clear actionable instruction directed to addressed-to (Megha). Best guess; recommend iMessage Q&A confirmation.",
  "source_email_id": "...",
  "source_subject": "Re: Wispr promo code not working",
  "source_tag": "Wispr",
  "confidence": "low"
}}
```

`confidence: low` triggers `[?]` prefix in MS To Do AND queues an iMessage Q&A: "Should I keep treating Wispr support replies as your tasks? Reply yes/no." Megha's answer crystallizes into Q&A learned patterns.

### Example 6 — Airline promo (LLM judgment, not actionable)

Input:
```json
{"subject": "Last chance: 30% off all fares through May 1",
 "from_address": "deals@alaskaair.com",
 "to": ["megha@example.com"],
 "received": "2026-04-22T14:00:00Z",
 "body_text": "Book by May 1 to save big on spring and summer travel..."}
```

Output:
```json
{"status": "skipped",
 "reason": "LLM judged: promotional content with no specific request or decision directed at the household. The May 1 deadline is marketing urgency, not an ask.",
 "email_id": "...",
 "subject": "Last chance: 30% off all fares through May 1"}
```

### Example 7 — Lumen Arts Gala invitation (LLM judgment from Example pattern, low confidence first time)

Input:
```json
{"subject": "You're invited: Lumen Arts Annual Gala — RSVP by May 15",
 "from_address": "events@lumenarts.org",
 "to": ["megha@example.com"],
 "received": "2026-04-25T16:00:00Z",
 "body_text": "Dear Megha,\nWe'd love to see you at our Annual Gala on June 8..."}
```

Output (Examples table has "Invitations / event RSVPs → addressed-to, confidence low"):
```json
{"status": "task", "task": {
  "title": "Decide on Lumen Arts Annual Gala (RSVP by May 15)",
  "due": "2026-05-15",
  "owner": "megha",
  "owner_reason": "Example pattern: Invitations & event RSVPs → addressed-to (Megha). Title uses 'Decide on [event]' pattern.",
  "source_email_id": "...",
  "source_subject": "You're invited: Lumen Arts Annual Gala — RSVP by May 15",
  "source_tag": "LumenArts",
  "confidence": "low"
}}
```

`confidence: low` per the Examples table convention for invitations (the decision is discretionary). After Megha confirms via Q&A whether Lumen Arts specifically should always be `low` or upgraded based on her relationship to the org, that resolution becomes a Q&A learned entry.

### Example 8 — WeekendClub follow-up, user already replied (thread-state skip, v0.1)

This is the canonical thread-aware-reasoning case. The 2026-04-27 false positive that drove the v0.1 patch.

Input:
```json
{"subject": "Thanks & WeekendClub CEO",
 "from_address": "tom@brightpathsearch.com",
 "to": ["megha@example.com"],
 "received": "2026-04-26T23:37:27Z",
 "body_text": "Hi Megha, sharing the JD for the WeekendClub CEO role here. Send me some good times for you...",
 "thread_state": {
   "user_replies": [
     {"sent_at": "2026-04-27T18:21:27Z",
      "body_preview": "Hi Tom, Thanks for bringing me the Weekend Club opportunity. I gave it real consideration before coming back to you. What resonated: the multi-sided marketplaces and creator monetization work align well with my experience...",
      "to": ["tom@brightpathsearch.com"]}
   ],
   "thread_message_count": 2
 }}
```

Output (steps 1–7 produce candidate task; step 8 detects user reply addresses the ask → skip):
```json
{"status": "skipped",
 "reason": "Thread state: Megha replied at 2026-04-27T18:21:27Z and the reply appears to address the ask (substantive response with consideration of the opportunity). Task would be stale. Reply preview: 'Hi Tom, Thanks for bringing me the Weekend Club opportunity. I gave it real consideration before coming back to you...'",
 "email_id": "...",
 "subject": "Thanks & WeekendClub CEO"}
```

### Example 9 — Holding response, keep task with downgraded confidence (thread-state, v0.1)

Input:
```json
{"subject": "Coffee next week?",
 "from_address": "kate.investor@example.com",
 "to": ["megha@example.com"],
 "received": "2026-04-26T15:00:00Z",
 "body_text": "Megha, would love to grab coffee next week to talk about the AI-native CEO search I'm advising on. What's a good day?",
 "thread_state": {
   "user_replies": [
     {"sent_at": "2026-04-26T22:00:00Z",
      "body_preview": "Kate! Great to hear from you. Let me check my calendar and get back to you tomorrow with 2-3 options.",
      "to": ["kate.investor@example.com"]}
   ],
   "thread_message_count": 2
 }}
```

Output (steps 1–7 produce candidate "Send Kate coffee times"; step 8 sees holding reply → keep task, refine title, downgrade confidence):
```json
{"status": "task", "task": {
  "title": "Get back to Kate with 2-3 coffee times for next week (committed Sun)",
  "due": null,
  "owner": "megha",
  "owner_reason": "LLM judgment: explicit ask in body, addressed to Megha. Thread state: Megha replied with a holding response committing to send times. Title refined to reflect commitment; confidence downgraded from high to medium since Megha's reply already changed the shape of the action.",
  "source_email_id": "...",
  "source_subject": "Coffee next week?",
  "source_tag": "Kate",
  "confidence": "medium"
}}
```

## What this skill does NOT do

- Read multiple emails (single email per invocation; orchestrator loops)
- Write to MS To Do (that's `task-writer-mstodo`)
- Send iMessage notifications or Q&A (the orchestrator handles iMessage I/O)
- Aggregate "Rule Candidates" across emails (the orchestrator does this from `confidence: low` task patterns)
- Filter by privacy / sender block list (deferred to v1)
- Fetch thread/sent-items data itself — the orchestrator builds `thread_state` and passes it in. The skill reasons over it but doesn't query Graph. (Updated 2026-04-28; v0 read latest message only, v0.1 reads thread state passed in.)
- Update Q&A learned patterns (the orchestrator does this in `capabilities/inbox-to-task.md` Q&A learned patterns subsection when an iMessage Q&A resolves)
