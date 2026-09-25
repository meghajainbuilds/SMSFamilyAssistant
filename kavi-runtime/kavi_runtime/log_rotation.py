"""Nightly rotation of launchd-captured stdout/stderr line logs (item #3
of 2026-05-06 batch 6).

Why this exists: launchd writes Kavi's stdout to
`~/Library/Logs/kavi-runtime.log` and stderr to
`~/Library/Logs/kavi-runtime.err.log`. These files grow unbounded — the
JSON log already rotates (we ship structured_log with size/age limits)
but the line logs do not, and they accumulate ~100 KB/day under normal
load. Over a year that's ~36 MB per file, fine, but we don't want to
discover a 5 GB file the day Megha needs to read it.

Approach: in-runtime rotation. Nightly at 03:30 PT, we:
  1. Copy the current contents of each log to `<file>.YYYY-MM-DD` (UTC).
  2. Truncate the original file (open in "w" mode + close). launchd's
     existing file descriptor keeps appending to the truncated inode.
  3. Delete archived files older than 30 days.

This avoids needing sudo on Kavi's Mac (newsyslog approach) and avoids
piping launchd's stdout through a Python sink (risky — a sink crash
silences logs). Tradeoff: the truncation has a 1-2 ms window where
launchd may write between our copy and our truncate; we accept that
loss as the cost of staying out of `/etc/`.

Failure modes are logged and swallowed: a rotation that fails should
NEVER take down the runtime.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# Default log paths under launchd. Tests override via the function args.
DEFAULT_LINE_LOGS: tuple[Path, ...] = (
    Path.home() / "Library" / "Logs" / "kavi-runtime.log",
    Path.home() / "Library" / "Logs" / "kavi-runtime.err.log",
)

KEEP_DAYS_DEFAULT = 30


def rotate_one(log_path: Path, *, now: datetime | None = None) -> Path | None:
    """Archive `log_path` to `<log_path>.YYYY-MM-DD` (UTC) and truncate
    the original. Returns the archive path, or None if the source file
    didn't exist (nothing to rotate).

    Truncation is done by opening the file in "w" mode and immediately
    closing — equivalent to `: > file` from the shell. launchd's open
    file descriptor continues to write to the same inode, which is now
    a 0-byte file."""
    now = now or datetime.now(timezone.utc)
    if not log_path.exists():
        logger.debug("log_rotation: source missing, skipping: %s", log_path)
        return None

    archive_path = log_path.with_name(f"{log_path.name}.{now.strftime('%Y-%m-%d')}")
    try:
        # copy2 preserves mtime / mode. We don't need them for archives,
        # but it's the same cost and keeps the on-disk shape identical
        # to the original for any tooling that inspects the file.
        shutil.copy2(log_path, archive_path)
    except Exception:
        logger.exception("log_rotation: copy failed for %s", log_path)
        return None

    try:
        # Truncate in place. Don't unlink + recreate — that would orphan
        # launchd's open fd to the deleted inode and lose all subsequent
        # writes until the runtime is restarted.
        with log_path.open("w") as f:
            f.write("")
    except Exception:
        logger.exception("log_rotation: truncate failed for %s", log_path)
        # Archive is still on disk; leaving the original intact is better
        # than leaving both intact + the archive partially written.
        return archive_path

    logger.info("log_rotation: rotated %s -> %s", log_path, archive_path)
    return archive_path


def prune_old_archives(log_path: Path, *, keep_days: int = KEEP_DAYS_DEFAULT,
                       now: datetime | None = None) -> list[Path]:
    """Delete archives of `log_path` older than `keep_days` from now (UTC).

    Archive naming: `<log_path>.YYYY-MM-DD`. Anything that doesn't parse
    as a date is left alone — defensive, so we never delete a file we
    can't classify.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=keep_days)).date()
    parent = log_path.parent
    if not parent.exists():
        return []

    deleted: list[Path] = []
    base_name = log_path.name
    prefix = f"{base_name}."
    for child in parent.iterdir():
        if not child.is_file():
            continue
        if not child.name.startswith(prefix):
            continue
        suffix = child.name[len(prefix):]
        try:
            archive_date = datetime.strptime(suffix, "%Y-%m-%d").date()
        except ValueError:
            logger.debug("log_rotation prune: skipping non-date archive %s", child)
            continue
        if archive_date < cutoff:
            try:
                child.unlink()
                deleted.append(child)
                logger.info("log_rotation prune: removed %s", child)
            except Exception:
                logger.exception("log_rotation prune: unlink failed for %s", child)
    return deleted


def nightly_log_rotation_job(config: dict, *,
                             paths: tuple[Path, ...] | None = None,
                             keep_days: int = KEEP_DAYS_DEFAULT,
                             now: datetime | None = None) -> None:
    """APScheduler hook. Rotate each line log, then prune old archives.
    Never raises — a rotation failure must never cascade to the runtime."""
    targets = paths or DEFAULT_LINE_LOGS
    for log_path in targets:
        try:
            rotate_one(log_path, now=now)
        except Exception:
            logger.exception("nightly log rotation failed for %s", log_path)
        try:
            prune_old_archives(log_path, keep_days=keep_days, now=now)
        except Exception:
            logger.exception("nightly log rotation prune failed for %s", log_path)
