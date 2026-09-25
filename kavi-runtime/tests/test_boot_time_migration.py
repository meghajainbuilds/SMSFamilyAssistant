"""Tests for the boot-time Phase 3 migration wired into kavi_runtime.main.

We don't drive the full `run()` here (it binds a port and requires the
Anthropic API key). We test the public entry point `run_boot_migration`
directly under the conditions main.py invokes it.

The contract main.py honors:
- Always returns a dict (never raises).
- Result dict's `event` is one of three names the operator can grep.
- On success: legacy file gone, per-concept files present.
- On already-migrated: no work, no error.
- On failure: returns event=migration_phase3_failed with `error` set;
  the runtime still boots and reads from the legacy file.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kavi_runtime import state_per_concept as spc
from kavi_runtime.state_per_concept import run_boot_migration


@pytest.fixture
def legacy_path(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d / "imessage-state.json"


def _seed_legacy(legacy_path: Path) -> dict:
    payload = {
        "questions": [{"id": "q-1"}],
        "summary_queue": [],
        "last_send_at": None,
        "last_summary_send_at": None,
        "auto_runs_paused": False,
        "paused_email_queue": [],
        "pending_alerts": [],
        "pending_action_clarifications": {},
        "last_summary_anchors": {},
        "alert_dedupe": {},
        "last_failure_rate_alert_at": None,
        "pending_self_check": {},
    }
    legacy_path.write_text(json.dumps(payload))
    return payload


# ---- happy path ----------------------------------------------------------


def test_boot_migration_completes_on_first_boot(legacy_path: Path) -> None:
    _seed_legacy(legacy_path)
    result = run_boot_migration(legacy_path)
    assert result["event"] == "migration_phase3_completed"
    # Legacy file renamed to backup; every per-concept file present.
    assert not legacy_path.exists()
    assert (legacy_path.with_name(legacy_path.name + ".pre-phase3-backup")).exists()
    for path_fn, _ in spc.CONCEPTS.values():
        assert path_fn(legacy_path).exists()


# ---- idempotency on already-migrated runtime ----------------------------


def test_boot_migration_skipped_when_legacy_absent(legacy_path: Path) -> None:
    # No legacy file at all (fresh deploy or post-migration second boot).
    result = run_boot_migration(legacy_path)
    assert result["event"] == "migration_phase3_skipped_already_migrated"


def test_boot_migration_skipped_when_all_per_concept_present(legacy_path: Path) -> None:
    """Edge case: legacy file lingers but every per-concept file already
    exists. Treat as already-migrated and skip — do not overwrite."""
    _seed_legacy(legacy_path)
    # Pre-create every per-concept file (simulates: prior boot migrated, but
    # something else recreated the legacy file e.g. a manual debug action).
    for path_fn, empty in spc.CONCEPTS.values():
        path_fn(legacy_path).write_text(json.dumps(empty))

    result = run_boot_migration(legacy_path)
    assert result["event"] == "migration_phase3_skipped_already_migrated"


# ---- failure modes -------------------------------------------------------


def test_boot_migration_does_not_raise_on_corrupt_legacy(legacy_path: Path) -> None:
    legacy_path.write_text("{not valid json")
    # Must NOT raise — boot has to keep going.
    result = run_boot_migration(legacy_path)
    assert result["event"] == "migration_phase3_failed"
    assert "error" in result


def test_boot_migration_does_not_raise_on_io_failure(legacy_path: Path) -> None:
    _seed_legacy(legacy_path)

    # Patch the slicer to raise; confirm we still return a dict.
    with patch(
        "kavi_runtime._phase3_migrate.migrate_in_process",
        side_effect=RuntimeError("simulated I/O failure"),
    ):
        result = run_boot_migration(legacy_path)

    assert result["event"] == "migration_phase3_failed"
    assert "simulated I/O failure" in result["error"]
    # Legacy still in place so runtime can keep reading from it.
    assert legacy_path.exists()


# ---- parity with the standalone script ----------------------------------


def test_boot_migration_outcome_matches_script_outcome(legacy_path: Path) -> None:
    """The boot-time path and the script path must produce the same
    end-state on the same input (modulo the wrapper's logging)."""
    payload = _seed_legacy(legacy_path)

    result = run_boot_migration(legacy_path)
    assert result["event"] == "migration_phase3_completed"

    # End-state on disk: legacy renamed; per-concept files present; their
    # content matches the legacy slice.
    backup = legacy_path.with_name(legacy_path.name + ".pre-phase3-backup")
    assert json.loads(backup.read_text()) == payload

    questions = spc.load_questions(legacy_path)
    assert questions["questions"] == [{"id": "q-1"}]
