"""Webhook re-fire dedup for inbox-to-task and any other capability that
needs short-window idempotency on message_id.

MS Graph occasionally re-fires the same email webhook within 3-5 sec before
linkedResources are searchable; this cache short-circuits before any LLM /
Graph traffic. The outcome shape lets the eval log record the re-fire's
prior decision.

Lives in `runtime/` because the dispatcher (in handlers / capabilities) and
the test suite both need access.
"""

from __future__ import annotations

import threading
import time

_DEDUP_TTL_SEC = 120
_dedup_cache: dict[str, tuple[dict, float]] = {}
_dedup_lock = threading.Lock()
_message_locks: dict[str, threading.Lock] = {}


def _dedup_check(message_id: str) -> dict | None:
    """Return the cached outcome dict for this message_id, or None if not seen
    within the TTL window. Stale entries are pruned in-line. The outcome dict
    has shape {"decision": str, "task_id": str | None, "decision_id": str | None}."""
    now = time.monotonic()
    with _dedup_lock:
        stale = [k for k, (_, ts) in _dedup_cache.items() if now - ts > _DEDUP_TTL_SEC]
        for k in stale:
            del _dedup_cache[k]
            _message_locks.pop(k, None)
        entry = _dedup_cache.get(message_id)
        return entry[0] if entry else None


def _dedup_record(message_id: str, outcome: dict | str) -> None:
    """Record the decision outcome for this message_id. Accepts either a dict
    (preferred — full outcome shape) or a string (legacy — interpreted as a
    bare task_id with decision unknown). String form is kept backwards-
    compatible for any out-of-tree caller; in-tree callers should pass a
    dict so the eval log can record the re-fire's prior decision."""
    if isinstance(outcome, str):
        outcome = {"decision": "unknown", "task_id": outcome, "decision_id": None}
    with _dedup_lock:
        _dedup_cache[message_id] = (outcome, time.monotonic())


def _per_message_lock(message_id: str) -> threading.Lock:
    """Returns a lock scoped to this message_id. Concurrent processings for the same
    email serialize through this lock so dedup-check + create is atomic per email."""
    with _dedup_lock:
        return _message_locks.setdefault(message_id, threading.Lock())


# In-flight claims (added 2026-09-23). `_dedup_record` only lands after the
# LLM judgment returns, so a Graph re-fire arriving DURING the first judgment
# (median gap 4s vs 4-9s judgment latency) passed `_dedup_check` and paid for
# a second judgment: ~25% of all email judgments, ~$13-16/month. A claim is
# taken before the Graph fetch and released when processing ends (success or
# exception), so a crashed first run never suppresses a later retry.
_in_flight: set[str] = set()


def _claim_in_flight(message_id: str) -> bool:
    """Atomically claim this message_id. True if claimed; False if another
    worker is already processing it (caller should treat it as a re-fire)."""
    with _dedup_lock:
        if message_id in _in_flight:
            return False
        _in_flight.add(message_id)
        return True


def _release_in_flight(message_id: str) -> None:
    with _dedup_lock:
        _in_flight.discard(message_id)


__all__ = [
    "_dedup_check", "_dedup_record", "_per_message_lock", "_DEDUP_TTL_SEC",
    "_claim_in_flight", "_release_in_flight",
]
