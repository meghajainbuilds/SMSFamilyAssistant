"""test_handlers_imports.py — regression for 2026-05-07 NameError.

Originally: `_apply_qa_resolutions` (handlers.py) called `load_imessage_state`
and `save_imessage_state` at module scope, but those names were only scoped-
imported inside `_handle_resume_intent`. Every Q&A reply Megha sent crashed
with NameError; the user-visible symptom was the canned "I'm degraded right
now" fallback iMessage.

2026-06-02 (Phase 5 of the architectural refactor): the legacy
load_imessage_state / save_imessage_state shim was killed in favor of
direct per-concept calls. `_apply_qa_resolutions` now calls
`load_questions` / `save_questions` from `state_per_concept`. This test
keeps the same regression-shape: both names must be bound at
handlers.py module level.
"""

from __future__ import annotations


def test_handlers_module_imports_state_helpers():
    from kavi_runtime import handlers

    assert callable(getattr(handlers, "load_questions", None)), (
        "load_questions must be imported at handlers.py module level "
        "(referenced by _apply_qa_resolutions; scoped imports inside other "
        "functions are NOT enough)."
    )
    assert callable(getattr(handlers, "save_questions", None)), (
        "save_questions must be imported at handlers.py module level "
        "(referenced by _apply_qa_resolutions)."
    )
