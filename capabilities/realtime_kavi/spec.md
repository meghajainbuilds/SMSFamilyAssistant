# realtime-kavi spec

Spec file lives at the repo-level path `capabilities/realtime-kavi.md` and
is loaded by:

- Runtime engineers reading the behavior contract.
- Cross-references from other capability docs (`kavi-persona.md`,
  `inbox-to-task.md`, `kavi-anticipates.md`).

This file is a pointer, not a duplicate. The hyphen-vs-underscore divide
exists for one reason: Python packages cannot have hyphens in their names,
so the directory is `capabilities/realtime_kavi/` (importable) while the
spec keeps its hyphen filename for backward compatibility with every doc
that links to it.

**Open the spec at:** [`../realtime-kavi.md`](../realtime-kavi.md)
