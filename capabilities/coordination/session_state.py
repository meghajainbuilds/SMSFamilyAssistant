"""coordination session state — read/lookup/purge surface.

Single canonical surface for the SESSION-STATE axis. The in-process session
registry + idempotency hash table live in `capabilities/coordination/handler.py`
(it owns mutation). This module wraps the read-side helpers (lookup, purge)
so callers that need session-state visibility have a single import surface.

Persistence (added 2026-06-10, after the 2026-06-03 deploy restart orphaned
a live teacher-meeting session): sessions are write-through persisted to the
per-concept state file `coordination_sessions.json` (sibling of
questions.json etc.; helpers in `kavi_runtime/state_per_concept.py`, atomic
writes with a per-writer-unique tmp filename). The first lookup after a
process restart lazy-loads the file; closed sessions and sessions past the
24h idle timeout are pruned at load. Pass `config` to every lookup so the
hydration can resolve the state path.
"""

from __future__ import annotations

from typing import Any

from capabilities.coordination import handler as _h


def lookup_session_by_addressee_handle(
    addressee_handle: str, config: dict | None = None,
) -> dict[str, Any] | None:
    """Find an active session waiting on a reply from the given handle.
    Consults persisted state (lazy-loaded) when `config` is provided."""
    return _h.lookup_session_by_addressee_handle(addressee_handle, config=config)


def lookup_session_by_requester_handle(
    requester_handle: str, config: dict | None = None,
) -> dict[str, Any] | None:
    """Find an active coordination session whose REQUESTER is the given handle.
    Consults persisted state (lazy-loaded) when `config` is provided."""
    return _h.lookup_session_by_requester_handle(requester_handle, config=config)


def purge_idle_sessions(
    idle_timeout_sec: int | None = None, config: dict | None = None,
) -> int:
    """Scan the registry; pop sessions older than the timeout. Returns the
    count of sessions removed. Used by the scheduler tick.

    When `idle_timeout_sec` is None, the handler's default 24h ceiling applies.
    When `config` is provided, the purge writes through to the persisted file.
    """
    if idle_timeout_sec is None:
        return _h.purge_idle_sessions(config=config)
    return _h.purge_idle_sessions(idle_timeout_sec=idle_timeout_sec, config=config)


__all__ = [
    "lookup_session_by_addressee_handle",
    "lookup_session_by_requester_handle",
    "purge_idle_sessions",
]
