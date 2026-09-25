---
name: scheduled-reminders
status: proposed
capability_type:
  - agentic
  - two-way
owner: Megha (PM) + Claude (engineer)
last_updated: 2026-05-08
---
# scheduled-reminders

## TL;DR

When Megha or Max tells Kavi about a future commitment (dinner reservation, school pickup, doctor's appointment, departure time), Kavi proactively reminds the relevant household member at user-specified lead times so nobody forgets. Time-aware coordination that complements the existing real-time `kavi-coordinates` flow.

**Status: proposed.** Seed example captured 2026-05-08 from production. Phase 0a (template + interview) pending. Not built.

## Why now

- **What's broken or at risk:** today's `kavi-coordinates` capability handles real-time asks ("ask Max if he can do dinner tonight") but has no concept of *time-shifted* asks. Production trace 2026-05-08T17:49Z showed Megha asking Kavi to "remind Max that he should be home by 5 PM two hours in advance" — Kavi misread the lead-time ask as a real-time coordination, sent Max the reminder *immediately* (10:50 AM PT), then did nothing else. The 2-hour-pre cue never fired. Megha's actual ask silently failed.
- **Who feels it and when:** the household member who needs the reminder (typically Max for evening commitments, Megha for morning ones) misses or scrambles for time-bound commitments. Frequency: at least 2-3x/week based on dinner/event coordination patterns. Cost when it fails: late arrivals, missed reservations, schedule churn.
- **Why now (Doshi LNO):** Leverage. Compounds with `kavi-coordinates` and inbox-to-task — every email-extracted "due tomorrow at X" can route through this surface for proactive reminders rather than passive task entries.

## Open questions

These are the questions that need a PM decision before Phase 0a interview:

- **Channel.** Reminders go via iMessage to the addressee? Or shared chat? What about email if iMessage fails (today's Outlook fallback path delivered via email when BlueBubbles couldn't verify)?
- **Confirmation back to requester.** When the reminder fires, does Megha get a "sent the 3 PM reminder to Max" confirmation? At reminder time, or only on outcome?
- **Escalation if reminder unacknowledged.** If Max doesn't reply to the 4 PM reminder, does Kavi escalate to Megha? Re-nudge Max? Stop?
- **Lead-time grammar.** Megha said "two hours in advance." How does Kavi parse "two hours before X" vs "at 3 PM" vs "halfway between now and X"? LLM judgment, but the spec needs the canonical phrasings to support.
- **Cancellation.** If Megha later texts "actually, dinner's off," does the scheduled reminder auto-cancel? How is the link between the original commitment and the reminder maintained?
- **Multi-reminder vs single reminder.** The seed example asked for two reminders (3 PM + 4 PM). Is that the user's call each time, or a default cadence (e.g., 2hr-pre + 1hr-pre)?
- **Time zone.** Default Pacific (Megha is in Seattle). What about travel-day commitments?
- **Storage.** Where do scheduled reminders live? `imessage_state.json`? Separate `scheduled_reminders.jsonl`? In MS To Do as a future-dated task with reminder=on?

## Behavior (the spec) — DRAFT FROM SEED EXAMPLE

This section captures Megha's founding example faithfully. Not yet evaluated; not yet implemented.

### Few-shot examples

**Example 1 — Dinner reservation with proactive reminder (seed, 2026-05-08)**

**Setup:** Megha sends Kavi:
<!-- private:srm-001 -->> "Let Max know that I was able to grab reservation for 5:45 for Lakeside Tavern. I'll plan on getting the kids home by 4:30 if you reach home by five Ish, that's fine."<!-- /private -->

Followed by:
> "By the way, remind Max that he should be home by 5 PM two hours in advance"

**Expected Kavi behavior:**

1. **Immediate (within 60s):** message Max via iMessage:
<!-- private:srm-002 -->   > "Hey Max, Megha booked dinner for us at Lakeside Tavern at 5:45 PM tonight. Plan to be home by 5 PM so we can leave on time. Megha will have the kids home by 4:30."
2. **Schedule two future reminders to Max** (relative to the 5 PM target time):
   - 3 PM PT: "Hey Max, dinner at 5:45 PM tonight at Lakeside Tavern — heads up, should leave home around 5 PM. Are you on track?"<!-- /private -->
   - 4 PM PT: "Hey Max, ~1 hour to dinner pickup. Confirming you'll be home by 5?"
3. **At reminder fire time:** the LLM composes the actual reminder text in voice (no template). Composer is given the original commitment context + the time remaining + Max's most-recent reply (if any).
4. **Confirm back to Megha** when each reminder fires (single message; not chatty): "Sent Max the 3 PM heads-up. Reminding again at 4 PM."

**Example 2 — Same flow, two days out (variant Megha mentioned)**

If Megha's reservation is for, say, Friday at 5:45 PM and today is Wednesday: Kavi schedules the reminders for Friday at 3 PM + 4 PM (not Wednesday). Time-of-day relative to the commitment, not relative to "now."

### Good outputs

- Reminder text quotes the actual commitment Megha originally described (date, time, place, who's involved).
- Reminder fires within ±2 minutes of the scheduled time.
- Reminder is in Kavi's voice, LLM-composed, not a template (kavi-persona Principle 7).
- Megha receives a single confirmation per reminder firing, not chatty status.
- If Max replies to the reminder ("got it" / "running late"), Kavi reads the reply and either marks the reminder cycle done or escalates per the cancellation/escalation rules (TBD).

### Bad outputs / failure modes

- **Silent failure (the production bug from 2026-05-08):** ask interpreted as real-time coordination, fired once at request time, scheduled cue never created.
- **Lost-context reminder:** reminder fires saying "heads up about your thing" without naming the actual commitment.
- **Reminder fires after the commitment time has passed.**
- **Reminder fires for a cancelled commitment** (no link maintained between cancellation and scheduled cue).
- **Duplicate reminders** (same commitment scheduled twice because the runtime didn't recognize it had already been scheduled).
- **Time-zone errors** (3 PM UTC instead of 3 PM PT).

### Action-claim correspondence

**Required per template rules** (this capability is `agentic` + `two-way`).

1. **Action verbs the composer can produce:** `scheduled, queued, sent, reminded` (canonical list from kavi-persona.md Principle 7 + capability-specific `reminded`).
2. **Tool result that grounds each verb:**
   - `scheduled` / `queued` — backed by a successful append to whatever durable scheduled-reminders store the runtime adopts (TBD: `scheduled_reminders.jsonl` on Kavi's Mac OR MS To Do future-dated task with reminder=on).
   - `sent` — backed by BlueBubbles `verified=True` send result (same pattern as `coordination_addressee_reach`).
   - `reminded` — same as `sent` plus a logged `reminder_fired` event tying back to the original commitment ID.
3. **Runtime behavior when the tool result is missing:** drop the verb claim into `alert_fallback`, per the kavi-persona.md "I caught myself" pattern. G-A1 enforces this at runtime.

## Phase 0a — what comes next

**Before any code is written**, per `feedback_interview_template_first.md`:

1. Resolve the eight open questions above via interview-driven discovery (the `Templates/PROMPT - Discovery interview.md` flow).
2. Update `Templates/HomeOS-capability.md` if any new template sections are needed for the time-shifted-action pattern (e.g., a new section for "trigger condition + lead time" in the spec).
3. Decide storage shape: where do scheduled reminders live, and how do they survive runtime restarts?
4. Eval surface: how do we measure that this works? Likely a `reminder_fired_on_time` rate (within ±2 min of target) + a `commitment_recall` rate (% of scheduled reminders that fire) + a `cancellation_consistency` rate (% of cancelled commitments whose reminders also got cancelled).

Then, and only then, write the spec, build the capability, and add to evals.

## Changelog

- 2026-05-08 — Capability proposed. Seed example captured from production trace 2026-05-08T17:49Z (dinner reservation + 2-hour-pre reminder ask). Megha confirmed the desired behavior in chat. Phase 0a queued.
