# kavi-persona spec

Spec file lives at the repo-level path `capabilities/kavi-persona.md` and
is loaded by the runtime at compose time via `persona_loader.py` (config
key `paths.kavi_persona_md`).

The hyphen-vs-underscore divide exists because Python packages cannot have
hyphens; this directory is `capabilities/kavi_persona/` (importable)
while the spec keeps its hyphen filename for backward compatibility with
the runtime config, the persona loader's regression test, and every doc
that links to it.

**Open the spec at:** [`../kavi-persona.md`](../kavi-persona.md)
