"""Session-level test isolation.

Why: 2026-05-06 PM. A pytest run against the live runtime wrote fixture
Anthropic call rows (input_tokens=10, latency_ms=0, fictional senders)
into the prod structured log AND the cost dashboard rollup. The
fixture data was indistinguishable from real spend.

This conftest forces every test in the session to run with:

  - `KAVI_ENV=test` so structured_log records carry env="test"
    (P3.10 cost dashboard filter excludes these from spend rollups).
  - `structured_log._RUNTIME_ENV` aligned to "test" so code paths that
    read the in-memory cache (rather than the env var on every call) also
    write env="test".
  - `structured_log.default_log_path` redirected to a session tmp file so
    no test ever writes to ~/Library/Logs/kavi-runtime.json.log even if
    the test code bypasses per-test fixtures.

Per-test monkeypatches continue to override these session defaults.
There is intentionally no opt-out flag — tests that need real prod state
must run outside pytest.

Pairs with P3.10 (env tagging on Anthropic calls). Together they close
the test→prod-pollution failure class.
"""

from __future__ import annotations


# Household identity (phones, emails) lives in config, never in code. Tests
# run against the fictional example household; set before any kavi_runtime
# import so module-level handle sets load from it (2026-09-25).
import os as _os, pathlib as _pathlib
_os.environ.setdefault(
    "KAVI_HOUSEHOLD_CONFIG",
    str(_pathlib.Path(__file__).resolve().parent.parent / "config.example.yaml"),
)



import os
import shutil
import tempfile
from pathlib import Path

# Module-level so pytest_unconfigure can clean up.
_tmp_log_dir: Path | None = None
_original_default_log_path = None
_original_runtime_env: str | None = None


def pytest_configure(config):
    """Runs once before test collection. Patch the prod paths + force the
    test env tag. Idempotent — running pytest twice in the same process
    re-applies cleanly."""
    global _tmp_log_dir, _original_default_log_path, _original_runtime_env

    os.environ.setdefault("KAVI_ENV", "test")

    from kavi_runtime import structured_log

    _tmp_log_dir = Path(tempfile.mkdtemp(prefix="kavi_test_log_"))
    tmp_log = _tmp_log_dir / "kavi-runtime.json.log"

    _original_default_log_path = structured_log.default_log_path
    structured_log.default_log_path = lambda: tmp_log

    _original_runtime_env = structured_log._RUNTIME_ENV
    structured_log.set_runtime_env_for_test("test")


def pytest_unconfigure(config):
    """Runs once after the session. Restore originals + clean up tmp."""
    global _tmp_log_dir
    from kavi_runtime import structured_log

    if _original_default_log_path is not None:
        structured_log.default_log_path = _original_default_log_path
    if _original_runtime_env is not None:
        structured_log.set_runtime_env_for_test(_original_runtime_env)

    if _tmp_log_dir and _tmp_log_dir.exists():
        shutil.rmtree(_tmp_log_dir, ignore_errors=True)
        _tmp_log_dir = None
