"""state_per_concept.py — one file per concept (Phase 3 of EM-critique refactor).

Why: 2026-05-29's parent-assoc canonical run paid the "which file holds what"
tax. `imessage-state.json` carried 7+ conceptually-distinct concepts; the
Investigator looked at `pending_facts.jsonl` and missed the live bug input
in `questions` inside `imessage-state.json` entirely. Phase 3 splits the
monolithic file into one file per concept so each concept has exactly one
read path and one write path, matching the registry's `live_state_source`
column.

Concept ↔ file mapping (matches docs/phase3-concept-inventory.md):

    questions             /Users/kavi/HomeOS/state/questions.json
    summary_queue         /Users/kavi/HomeOS/state/summary_queue.json
    pause_state           /Users/kavi/HomeOS/state/pause_state.json
    pending_alerts        /Users/kavi/HomeOS/state/pending_alerts.json
    action_clarifications /Users/kavi/HomeOS/state/action_clarifications.json
    alert_dedupe          /Users/kavi/HomeOS/state/alert_dedupe.json
    self_check            /Users/kavi/HomeOS/state/self_check.json

Atomicity contract:

- Every save writes to a per-writer-unique tmp filename (via
  `tempfile.mkstemp` inside `state_io.atomic_write_bytes`), fsyncs, then
  renames over the canonical file. Two concurrent writers to the SAME
  concept file never collide on the tmp path. The 2026-05-06 silent
  incident (shared `.tmp` filename → corruption under concurrent writers)
  cannot recur.
- Every load tolerates a missing canonical file by returning the empty
  value for that concept (`[]` for lists, `{}` for dicts).
- Every load catches `json.JSONDecodeError`, archives the corrupt file
  to `<name>.corrupt-<utc-ts>`, and returns the empty value instead of
  crashing. This mirrors `state_io.read_json_recover` so a wedged
  per-concept file does not break the whole runtime — only that one
  concept cold-starts on its next write.

Shape contract: each file holds ONE concept's data at its document root.
- Lists at the root for `questions`, `summary_queue`, `pending_alerts`.
- Dicts at the root for `pause_state`, `action_clarifications`,
  `alert_dedupe`, `self_check`.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.state_io import atomic_write_json, read_json_recover

logger = logging.getLogger(__name__)


# ---- concept ↔ file mapping ------------------------------------------------


def _state_dir_from_legacy(legacy_path: Path) -> Path:
    """Return the directory holding the legacy imessage-state.json. New
    per-concept files live as siblings."""
    return Path(legacy_path).parent


def questions_path(legacy_path: Path) -> Path:
    return _state_dir_from_legacy(legacy_path) / "questions.json"


def summary_queue_path(legacy_path: Path) -> Path:
    return _state_dir_from_legacy(legacy_path) / "summary_queue.json"


def pause_state_path(legacy_path: Path) -> Path:
    return _state_dir_from_legacy(legacy_path) / "pause_state.json"


def pending_alerts_path(legacy_path: Path) -> Path:
    return _state_dir_from_legacy(legacy_path) / "pending_alerts.json"


def action_clarifications_path(legacy_path: Path) -> Path:
    return _state_dir_from_legacy(legacy_path) / "action_clarifications.json"


def alert_dedupe_path(legacy_path: Path) -> Path:
    return _state_dir_from_legacy(legacy_path) / "alert_dedupe.json"


def self_check_path(legacy_path: Path) -> Path:
    return _state_dir_from_legacy(legacy_path) / "self_check.json"


# Map concept name → (path_fn, empty_value). Used by the migration script
# and the boot-time entry point to iterate concepts in a stable order.
#
# NOTE: `coordination_sessions` (added 2026-06-10) is deliberately NOT in
# this map. CONCEPTS enumerates the concepts split OUT of the legacy
# monolithic imessage-state.json by the Phase 3 migration; coordination
# sessions never lived in the legacy file (they were in-memory-only until
# the 2026-06-03 deploy-restart session loss), so there is nothing to
# migrate and the boot-time migration must not consider the file's absence
# a migration trigger. The load/save helpers below follow the identical
# per-concept contract (atomic write via state_io, unique tmp per writer,
# corrupt-file archive + cold-start).
CONCEPTS: dict[str, tuple[Any, Any]] = {
    "questions": (questions_path, {"questions": [], "last_send_at": None}),
    "summary_queue": (
        summary_queue_path,
        {
            "summary_queue": [],
            "last_summary_send_at": None,
            "last_summary_anchors": {},
        },
    ),
    "pause_state": (
        pause_state_path,
        {
            "auto_runs_paused": False,
            "paused_since": None,
            "paused_reason": None,
            "paused_until": None,
            "liveness_alert_sent": False,
            "paused_email_queue": [],
            "paused_spend_usd": None,
            "spend_cap_bypass_month": None,
        },
    ),
    "pending_alerts": (pending_alerts_path, {"pending_alerts": []}),
    "action_clarifications": (
        action_clarifications_path,
        {"pending_action_clarifications": {}},
    ),
    "alert_dedupe": (
        alert_dedupe_path,
        {"alert_dedupe": {}, "last_failure_rate_alert_at": None},
    ),
    "self_check": (self_check_path, {"pending_self_check": {}}),
}


# ---- generic load/save helpers --------------------------------------------


def _load_concept(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Read a concept file. Missing → defaults. Corrupt → archive + defaults.

    Returns a fresh dict (callers may mutate freely without affecting the
    on-disk file or the module-level CONCEPTS defaults). Backfills missing
    keys from `default` so a partial earlier write still produces a
    complete shape.

    DEEP-COPY discipline: the module-level CONCEPTS dict carries mutable
    inner values (e.g., `{"pending_action_clarifications": {}}`). A shallow
    dict(default) would return a new outer dict but share the inner `{}`,
    so the next caller's mutation leaks into every subsequent load.
    Deep-copy at the boundary so each load returns a fully-isolated tree.
    """
    raw = read_json_recover(path, default=None)
    if raw is None:
        return copy.deepcopy(default)
    # Backfill missing keys; deep-copy each default value to avoid sharing
    # the same inner list/dict across callers.
    for k, v in default.items():
        if k not in raw:
            raw[k] = copy.deepcopy(v)
    return raw


def _save_concept(path: Path, data: dict[str, Any]) -> None:
    """Atomic write via state_io. Per-writer-unique tmp filename guaranteed
    by tempfile.mkstemp inside atomic_write_bytes."""
    atomic_write_json(path, data)


# ---- questions ------------------------------------------------------------


def load_questions(legacy_path: Path) -> dict[str, Any]:
    """Return {'questions': [...], 'last_send_at': '...|None'}."""
    return _load_concept(questions_path(legacy_path), CONCEPTS["questions"][1])


def save_questions(legacy_path: Path, data: dict[str, Any]) -> None:
    _save_concept(questions_path(legacy_path), data)


# ---- summary_queue --------------------------------------------------------


def load_summary_queue(legacy_path: Path) -> dict[str, Any]:
    return _load_concept(
        summary_queue_path(legacy_path), CONCEPTS["summary_queue"][1]
    )


def save_summary_queue(legacy_path: Path, data: dict[str, Any]) -> None:
    _save_concept(summary_queue_path(legacy_path), data)


# ---- pause_state ----------------------------------------------------------


def load_pause_state(legacy_path: Path) -> dict[str, Any]:
    return _load_concept(pause_state_path(legacy_path), CONCEPTS["pause_state"][1])


def save_pause_state(legacy_path: Path, data: dict[str, Any]) -> None:
    _save_concept(pause_state_path(legacy_path), data)


# ---- pending_alerts -------------------------------------------------------


def load_pending_alerts(legacy_path: Path) -> dict[str, Any]:
    return _load_concept(
        pending_alerts_path(legacy_path), CONCEPTS["pending_alerts"][1]
    )


def save_pending_alerts(legacy_path: Path, data: dict[str, Any]) -> None:
    _save_concept(pending_alerts_path(legacy_path), data)


# ---- action_clarifications ------------------------------------------------


def load_action_clarifications(legacy_path: Path) -> dict[str, Any]:
    return _load_concept(
        action_clarifications_path(legacy_path),
        CONCEPTS["action_clarifications"][1],
    )


def save_action_clarifications(legacy_path: Path, data: dict[str, Any]) -> None:
    _save_concept(action_clarifications_path(legacy_path), data)


# ---- alert_dedupe ---------------------------------------------------------


def load_alert_dedupe(legacy_path: Path) -> dict[str, Any]:
    return _load_concept(
        alert_dedupe_path(legacy_path), CONCEPTS["alert_dedupe"][1]
    )


def save_alert_dedupe(legacy_path: Path, data: dict[str, Any]) -> None:
    _save_concept(alert_dedupe_path(legacy_path), data)


# ---- self_check -----------------------------------------------------------


def load_self_check(legacy_path: Path) -> dict[str, Any]:
    return _load_concept(self_check_path(legacy_path), CONCEPTS["self_check"][1])


def save_self_check(legacy_path: Path, data: dict[str, Any]) -> None:
    _save_concept(self_check_path(legacy_path), data)


# ---- coordination_sessions (added 2026-06-10; NOT in CONCEPTS — see note) --

_COORDINATION_SESSIONS_EMPTY: dict[str, Any] = {"sessions": {}}


def coordination_sessions_path(legacy_path: Path) -> Path:
    return _state_dir_from_legacy(legacy_path) / "coordination_sessions.json"


def load_coordination_sessions(legacy_path: Path) -> dict[str, Any]:
    """Return {'sessions': {session_id: {...}}}. Missing file → empty.
    Corrupt file → archived + empty (same recovery contract as every
    other per-concept file)."""
    return _load_concept(
        coordination_sessions_path(legacy_path), _COORDINATION_SESSIONS_EMPTY
    )


def save_coordination_sessions(legacy_path: Path, data: dict[str, Any]) -> None:
    """Atomic write via state_io (per-writer-unique tmp filename — the
    2026-05-06 shared-.tmp corruption pattern cannot recur)."""
    _save_concept(coordination_sessions_path(legacy_path), data)


# ---- boot-time migration entry point --------------------------------------


def needs_migration(legacy_path: Path) -> bool:
    """True if the legacy monolithic state file exists AND at least one
    per-concept file is missing. False if all per-concept files exist
    (already migrated) or the legacy file is gone (fresh runtime)."""
    legacy = Path(legacy_path)
    if not legacy.exists():
        return False
    for path_fn, _empty in CONCEPTS.values():
        if not path_fn(legacy).exists():
            return True
    return False


def run_boot_migration(legacy_path: Path) -> dict[str, Any]:
    """Run the migration in-process if and only if `needs_migration` is True.

    Returns a structured-log-friendly dict with one of three event names:
      - `migration_phase3_completed` (legacy file existed, split succeeded)
      - `migration_phase3_skipped_already_migrated`
      - `migration_phase3_failed` (split started but failed mid-way)

    Never raises — boot must succeed even if migration fails. The startup
    probe can read the marker file separately if ops attention is needed.
    """
    legacy = Path(legacy_path)
    if not legacy.exists():
        return {"event": "migration_phase3_skipped_already_migrated", "reason": "legacy_absent"}
    if not needs_migration(legacy):
        return {"event": "migration_phase3_skipped_already_migrated", "reason": "all_files_present"}

    # Import here to avoid a circular import; the migration script lives
    # outside the package and imports from this module.
    try:
        from kavi_runtime._phase3_migrate import migrate_in_process
    except ImportError as e:
        return {"event": "migration_phase3_failed", "error": f"import_error: {e}"}

    try:
        result = migrate_in_process(legacy)
        return {"event": "migration_phase3_completed", **result}
    except Exception as e:  # noqa: BLE001 - boot must not crash
        logger.exception("phase3 boot migration failed; runtime will continue with legacy file")
        return {"event": "migration_phase3_failed", "error": str(e)}
