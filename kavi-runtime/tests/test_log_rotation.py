"""Tests for kavi_runtime.log_rotation (item #3 of 2026-05-06 batch 6).

The launchd line logs (kavi-runtime.log + kavi-runtime.err.log) grow
unbounded. We rotate them nightly via APScheduler. These tests cover:

  1. Rotation actually moves contents to a dated archive AND truncates
     the original (launchd's open fd keeps writing into the same inode).
  2. Prune deletes archives older than `keep_days` and leaves recent
     archives + non-date files alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from kavi_runtime.log_rotation import (
    nightly_log_rotation_job,
    prune_old_archives,
    rotate_one,
)


def test_rotate_one_archives_and_truncates(tmp_path: Path) -> None:
    """The contents of the source file land in `<file>.YYYY-MM-DD` and
    the source becomes a 0-byte file (truncate-in-place semantics)."""
    log_path = tmp_path / "kavi-runtime.log"
    log_path.write_text("line one\nline two\nline three\n")
    fixed_now = datetime(2026, 5, 6, 10, 30, tzinfo=timezone.utc)

    archive = rotate_one(log_path, now=fixed_now)

    assert archive is not None
    assert archive.name == "kavi-runtime.log.2026-05-06"
    assert archive.read_text() == "line one\nline two\nline three\n"
    # Original is truncated, not deleted — preserves launchd's open fd.
    assert log_path.exists()
    assert log_path.read_text() == ""


def test_prune_removes_old_archives_only(tmp_path: Path) -> None:
    """Prune deletes archives older than `keep_days` and leaves recent
    archives + the live log file + non-date sidecar files untouched."""
    log_path = tmp_path / "kavi-runtime.log"
    log_path.write_text("")  # live file must survive prune

    fixed_now = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
    # 31 days ago — should be pruned with keep_days=30.
    old_archive = tmp_path / f"kavi-runtime.log.{(fixed_now - timedelta(days=31)).strftime('%Y-%m-%d')}"
    old_archive.write_text("old contents")
    # 5 days ago — should survive.
    recent_archive = tmp_path / f"kavi-runtime.log.{(fixed_now - timedelta(days=5)).strftime('%Y-%m-%d')}"
    recent_archive.write_text("recent contents")
    # Non-date sibling — must be left alone.
    other = tmp_path / "kavi-runtime.log.backup"
    other.write_text("not a dated archive")

    deleted = prune_old_archives(log_path, keep_days=30, now=fixed_now)

    assert old_archive in deleted
    assert recent_archive not in deleted
    assert not old_archive.exists()
    assert recent_archive.exists()
    assert other.exists()
    assert log_path.exists()


def test_nightly_job_rotates_multiple_files_and_swallows_failures(tmp_path: Path) -> None:
    """The scheduler hook accepts a tuple of paths and rotates each.
    A missing file is skipped silently (returns None) — never raises."""
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_text("a contents")
    b.write_text("b contents")
    missing = tmp_path / "does_not_exist.log"
    fixed_now = datetime(2026, 5, 6, 10, 30, tzinfo=timezone.utc)

    nightly_log_rotation_job(
        {}, paths=(a, b, missing), keep_days=30, now=fixed_now,
    )

    assert (tmp_path / "a.log.2026-05-06").read_text() == "a contents"
    assert (tmp_path / "b.log.2026-05-06").read_text() == "b contents"
    assert a.read_text() == ""
    assert b.read_text() == ""
    assert not missing.exists()
