"""Local nightly snapshot of state files (item #1 of 2026-05-06 hygiene pass).

Why: if any of `imessage-state.json`, `learned_facts.jsonl`, `pending_facts.jsonl`,
`corrections.jsonl`, or `runs.jsonl` gets corrupted (truncated mid-write,
accidental schema migration, runtime bug), Megha currently has no rollback —
they're append-only on Kavi's Mac with no second copy. A nightly snapshot
gives us a 14-day rollback window without standing up cloud backups (which
are queued separately as item #E).

What ships:
- `snapshot_state(config)` — copies the 5 source files to a UTC-dated
  directory under `paths.snapshots_root` (defaults to
  `/Users/kavi/kavi-runtime/snapshots/`).
- `prune_old_snapshots(config, keep_days=14)` — deletes any dated dir older
  than the retention window.
- `nightly_snapshot_job(config)` — wraps both for the APScheduler hook.

Failure modes are logged and swallowed: a snapshot that fails should NEVER
take down the runtime. Missing source files (e.g., no corrections yet) are
skipped silently; the snapshot still runs for the files that exist.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# Filenames we snapshot. Resolved against `paths.homeos_root` if a path
# isn't explicitly configured. Keep this list in sync with anything we'd
# need to reconstruct a runtime state from cold storage.
DEFAULT_STATE_FILES: tuple[str, ...] = (
    "imessage-state.json",
    "learned_facts.jsonl",
    "pending_facts.jsonl",
    "corrections.jsonl",
    "runs.jsonl",
)


def _snapshots_root(config: dict) -> Path:
    """Resolve the snapshots root, defaulting to a sibling of the runtime
    install. Allows tests to override via paths.snapshots_root."""
    configured = config.get("paths", {}).get("snapshots_root")
    if configured:
        return Path(configured)
    # Default lives next to the runtime install on Kavi's Mac. Tests should
    # override via the config dict; we never assume Kavi's paths in tests.
    return Path("/Users/kavi/kavi-runtime/snapshots")


def _resolve_state_file(config: dict, filename: str) -> Path:
    """Look up an explicit configured path for `filename` (e.g.,
    `paths.imessage_state` for imessage-state.json) and fall back to
    `homeos_root/<filename>` otherwise."""
    paths = config.get("paths", {})
    # Explicit configured paths take priority. Map by filename.
    explicit_keys = {
        "imessage-state.json": "imessage_state",
        "learned_facts.jsonl": "learned_facts",
        "corrections.jsonl": "corrections_jsonl",
    }
    key = explicit_keys.get(filename)
    if key and paths.get(key):
        return Path(paths[key])
    homeos_root = paths.get("homeos_root")
    if homeos_root:
        return Path(homeos_root) / filename
    # Last-resort fallback: assume CWD. Tests always pass explicit config.
    return Path(filename)


def snapshot_state(config: dict, *, now: datetime | None = None) -> Path:
    """Copy the 5 state files to a UTC-dated dir under snapshots_root.

    Returns the dated directory path. Missing source files are skipped
    silently (they may not exist yet on a fresh install). Per-file copy
    failures log and continue — partial snapshot beats no snapshot."""
    now = now or datetime.now(timezone.utc)
    date_dir = _snapshots_root(config) / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = 0
    for filename in DEFAULT_STATE_FILES:
        src = _resolve_state_file(config, filename)
        if not src.exists():
            logger.debug("snapshot: source missing, skipping: %s", src)
            skipped += 1
            continue
        try:
            shutil.copy2(src, date_dir / filename)
            copied += 1
        except Exception:
            logger.exception("snapshot: copy failed for %s", src)

    logger.info(
        "snapshot complete: dir=%s copied=%d skipped=%d", date_dir, copied, skipped
    )
    return date_dir


def prune_old_snapshots(config: dict, *, keep_days: int = 14,
                        now: datetime | None = None) -> list[Path]:
    """Delete dated dirs older than `keep_days` from today (UTC). Returns
    the list of deleted paths so the scheduler can log a rollup line.

    Snapshot dir naming convention is `YYYY-MM-DD`; anything that doesn't
    parse is left alone (defensive — never delete a dir we can't classify)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=keep_days)).date()
    root = _snapshots_root(config)
    if not root.exists():
        return []

    deleted: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            child_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            logger.debug("snapshot prune: skipping non-date dir %s", child)
            continue
        if child_date < cutoff:
            try:
                shutil.rmtree(child)
                deleted.append(child)
                logger.info("snapshot prune: removed %s", child)
            except Exception:
                logger.exception("snapshot prune: rmtree failed for %s", child)
    return deleted


def nightly_snapshot_job(config: dict) -> None:
    """APScheduler hook. Snapshot first, then prune. Never raises."""
    try:
        snapshot_state(config)
    except Exception:
        logger.exception("nightly snapshot failed")
    try:
        prune_old_snapshots(config)
    except Exception:
        logger.exception("nightly snapshot prune failed")
