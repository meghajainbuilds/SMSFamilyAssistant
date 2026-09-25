"""Persisted running-total Anthropic spend counter.

Why (2026-06-10): the /status spend fields and the $30/month cap both
summed `usage` rows from `paths.runtime_events_jsonl` — a file no code
ever writes usage rows to. Every reading was $0.00 since inception and
the cap-trip branch was unreachable. Actual usage flows through
`ClaudeClient._log_call_done` (every `messages.create` site in the
codebase), which now calls `record_spend` here after each call. /status
and the cap read the running total O(1) instead of replaying a log.

State file: `anthropic_spend.json`, living next to the other per-concept
state files (same directory as `paths.imessage_state`, matching the
`state_per_concept` path-derivation pattern). Like `coordination_sessions`
(2026-06-10), this concept is deliberately NOT in
`state_per_concept.CONCEPTS`: that map drives the Phase 3 legacy-file
migration, and spend never lived in the legacy monolithic state file —
its absence must not look like a migration trigger. The load/save
contract is otherwise identical: atomic write via `state_io` (per-writer-
unique tmp filename), corrupt file archived + cold-start.

Shape:
    {
      "month_key": "2026-06",            # Pacific calendar month
      "month_usd": 12.3456,
      "days": {"2026-06-10": 0.8123},    # current month's Pacific days only
      "previous_months": {"2026-05": 14.2},  # bounded: last 12 months
      "last_call": {"ts": "...", "model": "...", "call_type": "...", "usd": ...}
    }

"Today" and the month boundary are PACIFIC time (household convention;
/status displays PT).

Failure contract: `record_spend` NEVER raises — it sits on the API-call
hot path inside `_log_call_done`, and a broken spend counter must never
break Kavi's actual replies. Failures are logged and dropped.
"""

from __future__ import annotations

import copy
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from kavi_runtime.state import LOCAL_TZ, utc_now_iso
from kavi_runtime.state_io import atomic_write_json, read_json_recover

logger = logging.getLogger(__name__)

SPEND_FILE_NAME = "anthropic_spend.json"

# Previous-months archive is bounded so the file never grows past ~1 year
# of history. 12 keeps a full year visible for trend questions.
PREVIOUS_MONTHS_KEPT = 12

_EMPTY: dict[str, Any] = {
    "month_key": None,
    "month_usd": 0.0,
    "days": {},
    "previous_months": {},
    "last_call": None,
}

# In-process serialization of read-modify-write cycles. The atomic write
# in state_io prevents file corruption under concurrency, but without a
# lock two concurrent writers could each read the same base total and one
# increment would be lost. The runtime is single-process; this lock is
# sufficient.
_lock = threading.Lock()


def spend_state_path(config: dict) -> Path:
    """Sibling of the other per-concept state files: derived from the
    directory of `paths.imessage_state` (same derivation as
    `state_per_concept._state_dir_from_legacy`)."""
    return Path(config["paths"]["imessage_state"]).parent / SPEND_FILE_NAME


def _month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def _day_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def _pacific_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(LOCAL_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=LOCAL_TZ)
    return now.astimezone(LOCAL_TZ)


def _load(path: Path) -> dict[str, Any]:
    """Missing → empty shape. Corrupt → archived by state_io + empty shape
    (the counter cold-starts; history before the corruption is lost, which
    is acceptable for a spend gauge and logged CRITICAL by state_io)."""
    raw = read_json_recover(path, default=None)
    if not isinstance(raw, dict):
        return copy.deepcopy(_EMPTY)
    for k, v in _EMPTY.items():
        if k not in raw:
            raw[k] = copy.deepcopy(v)
    return raw


def _rollover_if_needed(data: dict[str, Any], month_key: str) -> dict[str, Any]:
    """On Pacific month change: archive the finished month's total into
    `previous_months` (bounded at PREVIOUS_MONTHS_KEPT, newest kept) and
    reset the running counters."""
    if data.get("month_key") == month_key:
        return data
    old_key = data.get("month_key")
    if old_key:
        prev = dict(data.get("previous_months") or {})
        prev[old_key] = round(float(data.get("month_usd") or 0.0), 6)
        # Month keys are YYYY-MM so lexicographic sort == chronological.
        data["previous_months"] = {
            k: prev[k] for k in sorted(prev)[-PREVIOUS_MONTHS_KEPT:]
        }
    data["month_key"] = month_key
    data["month_usd"] = 0.0
    data["days"] = {}
    return data


def record_spend(
    config: dict,
    usd: float,
    *,
    model: str,
    call_type: str,
    now: datetime | None = None,
) -> None:
    """Add `usd` to today's and this month's running totals. Atomic
    read-modify-write (per-writer-unique tmp via state_io). NEVER raises:
    any failure is logged and dropped so the API-call path is unaffected.
    """
    try:
        if not usd or usd <= 0:
            return
        now_pt = _pacific_now(now)
        path = spend_state_path(config)
        with _lock:
            data = _load(path)
            data = _rollover_if_needed(data, _month_key(now_pt))
            data["month_usd"] = round(float(data["month_usd"]) + usd, 10)
            day = _day_key(now_pt)
            days = data.get("days") or {}
            days[day] = round(float(days.get(day, 0.0)) + usd, 10)
            # Keep only the current month's days (rollover already reset,
            # but a clock skew should never let stray days accumulate).
            data["days"] = {
                k: v for k, v in days.items() if k.startswith(data["month_key"])
            }
            data["last_call"] = {
                "ts": utc_now_iso(),
                "model": model,
                "call_type": call_type,
                "usd": round(usd, 8),
            }
            atomic_write_json(path, data)
    except Exception:
        logger.warning(
            "spend_state.record_spend failed (continuing; API call path unaffected)",
            exc_info=True,
        )


def record_spend_from_usage(
    config: dict,
    model: str,
    call_type: str,
    usage: dict[str, Any] | None,
    now: datetime | None = None,
) -> None:
    """Convenience hook for `ClaudeClient._log_call_done`: price the call's
    usage dict via the canonical pricing module and record it. Accepts both
    cache-field naming conventions (see pricing.cost_usd_from_usage).
    Inherits record_spend's never-raises contract."""
    try:
        from kavi_runtime.pricing import cost_usd_from_usage
        usd = cost_usd_from_usage(model, usage)
        if usd > 0:
            record_spend(config, usd, model=model, call_type=call_type, now=now)
    except Exception:
        logger.warning("spend_state.record_spend_from_usage failed (continuing)",
                       exc_info=True)


def read_spend(config: dict, *, now: datetime | None = None) -> dict[str, float]:
    """O(1) read for /status and the monthly cap.

    Returns {"today_usd": float, "month_usd": float} for the current
    Pacific day/month. A stale file from a previous month reads as $0.00
    (the rollover archive happens lazily on the next record_spend).
    Failure-safe: any error reads as $0.00 — the cap treats unreadable
    state as not-tripped rather than pausing Kavi on a broken file.
    """
    try:
        now_pt = _pacific_now(now)
        data = _load(spend_state_path(config))
        if data.get("month_key") != _month_key(now_pt):
            return {"today_usd": 0.0, "month_usd": 0.0}
        days = data.get("days") or {}
        return {
            "today_usd": float(days.get(_day_key(now_pt), 0.0)),
            "month_usd": float(data.get("month_usd") or 0.0),
        }
    except Exception:
        logger.warning("spend_state.read_spend failed (returning $0.00)", exc_info=True)
        return {"today_usd": 0.0, "month_usd": 0.0}


__all__ = [
    "SPEND_FILE_NAME",
    "PREVIOUS_MONTHS_KEPT",
    "spend_state_path",
    "record_spend",
    "record_spend_from_usage",
    "read_spend",
]
