"""coordination selection layer — intent classification and routing wrappers.

Single canonical surface for the SELECTION axis of the coordination
capability. The Investigator who opens `capabilities/coordination/selection.py`
finds the entry-point dispatcher AND the registry lookup wrappers in one
place.

Implementation lives in sibling files:
- `dispatch.py` — `_try_handle_coordination_intent` (entry point)
- `handler.py` — `start_coordination`, course-correction + reply handlers,
  registry lookups, purge

This module wraps + re-exposes those so selection-axis callers (handlers.py,
tests, debug scripts) use a single import surface.
"""

from __future__ import annotations

from typing import Any

from capabilities.coordination.dispatch import _try_handle_coordination_intent
from capabilities.coordination import handler as _h


def try_handle_coordination_intent(
    *,
    free_text: str,
    sender_handle: str | None,
    claude: Any,
    graph: Any,
    config: dict,
) -> dict[str, Any] | None:
    """Public alias. Forwards to the underscore-prefixed dispatcher in
    dispatch.py. Wrapper exists so the selection-axis surface has its own
    callable not just a re-export."""
    return _try_handle_coordination_intent(
        free_text=free_text,
        sender_handle=sender_handle,
        claude=claude,
        graph=graph,
        config=config,
    )


def start_coordination(*args, **kwargs):
    """Selection wrapper for handler.start_coordination — the kick-off entry
    once a coordination intent classifies high-confidence."""
    return _h.start_coordination(*args, **kwargs)


def handle_requester_course_correction(*args, **kwargs):
    """Selection wrapper for the requester-side course-correction handler."""
    return _h.handle_requester_course_correction(*args, **kwargs)


def handle_addressee_reply(*args, **kwargs):
    """Selection wrapper for the addressee-reply handler."""
    return _h.handle_addressee_reply(*args, **kwargs)


# Direct re-exports for callers that need the legacy names.
lookup_session_by_addressee_handle = _h.lookup_session_by_addressee_handle
lookup_session_by_requester_handle = _h.lookup_session_by_requester_handle
purge_idle_sessions = _h.purge_idle_sessions


__all__ = [
    "_try_handle_coordination_intent",
    "try_handle_coordination_intent",
    "start_coordination",
    "handle_requester_course_correction",
    "handle_addressee_reply",
    "lookup_session_by_addressee_handle",
    "lookup_session_by_requester_handle",
    "purge_idle_sessions",
]
