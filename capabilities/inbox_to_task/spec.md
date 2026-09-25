# inbox-to-task spec

Spec file lives at the repo-level path `capabilities/inbox-to-task.md` and
is loaded by the runtime at compose time (`config.yaml` →
`paths.inbox_to_task_md` → `persona_loader` / `claude_client`).

The hyphen-vs-underscore divide exists because Python packages cannot have
hyphens; this directory is `capabilities/inbox_to_task/` (importable)
while the spec keeps its hyphen filename for backward compatibility with
the runtime config and every doc that links to it.

**Open the spec at:** [`../inbox-to-task.md`](../inbox-to-task.md)
