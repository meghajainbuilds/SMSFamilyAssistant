"""Smoke tests for scripts/render_cost_dashboard.py.

Updated 2026-05-06: the dashboard now scans a list of JSONL files (not a
single runs.jsonl) so it can pick up usage rows that live in per-
capability eval surfaces (eval-inbox-judgments.jsonl, eval-persona-
outbound-judgments.jsonl, etc.). Tests cover both the single-file
override and the multi-file scan path."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make scripts/ importable as a top-level package without an __init__.py.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import render_cost_dashboard as dash  # type: ignore  # noqa: E402


def test_dashboard_produces_non_empty_html(tmp_path: Path) -> None:
    runs_path = tmp_path / "runs.jsonl"
    now = datetime.now(timezone.utc)
    rows = [
        {
            "ts": (now - timedelta(days=1)).isoformat(),
            "event_type": "email_to_tasks",
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 300,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 400,
            },
        },
        {
            "ts": (now - timedelta(days=2)).isoformat(),
            "event_type": "classify_qa_reply",
            "model": "claude-sonnet-4-6",
            "usage": {
                "input_tokens": 200,
                "output_tokens": 30,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
        # Outside the 7-day window — should not be counted.
        {
            "ts": (now - timedelta(days=15)).isoformat(),
            "event_type": "email_to_tasks",
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 999999, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        },
    ]
    runs_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    out_path = tmp_path / "cost.html"
    rc = dash.main(["--runs-path", str(runs_path), "--out", str(out_path)])
    assert rc == 0
    html = out_path.read_text()
    assert len(html) > 500
    # Top-line summary line should be present.
    assert "Last 7 days:" in html
    # Both call types should appear in the body.
    assert "email_to_tasks" in html
    assert "classify_qa_reply" in html
    # Out-of-window row excluded.
    assert "999,999" not in html


def test_dashboard_scans_multiple_files_under_root(tmp_path: Path) -> None:
    """When invoked with --scan-root, every JSONL under that root is read
    and rows with `usage` blocks aggregate across files. This is the
    fix for the 2026-05-06 regression where pointing at runtime-events.jsonl
    alone produced $0.00 even though usage lived in sibling files."""
    now = datetime.now(timezone.utc)
    eval_dir = tmp_path / "evals"
    inbox_path = eval_dir / "inbox-to-task" / "eval-inbox-judgments.jsonl"
    persona_path = eval_dir / "kavi-persona" / "eval-persona-outbound-judgments.jsonl"
    metrics_path = tmp_path / "metrics" / "runtime-events.jsonl"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    persona_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    # Inbox-to-task row (has usage).
    inbox_path.write_text(json.dumps({
        "ts": (now - timedelta(hours=4)).isoformat(),
        "capability": "inbox-to-task",
        "decision": "task",
        "usage": {
            "input_tokens": 800,
            "output_tokens": 100,
            "cache_creation_input_tokens": 400,
            "cache_read_input_tokens": 0,
        },
    }) + "\n")
    # Persona-outbound row (has usage in `_usage`-shaped fallback).
    persona_path.write_text(json.dumps({
        "ts": (now - timedelta(hours=2)).isoformat(),
        "capability": "kavi-persona",
        "kind": "conversational",
        "usage": {
            "input_tokens": 200,
            "output_tokens": 30,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 1500,
        },
    }) + "\n")
    # Operational event with NO usage — must be silently skipped.
    metrics_path.write_text(json.dumps({
        "ts": (now - timedelta(hours=1)).isoformat(),
        "event_type": "periodic_summary",
        "is_rollup": True,
    }) + "\n")

    out_path = tmp_path / "cost.html"
    rc = dash.main(["--scan-root", str(tmp_path), "--out", str(out_path)])
    assert rc == 0
    html = out_path.read_text()
    # Both capability buckets show up.
    assert "inbox-to-task" in html
    assert ("conversational" in html) or ("kavi-persona" in html)
    # rows_with_usage = 2 (inbox + persona); the periodic_summary row was
    # excluded because it had no `usage` key.
    assert "(2 with usage)" in html


def test_dashboard_handles_underscore_usage_field(tmp_path: Path) -> None:
    """Some legacy classify_* rows persisted usage under `_usage` (not
    `usage`). The dashboard should still pick those up so we don't blind-
    drop a chunk of real cost."""
    now = datetime.now(timezone.utc)
    runs_path = tmp_path / "evals" / "kavi-persona" / "eval-persona-action-intent.jsonl"
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    runs_path.write_text(json.dumps({
        "ts": (now - timedelta(hours=3)).isoformat(),
        "kind": "classify_action_intent",
        "_usage": {
            "input_tokens": 500,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }) + "\n")

    out_path = tmp_path / "cost.html"
    rc = dash.main(["--scan-root", str(tmp_path), "--out", str(out_path)])
    assert rc == 0
    html = out_path.read_text()
    assert "(1 with usage)" in html
    assert "classify_action_intent" in html
