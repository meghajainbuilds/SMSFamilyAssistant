"""test_legacy_state_paths.py — verifies P1.6 legacy path detection.

Why: 2026-05-06 PM, the imessage_state file was configured to live at
~/HomeOS/.claude/imessage-state.json — a Claude Code working-tree marker,
not a state directory. Putting persistent state there caused confusion
and contributed to today's silent-failure debugging. The fix moves the
file to HomeOS/state/ and adds a startup check that warns if any file
is still at the legacy location.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capabilities.realtime_kavi.state_invariants import (
    Violation,
    check_legacy_state_paths,
    check_startup_invariants,
)


def test_no_violations_when_legacy_paths_dont_exist(tmp_path):
    """Empty result when none of the legacy paths exist on disk."""
    fake_legacy = [tmp_path / "nope1.json", tmp_path / "nope2.json"]
    violations = check_legacy_state_paths(fake_legacy)
    assert violations == []


def test_legacy_file_detected_warning(tmp_path):
    legacy = tmp_path / "imessage-state.json"
    legacy.write_text('{"questions": []}')
    violations = check_legacy_state_paths([legacy])
    assert len(violations) == 1
    v = violations[0]
    assert v.severity == "WARNING"
    assert "legacy state file" in v.message
    assert str(legacy) in v.message
    assert "HomeOS/state/" in v.message or "rsync" in (v.fix_command or "")


def test_multiple_legacy_files_each_get_a_violation(tmp_path):
    legacy_a = tmp_path / "a.json"
    legacy_b = tmp_path / "b.json"
    legacy_a.write_text("{}")
    legacy_b.write_text("{}")
    violations = check_legacy_state_paths([legacy_a, legacy_b])
    assert len(violations) == 2
    for v in violations:
        assert v.severity == "WARNING"


def test_warning_severity_does_not_flip_health_to_503(tmp_path, monkeypatch):
    """WARNING violations are surfaced but should not flip /health to
    degraded — runtime is healthy with the new path; legacy is just untidy."""
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}")

    # Patch the module-level legacy paths so check_startup_invariants
    # picks up our test file.
    from capabilities.realtime_kavi import state_invariants
    monkeypatch.setattr(state_invariants, "LEGACY_RUNTIME_STATE_PATHS", [legacy])

    violations = check_startup_invariants([], include_legacy_state_check=True)
    assert len(violations) == 1
    assert violations[0].severity == "WARNING"
    # /health logic only flips to 503 on CRITICAL — verify this would NOT.
    has_critical = any(v.severity == "CRITICAL" for v in violations)
    assert has_critical is False


def test_check_startup_invariants_can_skip_legacy_check(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}")
    from capabilities.realtime_kavi import state_invariants
    monkeypatch.setattr(state_invariants, "LEGACY_RUNTIME_STATE_PATHS", [legacy])

    violations = check_startup_invariants([], include_legacy_state_check=False)
    assert violations == []
