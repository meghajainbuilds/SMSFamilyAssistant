"""test_env_tagging.py — verifies every structured_log event carries an
env=prod|test tag, and that the cost dashboard filters out non-prod rows.

Why: 2026-05-06. A pytest run against the live runtime wrote fixture
Anthropic call rows (input_tokens=10, latency_ms=0, fictional senders) into
the prod structured log. Those rows landed in the cost dashboard's spend
rollup. The fix: every event carries an env tag, and the dashboard skips
non-prod rows. Test-suite stays explicit about its environment by setting
KAVI_ENV=test.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kavi_runtime import structured_log


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    """Redirect structured_log writes to a temp file."""
    log_path = tmp_path / "kavi-runtime.json.log"
    structured_log.reset_handlers_for_test()
    yield log_path
    structured_log.reset_handlers_for_test()


@pytest.fixture(autouse=True)
def _reset_env():
    original = structured_log._RUNTIME_ENV
    yield
    structured_log.set_runtime_env_for_test(original)


def _read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_default_env_is_prod(isolated_log):
    structured_log.set_runtime_env_for_test("prod")
    structured_log.log_event("anthropic", "call_start", path=isolated_log,
                             call_type="compose", model="x")
    records = _read_records(isolated_log)
    assert len(records) == 1
    assert records[0]["env"] == "prod"


def test_test_env_tags_test(isolated_log):
    structured_log.set_runtime_env_for_test("test")
    structured_log.log_event("anthropic", "call_start", path=isolated_log,
                             call_type="compose", model="x")
    records = _read_records(isolated_log)
    assert records[0]["env"] == "test"


def test_cost_dashboard_skips_test_env_rows(tmp_path, monkeypatch):
    """The cost dashboard's aggregate() must filter out env=test rows."""
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import render_cost_dashboard as dash  # type: ignore

    eval_dir = tmp_path / "evals" / "inbox-to-task"
    eval_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    real_row = {
        "ts": (now - timedelta(hours=2)).isoformat(),
        "capability": "inbox-to-task",
        "decision": "task",
        "env": "prod",
        "usage": {"input_tokens": 1000, "output_tokens": 200,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    test_row = {
        "ts": (now - timedelta(hours=2)).isoformat(),
        "capability": "inbox-to-task",
        "decision": "task",
        "env": "test",
        "usage": {"input_tokens": 999_999, "output_tokens": 999_999,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    (eval_dir / "eval-inbox-judgments.jsonl").write_text(
        json.dumps(real_row) + "\n" + json.dumps(test_row) + "\n",
    )
    monkeypatch.setattr(dash, "find_jsonl_paths",
                        lambda: list((tmp_path / "evals").rglob("*.jsonl")))

    result = dash.aggregate(dash.find_jsonl_paths(), days=7)
    # Only the prod row should be reflected in the rollup.
    assert result["rows_with_usage"] == 1


def test_cost_dashboard_treats_missing_env_as_prod(tmp_path, monkeypatch):
    """Legacy rows without an env field still count — preserves backward
    compatibility with logs written before the env tag landed."""
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import render_cost_dashboard as dash  # type: ignore

    eval_dir = tmp_path / "evals" / "inbox-to-task"
    eval_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    legacy_row = {
        "ts": (now - timedelta(hours=2)).isoformat(),
        "capability": "inbox-to-task",
        "decision": "task",
        # no `env` field
        "usage": {"input_tokens": 1000, "output_tokens": 200,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    (eval_dir / "eval-inbox-judgments.jsonl").write_text(json.dumps(legacy_row) + "\n")
    monkeypatch.setattr(dash, "find_jsonl_paths",
                        lambda: list((tmp_path / "evals").rglob("*.jsonl")))

    result = dash.aggregate(dash.find_jsonl_paths(), days=7)
    assert result["rows_with_usage"] == 1
