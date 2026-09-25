"""_phase3_migrate.py — split legacy imessage-state.json into per-concept files.

Internal entry point shared by both the boot-time migration in main.py and
the standalone migration script in scripts/migrate_state_to_per_concept.py.

Design choices:

- Idempotent. If a per-concept file already exists with non-default content,
  we skip the split for that concept and keep what's on disk. This lets a
  partial-failure mid-migration recover safely on the next boot.
- Backup, do not delete. The legacy file is renamed to
  `imessage-state.json.pre-phase3-backup` only after every concept has been
  successfully written. If any concept write fails, the legacy file stays
  in place untouched.
- Per-writer-unique tmp filenames (atomic_write_json under the hood). The
  2026-05-06 silent incident cannot recur.
- The legacy file may carry fields that don't map to any current concept
  (e.g., a typo, or a future field). Those fields are preserved on the
  legacy file → backup but are NOT silently dropped. The backup file is
  the safety net.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kavi_runtime.state_per_concept import CONCEPTS

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".pre-phase3-backup"


def _slice_legacy_into_concepts(legacy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map every concept name to the slice of `legacy` that belongs to it.

    Concept ownership is keyed on the concept's default dict — any key in
    that default belongs to the concept. Any legacy key not claimed by any
    concept is reported in the returned `__unowned` slice so the caller can
    log it.
    """
    sliced: dict[str, dict[str, Any]] = {}
    claimed_keys: set[str] = set()
    for concept, (_path_fn, empty_value) in CONCEPTS.items():
        slice_data: dict[str, Any] = {}
        for k in empty_value.keys():
            if k in legacy:
                slice_data[k] = legacy[k]
                claimed_keys.add(k)
            else:
                # Backfill the default so the per-concept file has a complete
                # shape post-migration.
                slice_data[k] = (
                    [] if isinstance(empty_value[k], list)
                    else {} if isinstance(empty_value[k], dict)
                    else empty_value[k]
                )
        sliced[concept] = slice_data

    unowned = {k: v for k, v in legacy.items() if k not in claimed_keys}
    if unowned:
        sliced["__unowned"] = unowned
    return sliced


def migrate_in_process(legacy_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Split the legacy file into per-concept files.

    Returns a dict for structured logging:
      {
        "concepts_written": ["questions", ...],
        "concepts_skipped_already_present": [...],
        "unowned_keys": [...],
        "backup_path": ".../imessage-state.json.pre-phase3-backup",
        "dry_run": bool,
      }

    Raises on I/O failure; caller decides whether to swallow (boot path) or
    propagate (standalone script path).
    """
    legacy = Path(legacy_path)
    if not legacy.exists():
        raise FileNotFoundError(f"legacy file not found: {legacy}")

    raw = legacy.read_text()
    try:
        legacy_data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"legacy file at {legacy} is corrupt JSON; refusing to migrate. {e}"
        ) from e

    sliced = _slice_legacy_into_concepts(legacy_data)
    unowned = sliced.pop("__unowned", {})

    written: list[str] = []
    skipped: list[str] = []

    for concept, slice_data in sliced.items():
        path_fn, _empty = CONCEPTS[concept]
        target = path_fn(legacy)
        if target.exists():
            # Already migrated for this concept (partial-failure recovery).
            skipped.append(concept)
            continue
        if dry_run:
            written.append(concept)
            continue
        # Lazy import to avoid a circular dependency on state_per_concept.
        from kavi_runtime.state_io import atomic_write_json
        atomic_write_json(target, slice_data)
        written.append(concept)

    backup_path: str | None = None
    if not dry_run and not skipped and written:
        # Only rename the legacy when ALL concepts were freshly written this
        # run. If we skipped any (recovery), the legacy was already renamed
        # on the prior pass — or the operator wants to keep it; either way,
        # do not touch it again.
        backup = legacy.with_name(legacy.name + BACKUP_SUFFIX)
        # Avoid overwriting an existing backup.
        if not backup.exists():
            legacy.rename(backup)
            backup_path = str(backup)
        else:
            backup_path = str(backup) + " (existing, not overwritten)"

    return {
        "concepts_written": written,
        "concepts_skipped_already_present": skipped,
        "unowned_keys": sorted(unowned.keys()),
        "backup_path": backup_path,
        "dry_run": dry_run,
    }
