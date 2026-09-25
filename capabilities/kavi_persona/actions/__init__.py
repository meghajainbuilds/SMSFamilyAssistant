"""kavi-persona actions — what Kavi does after an inbound iMessage.

Action verbs today: mark done, snooze, create task. Phase 4 (2026-06-02)
physical move is partial: `title_edits.py` holds the title-editing
helpers; the larger action layer (`_try_handle_action_intent`,
`_handle_create_task_verb`, etc.) still lives in `kavi_runtime/handlers.py`
pending follow-on Phase 4 work.

The re-exports below were removed 2026-06-02 because they caused a
circular import: `kavi_runtime.handlers` imports from
`capabilities.kavi_persona.actions.title_edits`, and the eager imports
here ran first → ImportError. New code that needs the action handlers
should import directly from `kavi_runtime.handlers` until the physical
move catches up.
"""
