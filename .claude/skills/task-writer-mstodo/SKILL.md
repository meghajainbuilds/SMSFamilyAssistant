---
name: task-writer-mstodo
description: Use when the orchestrator has ONE HomeOS task object and needs to land it in the McMullen-Jain Shared Microsoft To Do list. Handles deduplication by source_email_id (via linked-resource externalId) and applies the "[?] " prefix for low-confidence tasks. Reads the stable list ID from .claude/settings.local.json (fallback .claude/settings.json) (never resolves by list name). Returns the new task ID, or "skipped" if a duplicate already exists. Single task per call; the orchestrator loops. Do NOT use for reading email, deciding owner, loop-writing multiple tasks, or filtering by content.
allowed-tools: [Read, mcp__ms365__list-todo-tasks, mcp__ms365__create-todo-task, mcp__ms365__create-todo-linked-resource]
---

# task-writer-mstodo

Adapter that turns one HomeOS task object into one row in the McMullen-Jain Shared Microsoft To Do list, via the `ms365` MCP server. Only this skill knows the surface is Microsoft To Do.

## Input

A single task object:

```json
{
  "title": "string",
  "due": "YYYY-MM-DD | null",
  "owner": "megha | max | unassigned",
  "owner_reason": "one-line justification",
  "source_email_id": "graph message id",
  "source_subject": "string",
  "source_tag": "short scannable source tag (e.g. RR-Social, RR-Teachers, Maple, Evergreen, WeekendClub)",
  "confidence": "high | medium | low"
}
```

`source_tag` is required for the at-a-glance visibility of where the task came from. Upstream (`email-to-tasks`) is responsible for populating it — typically derived from sender domain, subject prefix, or content category. Examples: `RR-Social` (subject prefix `{RR Social Event}`), `RR-Teachers` (sender `RRteachers@maplestreetschool.org`), `Evergreen` (Evergreen Health Plan correspondence), `WeekendClub` (WeekendClub recruiting). Keep tags short (≤ 12 chars), no spaces, hyphenated.

## Procedure

1. **Resolve the target list ID.** Read `mstodo_shared_list_id` from `.claude/settings.local.json` (gitignored; holds the real ID). If absent there, read it from `.claude/settings.json` (committed; holds only the placeholder `YOUR_MS_TODO_LIST_ID`). If the key is missing, empty, or still the placeholder, return:
   `{"status": "error", "message": "mstodo_shared_list_id not set in .claude/settings.local.json — run HomeOS bootstrap"}`
   Do not fall back to a name lookup. The binding is intentional.

2. **Dedupe check.** Call `mcp__ms365__list-todo-tasks` on that list ID with `top: 50`, `orderby: "lastModifiedDateTime desc"`, and `expand: ["linkedResources"]`. The response includes each task's `linkedResources` array inline. For each returned task, scan its `linkedResources` (treat absence of the field as "no linked resources for this task" and continue). If any element has `externalId` equal to this task's `source_email_id`, return:
   `{"status": "skipped", "existing_task_id": "<id>"}`

3. **Render the title.** Format optimized for at-a-glance scanning of the shared list, where Megha and Max see each other's tasks.

   Base format:
   ```
   <owner_abbrev> [<source_tag>] <title>
   ```

   - `owner_abbrev`: `MJ` for `megha`, `MM` for `max`, `??` for `unassigned`. Always present.
   - `[<source_tag>]`: bracketed source tag from input. Always present.
   - `<title>`: the input title verbatim, no transformation.
   - If `confidence == "low"` → prepend `"[?] "` to the **entire** rendered string (so `[?]` leads the line, before `MJ`).

   Examples:
   - High conf, megha, RR-Social: `MJ [RR-Social] Decide on SCT season ticket group buy ($27/pop, today only)`
   - Low conf, max, Evergreen: `[?] MM [Evergreen] Reimburse Evergreen Health Plan checks`
   - High conf, megha, WeekendClub: `MJ [WeekendClub] Send Tom Baker times to discuss WeekendClub CEO role`

4. **Build the task payload.**
  - `title`: rendered title from step 3.
  - `body`: `{ "contentType": "text", "content": "Owner: {owner} — {owner_reason}\nFrom: {source_subject}" }`
  - If `due` is non-null, `dueDateTime`: `{ "dateTime": "<due>T00:00:00", "timeZone": "America/Los_Angeles" }`. Omit otherwise.

5. **Create the task.** Call `mcp__ms365__create-todo-task` with the list ID and payload. Capture the returned task ID.

6. **Link the source email.** Call `mcp__ms365__create-todo-linked-resource` on the new task with:
  - `externalId`: `source_email_id`
  - `applicationName`: `"HomeOS inbox-scan"`
  - `displayName`: `source_subject`
  - `webUrl`: omit (consumer Outlook web URLs are unstable for personal accounts)

7. **Return success.**
   `{"status": "created", "task_id": "<new_id>"}`

## Error handling

- Any MCP call returning a non-2xx → return `{"status": "error", "message": "<MCP error>", "step": "<step number>"}`.
- Do not retry; let the caller decide.
- Do not attempt to clean up a partially-created task if step 6 fails — the linked resource is missing but the task is real; surface the error and let the caller's next dedupe attempt see it.

## What this skill does NOT do

- Read email
- Decide owner
- Loop over multiple tasks
- Filter by content / privacy
