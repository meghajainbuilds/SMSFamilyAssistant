---
description: Capture the current session into handoffs/handoff-YYYY-MM-DD.md so the next session can pick up cleanly. Uses the standard handoff template (Pickup, What changed today, Open work). If today's file exists, asks whether to overwrite or append a new section.
allowed-tools:
  - Read
  - Write
  - Bash
argument-hint: "(no arguments)"
---

# /write-handoff — capture session summary

Megha invokes this when she's ending a session. Generate a session summary from the current conversation context and write it to a dated file in `handoffs/`.

## Procedure

1. **Compute today's date.** `bash: date +%Y-%m-%d`. Capture as `today`.

2. **Compute target path.** `/Users/meghajain/Documents/HomeOS/handoffs/handoff-${today}.md`.

3. **Check if file exists.** `bash: test -f <path> && echo exists`.
  - If exists: ask Megha "Today's handoff already exists. Overwrite or append a new dated section?" Wait for her answer. If she does not pick clearly, default to append.
  - If not: proceed.

4. **Generate the summary from this session's context.** Use the template below. Pull facts from the actual conversation (what was decided, what files were edited, what is still open). Do not invent content. If something is uncertain, omit it rather than guess.

5. **Verify every carried-forward open item before listing it.** This step prevents stale items from accumulating across handoffs (the SSH-deferred and town-hall-task pattern caught 2026-05-04). For every item from a prior handoff that you'd otherwise carry into "Open work":
   - Each item MUST declare a verification check — a command, file read, or observation that proves whether it's still open.
   - Run the check before writing the handoff. Use the appropriate tool (`Bash`, `Read`, etc.).
   - If the check shows the item is **resolved**, drop it from the new handoff. Note in chat that you dropped it and why.
   - If the check shows the item is **still open**, carry it forward AND include the verification check inline next to the item (so the next session can re-verify cheaply).
   - If you cannot construct a verification check for an item (e.g., "I plan to think about X"), surface that in chat — items without checks should not live in "Open work" indefinitely. Either define a check, convert to a backlog entry, or drop.
   - New items added this session must also include a verification check when listed.

6. **Write the file** with the Write tool.

7. **Append session pattern flags to the library.** For every `[Pattern flag: <name>]` callout in this session's chat that did NOT get a `/ai-fluency` call:
   - Path: `/Users/meghajain/Documents/HomeOS/handoffs/pattern-library.md`. Create the file if missing with the header block below.
   - Format per row: `- [ ] <YYYY-MM-DD> — \`<name>\` — <one-line description from the chat where it was flagged> — source: handoff-<YYYY-MM-DD>.md`
   - Dedupe by name. If a row with the same `<name>` already exists (ticked or unticked), skip — do not add a duplicate.
   - Header block (only on file creation):
     ```markdown
     # Pattern library

     Append-only checklist of named AI/engineering patterns flagged in chat that are not yet written up in `~/Documents/ai-fluency/`. New patterns get appended by `/write-handoff`. Tick `[x]` (or remove the row) when an article is written.

     ```

8. **Confirm** in chat: "Wrote handoff to `handoffs/handoff-${today}.md`." Include a one-line note on which prior items were dropped via verification, plus a line on how many new patterns were added to the library (e.g., "Added 2 patterns to library. Skipped 1 duplicate.").

## Template (v0)

```markdown
# HomeOS handoff — YYYY-MM-DD

## Pickup for next session

Numbered, concrete actions for next-session-Megha. Start with the first thing she'd want to do tomorrow. Each item should be actionable in one step (read this file, run this command, decide between A and B).

## What changed today

Categorized subsections by topic (e.g., "Repo conventions", "household.md edits", "judgment-log/", "Code edits"). Each subsection is a bullet list of actual changes with file paths where relevant.

## Open work

Items explicitly not done. Each entry MUST include:
- **Status:** waiting on what, blocked by what, deferred to when.
- **Verify with:** a command / file read / observation that proves whether it's still open. Next session re-runs this check before re-listing the item.

Example:
- **Tailscale SSH on Kavi's Mac** — deferred permanent SSH unblock pattern. *Verify with:* `ssh kavi@100.64.0.10 'echo ok'` succeeds without password prompt.

## Style and process notes carried forward

Brief. Only include if something changed about how we work today. Otherwise reference the prior handoff.
```

## What this command does NOT do

- Write the handoff without consulting the session. Pull from the conversation, not from imagination.
- Touch any other files. Only writes to `handoffs/`.
- Run automatically. Only fires when Megha types `/write-handoff` explicitly.

## Iteration plan

The template above is v0. When Megha gives a refined template, replace the Template section in this file with hers. The Procedure section stays the same.
