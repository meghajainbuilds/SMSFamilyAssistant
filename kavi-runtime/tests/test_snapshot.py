"""Tests for kavi_runtime.snapshot — nightly state-file backup + retention."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kavi_runtime.snapshot import (
    DEFAULT_STATE_FILES,
    nightly_snapshot_job,
    prune_old_snapshots,
    snapshot_state,
)


def _make_config(tmp_path: Path) -> dict:
    """Build a config dict that points snapshot/state paths into tmp_path."""
    homeos = tmp_path / "homeos"
    homeos.mkdir()
    snaps = tmp_path / "snapshots"
    return {
        "paths": {
            "homeos_root": str(homeos),
            "imessage_state": str(homeos / "imessage-state.json"),
            "learned_facts": str(homeos / "learned_facts.jsonl"),
            "corrections_jsonl": str(homeos / "corrections.jsonl"),
            "snapshots_root": str(snaps),
        }
    }


def _seed_state_files(config: dict) -> None:
    homeos = Path(config["paths"]["homeos_root"])
    (homeos / "imessage-state.json").write_text(json.dumps({"seen": []}))
    (homeos / "learned_facts.jsonl").write_text('{"id": "f1"}\n')
    (homeos / "pending_facts.jsonl").write_text('{"id": "p1"}\n')
    (homeos / "corrections.jsonl").write_text('{"id": "c1"}\n')
    (homeos / "runs.jsonl").write_text('{"run_id": "r1"}\n')


def test_snapshot_creates_dated_dir_and_copies_all_five_files(tmp_path: Path) -> None:
    """Acceptance: a snapshot run creates today's UTC-dated dir under
    snapshots_root and copies all 5 state files we declared in
    DEFAULT_STATE_FILES."""
    config = _make_config(tmp_path)
    _seed_state_files(config)

    fixed_now = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
    out_dir = snapshot_state(config, now=fixed_now)

    assert out_dir.name == "2026-05-06"
    assert out_dir.exists()
    for filename in DEFAULT_STATE_FILES:
        copied = out_dir / filename
        assert copied.exists(), f"missing snapshot of {filename}"
        # Sanity: payload identical to source.
        src_path = Path(config["paths"]["homeos_root"]) / filename
        if filename == "imessage-state.json":
            src_path = Path(config["paths"]["imessage_state"])
        elif filename == "learned_facts.jsonl":
            src_path = Path(config["paths"]["learned_facts"])
        elif filename == "corrections.jsonl":
            src_path = Path(config["paths"]["corrections_jsonl"])
        assert copied.read_text() == src_path.read_text()


def test_snapshot_skips_missing_source_files(tmp_path: Path) -> None:
    """Missing source files (fresh install — no corrections yet) should not
    crash the snapshot job. The run still produces the date dir, just with
    fewer files."""
    config = _make_config(tmp_path)
    homeos = Path(config["paths"]["homeos_root"])
    # Only seed 2 of the 5 files.
    (homeos / "imessage-state.json").write_text("{}")
    (homeos / "runs.jsonl").write_text('{"run_id": "r1"}\n')

    fixed_now = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
    out_dir = snapshot_state(config, now=fixed_now)

    assert (out_dir / "imessage-state.json").exists()
    assert (out_dir / "runs.jsonl").exists()
    assert not (out_dir / "learned_facts.jsonl").exists()


def test_prune_deletes_older_than_14_days(tmp_path: Path) -> None:
    """Acceptance: retention deletes the 15th-oldest snapshot dir but
    keeps the most recent 14."""
    config = _make_config(tmp_path)
    snaps = Path(config["paths"]["snapshots_root"])
    snaps.mkdir(parents=True)

    today = datetime(2026, 5, 6, tzinfo=timezone.utc).date()
    # Create dirs for the last 20 days (so 6 should be deleted at keep_days=14).
    created: list[Path] = []
    for n in range(20):
        d = today - timedelta(days=n)
        sub = snaps / d.strftime("%Y-%m-%d")
        sub.mkdir()
        (sub / "marker.txt").write_text("x")
        created.append(sub)

    # Confirm a non-date dir is left alone.
    weird = snaps / "manual-backup"
    weird.mkdir()
    (weird / "marker.txt").write_text("keep me")

    fixed_now = datetime(2026, 5, 6, 10, 0, tzinfo=timezone.utc)
    deleted = prune_old_snapshots(config, keep_days=14, now=fixed_now)

    deleted_names = sorted(p.name for p in deleted)
    # cutoff = today - 14 days. We delete dirs strictly older than cutoff,
    # i.e., days 15-19 ago (5 dirs). The 14-day-old dir sits exactly on the
    # cutoff and is kept. The 15th-oldest dir is the first to go.
    expected_deleted = sorted(
        (today - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(15, 20)
    )
    assert deleted_names == expected_deleted
    # The 15th-oldest specifically (the spec line) is gone.
    fifteenth_oldest = today - timedelta(days=15)
    assert not (snaps / fifteenth_oldest.strftime("%Y-%m-%d")).exists()
    # 14-day-old dir is kept (boundary).
    fourteenth_oldest = today - timedelta(days=14)
    assert (snaps / fourteenth_oldest.strftime("%Y-%m-%d")).exists()
    # Non-date dir untouched.
    assert weird.exists()


def test_nightly_job_swallows_exceptions(tmp_path: Path) -> None:
    """The scheduler hook must never raise — a snapshot bug should not
    take the runtime down. We pass a config with an unwritable path and
    confirm the job returns cleanly."""
    config = {"paths": {"snapshots_root": "/nonexistent/forbidden/path"}}
    # Should not raise.
    nightly_snapshot_job(config)
