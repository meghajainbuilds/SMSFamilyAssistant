"""kavi-persona — everything Kavi says or does as a person.

Spec: `capabilities/kavi-persona.md`.

`capability_type: [meta, generative, two-way]`.

This is the largest capability. It owns:
- periodic_summary (morning digest + 9 PM rollup composer + selection)
- iMessage Q&A loop (questions + exchanges + replies)
- action handlers (mark done / snooze / create_task)
- action intent classification + target matching
- pending clarification resolver
- post-action reply composer
- action-clarifying reply composer
- Q&A question composer
- weekly self-check composer
- pause intent + skip-correction handling

Per-capability directory created 2026-06-02 (Phase 4 of the architectural
refactor). Re-exports point at canonical homes under `kavi_runtime/`.
"""
