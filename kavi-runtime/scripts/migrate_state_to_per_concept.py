#!/usr/bin/env python3
"""migrate_state_to_per_concept.py — split imessage-state.json into per-concept files.

Phase 3 of the EM-critique refactor. The legacy monolithic imessage-state.json
on Kavi holds 7 conceptually-distinct concepts. This script reads the legacy
file, writes each concept's slice to its own canonical file
(/Users/kavi/HomeOS/state/<concept>.json), and renames the legacy file to
.pre-phase3-backup once every concept lands.

Usage:
    # Real migration:
    python3 -m scripts.migrate_state_to_per_concept

    # Dry run (no files written; prints planned writes):
    python3 -m scripts.migrate_state_to_per_concept --dry-run

    # Override path (defaults to /Users/kavi/HomeOS/state/imessage-state.json):
    python3 -m scripts.migrate_state_to_per_concept --legacy-path /tmp/state/imessage-state.json

Properties:
- Idempotent. Re-running after a successful migration is a no-op (logs
  "migration already complete" and exits 0).
- Partial-failure recovery. If the script dies after writing some but not
  all per-concept files, re-running picks up where the previous run left
  off (the legacy file is renamed to backup ONLY when all concepts write
  successfully in a single run).
- Atomic writes. Each per-concept file is written via state_io's atomic
  helper (per-writer-unique tmp filename, fsync, then rename).
- Safe on errors. Any error mid-script leaves the legacy file untouched
  so the runtime can keep running off it.

This script is also invokable in-process by the runtime's boot-time
migration in kavi_runtime/main.py. Both call into _phase3_migrate.migrate_in_process.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running both as `python -m scripts.migrate_state_to_per_concept` and
# as a bare `python scripts/migrate_state_to_per_concept.py` invocation.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from kavi_runtime._phase3_migrate import migrate_in_process  # noqa: E402
from kavi_runtime.state_per_concept import needs_migration  # noqa: E402

DEFAULT_LEGACY_PATH = Path("/Users/kavi/HomeOS/state/imessage-state.json")

logger = logging.getLogger("phase3.migrate")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-path",
        type=Path,
        default=DEFAULT_LEGACY_PATH,
        help=f"Path to the legacy imessage-state.json (default: {DEFAULT_LEGACY_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report planned writes without modifying any files.",
    )
    args = parser.parse_args(argv)

    legacy_path: Path = args.legacy_path

    if not legacy_path.exists():
        # Either fresh runtime (no legacy file ever existed) or migration
        # already completed in a prior run. Either way, no work to do.
        logger.info("migration already complete (legacy file %s absent)", legacy_path)
        return 0

    if not needs_migration(legacy_path):
        logger.info(
            "migration already complete (every per-concept file present alongside %s)",
            legacy_path,
        )
        return 0

    try:
        result = migrate_in_process(legacy_path, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 - top-level safety net
        logger.error("migration failed: %s", e)
        return 2

    # Pretty-print structured result for the operator.
    logger.info("migration result:\n%s", json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
