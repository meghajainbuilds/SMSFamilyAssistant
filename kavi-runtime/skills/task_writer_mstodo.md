# task-writer-mstodo (kavi-runtime port)

Adapter that turns one HomeOS task object into one row in the McMullen-Jain Shared Microsoft To Do list, via the MS Graph SDK. Only this module knows the surface is Microsoft To Do.

In the runtime, this is a deterministic Python function in `graph_client.create_todo_task`, not a Claude prompt. The prompt-style spec is preserved here for reference and for consistency with the Claude Code skill.

## Input

Same shape as the upstream `email_to_tasks` task object:

```json
{
  "title": "string",
  "due": "YYYY-MM-DD | null",
  "owner": "megha | max | unassigned",
  "owner_reason": "one-line justification",
  "source_email_id": "graph message id",
  "source_subject": "string",
  "source_tag": "short scannable source tag",
  "confidence": "high | medium | low"
}
```

## Procedure

1. Resolve the target list ID from `.claude/settings.json` `mstodo_shared_list_id`. Error out if missing.

2. **Dedupe check.** Fetch recent tasks with `linkedResources` expanded. Skip if any task's `linkedResources[].externalId` equals `source_email_id`.

3. **Render the title:**
   ```
   <owner_abbrev> [<source_tag>] <title>
   ```
   `MJ` for megha, `MM` for max, `??` for unassigned. If `confidence == "low"`, prepend `[?] ` to the entire string.

4. **Build payload:**
   - `title`: rendered title
   - `body`: `{"contentType": "text", "content": "Owner: {owner} — {owner_reason}\nFrom: {source_subject}"}`
   - If `due`, `dueDateTime`: `{"dateTime": "<due>T00:00:00", "timeZone": "America/Los_Angeles"}`

5. **Create task** via Graph SDK `lists/{list_id}/tasks` POST.

6. **Link source email** via `lists/{list_id}/tasks/{task_id}/linkedResources` POST with `externalId=source_email_id`, `applicationName="HomeOS kavi-runtime"`, `displayName=source_subject`.

7. Return `{"status": "created", "task_id": "<new_id>"}`.

## Errors

Any Graph 4xx/5xx → `{"status": "error", "message": "<details>", "step": <num>}`. No retry; runtime decides.
