"""Tests for scripts/migrate_state_to_per_concept.py and the in-process
slicer it calls (_phase3_migrate.migrate_in_process).

Covers:
- Round-trip: legacy → per-concept files + backup, with content equality.
- Idempotency: a second run is a no-op.
- Partial-failure recovery: if some per-concept files exist, the rest get
  migrated and the legacy file is NOT renamed (so the next run completes).
- Dry-run: no files written, no backup created.
- Unowned keys: legacy keys that don't map to any concept are preserved on
  the backup file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kavi_runtime import state_per_concept as spc
from kavi_runtime._phase3_migrate import BACKUP_SUFFIX, migrate_in_process


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "migrate_state_to_per_concept.py"


@pytest.fixture
def legacy_path(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d / "imessage-state.json"


@pytest.fixture
def synthetic_legacy_content() -> dict:
    """Mirrors the real shape on Kavi as of 2026-06-02."""
    return {
        "questions": [
            {"id": "q-1", "task_id": "t-1", "task_title_rendered": "Hello"},
        ],
        "summary_queue": [{"id": "s-1"}],
        "last_send_at": "2026-06-02T12:00:00Z",
        "last_summary_send_at": "2026-06-02T04:00:00Z",
        "auto_runs_paused": False,
        "paused_since": None,
        "paused_reason": None,
        "paused_email_queue": [],
        "pending_alerts": [],
        "spend_cap_bypass_month": None,
        "pending_action_clarifications": {
            "+15555550101": {"action_type": "mark_done"}
        },
        "last_summary_anchors": {"+15555550101": {"anchor_task_id": "t-x"}},
        "alert_dedupe": {"sender|class": 123456.0},
        "last_failure_rate_alert_at": 123456.0,
        "pending_self_check": {"sent_at": "2026-05-30T21:00:00Z"},
    }


# ---- round-trip ----------------------------------------------------------


def test_round_trip_migration_writes_every_concept(
    legacy_path: Path, synthetic_legacy_content: dict
) -> None:
    legacy_path.write_text(json.dumps(synthetic_legacy_content))

    result = migrate_in_process(legacy_path)

    assert set(result["concepts_written"]) == set(spc.CONCEPTS.keys())
    assert result["concepts_skipped_already_present"] == []
    assert result["dry_run"] is False

    # Per-concept files all written with the expected content.
    questions = spc.load_questions(legacy_path)
    assert questions["questions"] == synthetic_legacy_content["questions"]
    assert questions["last_send_at"] == "2026-06-02T12:00:00Z"

    summary = spc.load_summary_queue(legacy_path)
    assert summary["summary_queue"] == [{"id": "s-1"}]
    assert summary["last_summary_send_at"] == "2026-06-02T04:00:00Z"
    assert summary["last_summary_anchors"] == {"+15555550101": {"anchor_task_id": "t-x"}}

    pause = spc.load_pause_state(legacy_path)
    assert pause["auto_runs_paused"] is False
    assert pause["paused_email_queue"] == []

    dedupe = spc.load_alert_dedupe(legacy_path)
    assert dedupe["alert_dedupe"] == {"sender|class": 123456.0}
    assert dedupe["last_failure_rate_alert_at"] == 123456.0

    sc = spc.load_self_check(legacy_path)
    assert sc["pending_self_check"] == {"sent_at": "2026-05-30T21:00:00Z"}

    # Legacy file renamed to backup.
    assert not legacy_path.exists()
    backup = legacy_path.with_name(legacy_path.name + BACKUP_SUFFIX)
    assert backup.exists()
    assert json.loads(backup.read_text()) == synthetic_legacy_content


# ---- idempotency ---------------------------------------------------------


def test_second_run_is_a_no_op(
    legacy_path: Path, synthetic_legacy_content: dict
) -> None:
    legacy_path.write_text(json.dumps(synthetic_legacy_content))
    first = migrate_in_process(legacy_path)
    assert first["concepts_written"]

    # First call has renamed the legacy file to backup, so the script
    # invocation should detect "no work to do".
    assert not spc.needs_migration(legacy_path)

    # Calling migrate_in_process again on the legacy_path would raise
    # because the legacy file no longer exists; the script wrapper guards
    # against that. Test the wrapper via subprocess.
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--legacy-path", str(legacy_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    combined = (proc.stdout + proc.stderr).lower()
    assert "migration already complete" in combined


# ---- partial-failure recovery -------------------------------------------


def test_partial_failure_recovery_completes_remaining(
    legacy_path: Path, synthetic_legacy_content: dict
) -> None:
    """Simulate a prior interrupted run that wrote 3 of 7 concept files.
    The re-run completes the remaining 4 and does NOT re-write the 3 that
    already exist."""
    legacy_path.write_text(json.dumps(synthetic_legacy_content))

    # Pre-create 3 per-concept files with sentinel content. The migration
    # must not overwrite them.
    spc.questions_path(legacy_path).write_text(json.dumps({"sentinel": "questions"}))
    spc.summary_queue_path(legacy_path).write_text(json.dumps({"sentinel": "summary"}))
    spc.pause_state_path(legacy_path).write_text(json.dumps({"sentinel": "pause"}))

    result = migrate_in_process(legacy_path)

    assert set(result["concepts_skipped_already_present"]) == {
        "questions",
        "summary_queue",
        "pause_state",
    }
    assert set(result["concepts_written"]) == set(spc.CONCEPTS.keys()) - {
        "questions",
        "summary_queue",
        "pause_state",
    }

    # Sentinels preserved.
    assert json.loads(spc.questions_path(legacy_path).read_text()) == {"sentinel": "questions"}

    # Legacy file is NOT renamed when concepts were skipped — we don't know
    # whether the prior run's backup already happened. Operator can clean up
    # manually if needed.
    assert legacy_path.exists()


# ---- dry-run -------------------------------------------------------------


def test_dry_run_writes_no_files(
    legacy_path: Path, synthetic_legacy_content: dict
) -> None:
    legacy_path.write_text(json.dumps(synthetic_legacy_content))

    result = migrate_in_process(legacy_path, dry_run=True)
    assert result["dry_run"] is True
    assert set(result["concepts_written"]) == set(spc.CONCEPTS.keys())

    # No per-concept files written.
    for path_fn, _ in spc.CONCEPTS.values():
        assert not path_fn(legacy_path).exists()
    # Legacy still in place.
    assert legacy_path.exists()
    assert not (legacy_path.with_name(legacy_path.name + BACKUP_SUFFIX)).exists()


# ---- unowned keys (forward compatibility) -------------------------------


def test_unowned_keys_preserved_on_backup(legacy_path: Path) -> None:
    """A future-added legacy key that no current concept claims should
    survive on the backup file and be reported in the result."""
    legacy_path.write_text(
        json.dumps(
            {
                "questions": [],
                "future_unknown_field": {"some": "value"},
            }
        )
    )

    result = migrate_in_process(legacy_path)
    assert "future_unknown_field" in result["unowned_keys"]
    backup = legacy_path.with_name(legacy_path.name + BACKUP_SUFFIX)
    assert json.loads(backup.read_text())["future_unknown_field"] == {"some": "value"}


# ---- script wrapper smoke ------------------------------------------------


def test_script_wrapper_runs_end_to_end(
    legacy_path: Path, synthetic_legacy_content: dict
) -> None:
    """Invoke the script binary directly and confirm it produces the same
    end-state as migrate_in_process."""
    legacy_path.write_text(json.dumps(synthetic_legacy_content))

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--legacy-path", str(legacy_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    assert not legacy_path.exists()
    assert (legacy_path.with_name(legacy_path.name + BACKUP_SUFFIX)).exists()
    for path_fn, _ in spc.CONCEPTS.values():
        assert path_fn(legacy_path).exists()


def test_script_wrapper_dry_run(legacy_path: Path, synthetic_legacy_content: dict) -> None:
    legacy_path.write_text(json.dumps(synthetic_legacy_content))
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--legacy-path",
            str(legacy_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    # No files written.
    for path_fn, _ in spc.CONCEPTS.values():
        assert not path_fn(legacy_path).exists()
    assert legacy_path.exists()
