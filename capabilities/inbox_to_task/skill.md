# inbox-to-task skill pointer

The BEHAVIOR-only skill file lives at `kavi-runtime/skills/email_to_tasks.md`.
The runtime loads it via `kavi_runtime/claude_client.run_email_to_tasks` ↔
`persona_loader` ↔ `config.yaml.paths.inbox_to_task_md`.

This pointer exists so the Investigator entry-point map is complete: there
is a `skill.md` slot in every capability directory. The hyphen-named file
keeps living under `kavi-runtime/skills/` because the deploy script
rsyncs that directory to Kavi.

**Open the skill at:** `kavi-runtime/skills/email_to_tasks.md`
