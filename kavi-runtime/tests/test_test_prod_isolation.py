"""test_test_prod_isolation.py — verifies the conftest.py guards from P1.5.

The session conftest must force KAVI_ENV=test, set the in-memory env tag
to "test", and redirect the default structured log path away from
~/Library/Logs/kavi-runtime.json.log. If any of these regress, fixture
data leaks into the prod cost dashboard (2026-05-06 PM incident).
"""

from __future__ import annotations

import os
from pathlib import Path

from kavi_runtime import structured_log


def test_kavi_env_is_test_during_pytest():
    assert os.environ.get("KAVI_ENV") == "test"


def test_runtime_env_is_test_during_pytest():
    """The in-memory tag must match the env var so log_event stamps env=test."""
    assert structured_log.runtime_env() == "test"


def test_default_log_path_is_session_tmp():
    """structured_log.default_log_path() must return a tmp path, not the
    real ~/Library/Logs/kavi-runtime.json.log."""
    path = Path(structured_log.default_log_path())
    real_prod_log = Path.home() / "Library" / "Logs" / "kavi-runtime.json.log"
    assert path != real_prod_log, (
        f"default_log_path returned the prod log path: {path}. "
        f"Conftest redirect failed; tests will write to prod."
    )


def test_log_event_writes_env_test_to_default_path():
    """End-to-end: a log_event call without an explicit path must write
    env=test into the conftest tmp file, not into prod."""
    structured_log.log_event(
        "anthropic", "call_start", call_type="test_isolation_check", model="x",
    )
    path = Path(structured_log.default_log_path())
    assert path.exists(), "tmp log path was never created"
    content = path.read_text()
    assert '"env": "test"' in content
    assert '"call_type": "test_isolation_check"' in content
