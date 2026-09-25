---
description: Read the most recent handoff in handoffs/ and surface its content. Use at the start of a new session to pick up where the prior session left off. Always reads the latest only; no date argument.
allowed-tools:
  - Read
  - Bash
argument-hint: "(no arguments)"
---

# /read-handoff — surface the latest handoff

Megha invokes this at the start of a new session. Find the most recent handoff file in `handoffs/`, read it into context, then summarize the pickup steps for her.

## Procedure

1. **Find the latest handoff.** `bash: ls -t /Users/meghajain/Documents/HomeOS/handoffs/handoff-*.md 2>/dev/null | head -1`. Capture the path.
  - If no file is returned: tell Megha "No handoffs found in `handoffs/`. This is a fresh start." Stop.

2. **Read the handoff file.** Use the Read tool on the captured path.

3. **Read latest labeled CSV per capability.** For each of `evals/kavi-persona/traces/` and `evals/inbox-to-task/traces/`:
   - `ls -t <folder>/eval-<scope>-labeled-week*-*.csv 2>/dev/null | head -1` — captures most-recent labeled CSV
   - Parse the date stamp from the filename (the `-<YYYY-MM-DD>` suffix). Compute days since.
   - If folder is empty, mark as "no labels yet."

4. **Count unticked patterns.** `wc -l < <(grep -c '^- \[ \]' /Users/meghajain/Documents/HomeOS/handoffs/pattern-library.md)` — just the COUNT. Do NOT list pattern rows inline in the readout (burns output tokens; Megha can open the file). If the file doesn't exist, count from the latest handoff's "Pattern flags this session" section.

5. **Surface the readout** in this exact shape:

   ```
   Handoff <date> (<N> days old).<flag stale if >3 days>

   Pickup:
   - Eval (today's must-do): persona last labeled <X> days ago (week<N>); inbox last labeled <X> days ago (week<N>). Flag "labeling overdue" if either is >7 days. Run the HTML viewer at `evals/viewer.html` if so. <one-line PM-visible context line from the latest handoff's Pickup>
   - Heads-up: <date-pinned items in the next 7 days, e.g. weekly cycle, demo, deadline>
   - If bandwidth, pick one:
     - <plain capability or work name>: <PM-visible impact>. <optional one-line tradeoff or blocker>
     - <plain capability or work name>: <PM-visible impact>.
     - <plain capability or work name>: <PM-visible impact>.
     - Or open kavi-runtime/backlog.md to triage.

   Open patterns: <N> unticked in handoffs/pattern-library.md. Run /ai-fluency <name> to article one.
   ```

   Bucket rules:
   - **Eval bucket:** always present. Reports last-labeled date per capability based on the latest CSV under `evals/<slug>/traces/`. Flag "labeling overdue" when >7 days since the last labeled CSV; mark "no labels yet" when the traces folder has no CSV. The one-line context comes from the latest handoff's Pickup section, in PM voice (what Megha notices when she runs the surface), never eng voice (what gets verified internally).
   - **Heads-up bucket:** date-pinned items in the next 7 days that don't require action today. One line each. Skip if none. **Drop items already surfaced 3+ sessions in a row** (Megha is tracking them; repeating burns attention). To detect: read the prior 3 handoffs' Pickup sections; if the same heads-up item appears in all of them, drop it from this readout. The user-facing test: "would Megha be surprised this is on the list, or has she seen it 5 times?"
   - **Bandwidth bucket:** 3-4 items from the latest handoff's "Open work" that are NOT eval, NOT heads-up, NOT permanently deferred (i.e., not gated on something out of Megha's hands). Frame each in PM voice with one-line user-visible impact. Order by leverage. **NEVER use numerical capability shorthand (Cap 1, Cap 2) or stage shorthand (Stage 2 unpark) — always use the plain capability name (kavi-coordinates, imessage-to-task) and translate engineering terms into what Megha or the family notices.** Always end with "Or open kavi-runtime/backlog.md to triage." Skip the bucket if no candidates.
   - **Open patterns line:** single line with the unticked count and the file pointer. Do NOT list rows. Drop the line if count is 0.

6. **Stand by.** Wait for Megha's direction on what to start.

## What this readout never does

- Mirror the full Open work section. Megha reads it herself.
- Print a separate eval status block. Eval is always inlined into the Pickup Eval bucket.
- List pattern rows inline. Always a count + pointer to `handoffs/pattern-library.md`.
- Use eng voice for verify or validate items. Always frame what Megha or the family notices when they run a surface, not what the runtime confirms internally.
- Use numerical capability shorthand (Cap 1, Cap 2) or stage shorthand (Stage 2 unpark). Always plain capability name + plain-language meaning.
- Repeat heads-up items already surfaced in 3+ prior readouts.
- Read all handoffs. Only the most recent.
- Take action on Pickup items automatically. Surface them, then wait.
- Edit or modify the handoff file in any way.
- Accept date arguments. Always reads the latest by filename sort.
