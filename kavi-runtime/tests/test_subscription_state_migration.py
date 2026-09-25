"""test_subscription_state_migration.py — verifies the startup
forward-migration of the single-account legacy graph_subscription.json into
the per-account subscriptions/<account>.json.

Why: 2026-05-06 silent-failure. Migration must be idempotent, must not
copy garbage, must leave the legacy file out of the read path after
migration, and must be a no-op when the per-account file already exists.

P2.8 update: split into two idempotent phases (populate + archive) so
a crash between them is recoverable on the next boot. Tests cover both
phases independently AND the resume-after-mid-migration-crash scenario.
"""

from __future__ import annotations

import json

import pytest

from kavi_runtime import graph_client
from kavi_runtime.graph_client import (
    _archive_legacy_if_orphaned,
    _migrate_legacy_subscription_state_forward,
    _populate_target_from_legacy,
)


VALID_LEGACY = {
    "subscription_id": "abc-123",
    "client_state": "secret-token",
    "expiration_dt": "2026-05-08T23:24:00Z",
    "notification_url": "https://example.invalid/graph/notifications",
    "resource": "/me/mailFolders/inbox/messages",
    "created_at": "2026-04-30T05:37:30Z",
}


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect the module-level path constants so tests never touch the
    real ~/.config/kavi state."""
    legacy_path = tmp_path / "graph_subscription.json"
    sub_dir = tmp_path / "subscriptions"
    sub_dir.mkdir()
    monkeypatch.setattr(graph_client, "LEGACY_SUBSCRIPTION_STATE_PATH", legacy_path)
    monkeypatch.setattr(graph_client, "SUBSCRIPTION_STATE_DIR", sub_dir)
    return legacy_path, sub_dir


def test_migration_copies_legacy_to_per_account_file(isolated_paths):
    legacy_path, sub_dir = isolated_paths
    legacy_path.write_text(json.dumps(VALID_LEGACY))
    _migrate_legacy_subscription_state_forward("megha@example.com")
    target = sub_dir / "megha@example.com.json"
    assert target.exists(), "per-account file should be created"
    persisted = json.loads(target.read_text())
    assert persisted["subscription_id"] == "abc-123"
    assert persisted["account"] == "megha@example.com"
    # Legacy file is archived, not deleted.
    assert not legacy_path.exists()
    archived = list(legacy_path.parent.glob("graph_subscription.json.migrated-*.bak"))
    assert len(archived) == 1


def test_migration_preserves_target_and_archives_legacy_when_both_exist(isolated_paths):
    """Target exists AND legacy exists (mid-migration state from a previous
    boot that crashed between phase 1 and phase 2). Phase 1 is a no-op for
    the target; phase 2 archives the legacy. P2.8 transactional resume."""
    legacy_path, sub_dir = isolated_paths
    target = sub_dir / "megha@example.com.json"
    existing = {
        "subscription_id": "existing-sub",
        "expiration_dt": "2026-06-01T00:00:00Z",
        "client_state": "x",
    }
    target.write_text(json.dumps(existing))
    legacy_path.write_text(json.dumps(VALID_LEGACY))
    _migrate_legacy_subscription_state_forward("megha@example.com")
    # Target unchanged.
    assert json.loads(target.read_text())["subscription_id"] == "existing-sub"
    # Legacy file is archived (resume completes the half-finished migration).
    assert not legacy_path.exists()
    archived = list(legacy_path.parent.glob("graph_subscription.json.migrated-*.bak"))
    assert len(archived) == 1


def test_migration_no_op_when_legacy_absent(isolated_paths):
    legacy_path, sub_dir = isolated_paths
    _migrate_legacy_subscription_state_forward("megha@example.com")
    assert list(sub_dir.iterdir()) == []
    assert not legacy_path.exists()


def test_migration_skips_corrupt_legacy(isolated_paths, caplog):
    legacy_path, sub_dir = isolated_paths
    legacy_path.write_text("{not valid json")
    with caplog.at_level("ERROR"):
        _migrate_legacy_subscription_state_forward("megha@example.com")
    target = sub_dir / "megha@example.com.json"
    assert not target.exists()
    # Corrupt legacy file is left in place for forensics, not migrated.
    assert legacy_path.exists()
    assert any("corrupt" in r.message for r in caplog.records)


def test_migration_skips_when_required_keys_missing(isolated_paths, caplog):
    legacy_path, sub_dir = isolated_paths
    bad = {"subscription_id": "abc"}  # missing expiration_dt + client_state
    legacy_path.write_text(json.dumps(bad))
    with caplog.at_level("ERROR"):
        _migrate_legacy_subscription_state_forward("megha@example.com")
    target = sub_dir / "megha@example.com.json"
    assert not target.exists()
    assert legacy_path.exists()
    assert any("missing required keys" in r.message for r in caplog.records)


# ---- P2.8 transactional / two-phase tests ---------------------------------


def test_resume_after_phase1_succeeded_phase2_crashed(isolated_paths):
    """Simulate a previous boot that wrote the target but crashed before
    archiving the legacy file. The next boot must see both files and
    archive the legacy."""
    legacy_path, sub_dir = isolated_paths
    target = sub_dir / "megha@example.com.json"
    # Both exist (mid-migration state).
    target.write_text(json.dumps(VALID_LEGACY))
    legacy_path.write_text(json.dumps(VALID_LEGACY))
    # Re-run the full migration; phase 1 returns True (target exists),
    # phase 2 archives the legacy.
    _migrate_legacy_subscription_state_forward("megha@example.com")
    assert not legacy_path.exists()
    archived = list(legacy_path.parent.glob("graph_subscription.json.migrated-*.bak"))
    assert len(archived) == 1


def test_phase1_idempotent_when_target_already_exists(isolated_paths):
    legacy_path, sub_dir = isolated_paths
    target = sub_dir / "megha@example.com.json"
    existing = {**VALID_LEGACY, "subscription_id": "existing-sub"}
    target.write_text(json.dumps(existing))
    legacy_path.write_text(json.dumps(VALID_LEGACY))
    assert _populate_target_from_legacy("megha@example.com") is True
    # Target unchanged.
    assert json.loads(target.read_text())["subscription_id"] == "existing-sub"


def test_phase1_returns_false_when_neither_file_exists(isolated_paths):
    legacy_path, sub_dir = isolated_paths
    assert _populate_target_from_legacy("megha@example.com") is False


def test_phase2_archives_orphaned_legacy(isolated_paths):
    """Phase 2 alone — archive the legacy file regardless of whether
    phase 1 ran in this boot."""
    legacy_path, sub_dir = isolated_paths
    legacy_path.write_text(json.dumps(VALID_LEGACY))
    _archive_legacy_if_orphaned()
    assert not legacy_path.exists()
    archived = list(legacy_path.parent.glob("graph_subscription.json.migrated-*.bak"))
    assert len(archived) == 1


def test_phase2_no_op_when_legacy_absent(isolated_paths):
    legacy_path, sub_dir = isolated_paths
    _archive_legacy_if_orphaned()
    archived = list(legacy_path.parent.glob("graph_subscription.json.migrated-*.bak"))
    assert archived == []
