"""Tests for the /runtime-metrics/cost HTTP endpoint (item 1a, 2026-05-06 batch 6).

Why this exists: Megha visits the cost dashboard from her laptop over
Tailscale; the endpoint must return real HTML with the expected shape so
she doesn't have to SSH into Kavi's Mac to read $ numbers.

Coverage:
  1. Endpoint returns 200 + text/html + a `<table>` tag when JSONL data
     exists under a temp scan root (proves wiring + render path).
  2. Endpoint returns 200 + text/html stub when no JSONL is found
     (proves we never 500 on a fresh install / missing data).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from capabilities.realtime_kavi.server import build_app


def _make_config(tmp_path: Path) -> dict:
    """Minimal config that build_app() accepts. Paths point at tmp."""
    return {
        "bluebubbles": {"inbound_webhook_path": "/bluebubbles/inbound"},
        "paths": {
            "eval_inbox_judgments_jsonl": str(tmp_path / "eval-inbox.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "eval-persona.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "eval-persona-inbound.jsonl"),
        },
    }


def test_runtime_metrics_cost_endpoint_returns_html_when_data_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """When JSONL rows with `usage` exist under the configured scan root,
    the endpoint returns a 200 + Content-Type text/html + a real <table>
    tag carrying daily breakdown."""
    # Stage one JSONL with a usage row inside the last 7 days.
    eval_dir = tmp_path / "evals" / "inbox-to-task"
    eval_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    (eval_dir / "eval-inbox-judgments.jsonl").write_text(json.dumps({
        "ts": (now - timedelta(hours=2)).isoformat(),
        "capability": "inbox-to-task",
        "decision": "task",
        "usage": {
            "input_tokens": 800,
            "output_tokens": 100,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }) + "\n")

    # Patch find_jsonl_paths so the dashboard scans our tmp dir, not the
    # real Kavi paths.
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import render_cost_dashboard as dash  # type: ignore
    monkeypatch.setattr(
        dash, "find_jsonl_paths",
        lambda: list((tmp_path / "evals").rglob("*.jsonl")),
    )

    client = TestClient(build_app(_make_config(tmp_path)))
    resp = client.get("/runtime-metrics/cost")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "<table>" in body
    assert "Last 7 days:" in body
    assert "inbox-to-task" in body


def test_runtime_metrics_cost_endpoint_returns_stub_when_no_data(
    tmp_path: Path, monkeypatch
) -> None:
    """When no JSONL files match, return a 200 + the empty-state HTML
    stub. We must never 500 the dashboard on Megha — a missing dataset
    is a fact, not a crash."""
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import render_cost_dashboard as dash  # type: ignore
    monkeypatch.setattr(dash, "find_jsonl_paths", lambda: [])

    client = TestClient(build_app(_make_config(tmp_path)))
    resp = client.get("/runtime-metrics/cost")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "No cost data found" in body
