"""Tests for the /status HTTP endpoint (added 2026-05-26).

Why this exists. The 60-min "no activity" iMessage alert path was removed
on 2026-05-26 because it generated 75 of 89 persona-eval rows over 7 days,
all false positives. The replacement is pull-based: Megha hits /status
from her phone on demand and sees eight fields describing the current
state. These tests prove:

  1. GET /status returns 200 + text/html + Cache-Control: no-store.
  2. The response body contains all eight field labels (test will fail
     loudly if a future refactor drops one).
  3. Empty state files do not crash the endpoint; the affected field
     renders "no data yet" instead.
  4. The "no-activity-alert cron job is gone" regression: the scheduler
     job list never contains "invocation-floor-check" again.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from kavi_runtime import runtime_status
from capabilities.realtime_kavi.server import build_app


def _make_config(tmp_path: Path) -> dict:
    """Minimal config that build_app() accepts. All paths point at tmp."""
    return {
        "bluebubbles": {"inbound_webhook_path": "/bluebubbles/inbound"},
        "imessage": {"megha_phone": "+15555550101"},
        "paths": {
            "eval_inbox_judgments_jsonl": str(tmp_path / "eval-inbox.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "inbound.jsonl"),
            "runtime_events_jsonl": str(tmp_path / "runs.jsonl"),
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "structured_log": str(tmp_path / "kavi-runtime.json.log"),
        },
    }


# ---- Eight expected field labels. The status page MUST render each one
# exactly as written so the test fails loudly if a refactor drops or
# renames one. Megha's phone bookmark assumes this shape.
EXPECTED_LABELS = [
    "Runtime time now",
    "Last inbound iMessage",
    "Last outbound iMessage",
    "Last cron tick",
    "Last successful Claude API call",
    "Last successful MS Graph call",
    "Today's Anthropic spend",
    "This month's Anthropic spend",
]


def test_status_endpoint_returns_200_html_with_no_store(tmp_path: Path) -> None:
    """Smoke test: endpoint wired, HTML content-type, no caching."""
    runtime_status.reset_for_test()
    client = TestClient(build_app(_make_config(tmp_path)))
    resp = client.get("/status")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("cache-control") == "no-store"


def test_status_endpoint_renders_all_eight_field_labels(tmp_path: Path) -> None:
    """Each of the eight labels appears verbatim in the response body."""
    runtime_status.reset_for_test()
    client = TestClient(build_app(_make_config(tmp_path)))
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.text
    for label in EXPECTED_LABELS:
        assert label in body, f"missing status field label: {label!r}"


def test_status_endpoint_handles_empty_state_files(tmp_path: Path) -> None:
    """When inbound / outbound / runs JSONL files do not exist, the
    affected fields render 'no data yet' rather than crashing the page.
    This is the fresh-install case Megha will hit on first deploy."""
    runtime_status.reset_for_test()
    client = TestClient(build_app(_make_config(tmp_path)))
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.text
    # Inbound/outbound/cron-tick/claude/graph all have no source data yet.
    # Each should print "no data yet" against its label.
    for label in [
        "Last inbound iMessage",
        "Last outbound iMessage",
        "Last cron tick",
        "Last successful Claude API call",
        "Last successful MS Graph call",
    ]:
        assert f"{label}: no data yet" in body, (
            f"empty-state field {label!r} should render 'no data yet', "
            f"got body:\n{body}"
        )


def test_status_endpoint_renders_recent_inbound_when_jsonl_has_rows(tmp_path: Path) -> None:
    """When eval-persona-inbound.jsonl has at least one row, the
    'Last inbound iMessage' field shows the row's timestamp + sender."""
    runtime_status.reset_for_test()
    cfg = _make_config(tmp_path)
    inbound_path = Path(cfg["paths"]["eval_persona_inbound_jsonl"])
    inbound_path.parent.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    inbound_path.write_text(json.dumps({
        "inbound_id": "i_test",
        "ts": ts,
        "sender": "+15555550101",
        "text": "ping",
    }) + "\n")

    client = TestClient(build_app(cfg))
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.text
    assert "Last inbound iMessage" in body
    assert "+15555550101" in body
    # The empty-state sentinel must NOT appear on the inbound line.
    assert "Last inbound iMessage: no data yet" not in body


def test_status_endpoint_renders_recent_outbound_when_jsonl_has_rows(tmp_path: Path) -> None:
    """When outbound JSONL has rows, 'Last outbound iMessage' shows
    timestamp + kind."""
    runtime_status.reset_for_test()
    cfg = _make_config(tmp_path)
    outbound_path = Path(cfg["paths"]["eval_persona_outbound_judgments_jsonl"])
    outbound_path.parent.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    outbound_path.write_text(json.dumps({
        "decision_id": "p_test",
        "ts": ts,
        "kind": "conversational",
        "text": "hi",
    }) + "\n")

    client = TestClient(build_app(cfg))
    resp = client.get("/status")
    body = resp.text
    assert "kind=conversational" in body
    assert "Last outbound iMessage: no data yet" not in body


def test_status_endpoint_reflects_recorded_graph_call(tmp_path: Path) -> None:
    """Once record_graph_call_ok fires, the 'Last successful MS Graph
    call' field surfaces the operation label."""
    runtime_status.reset_for_test()
    runtime_status.record_graph_call_ok("send_mail")

    client = TestClient(build_app(_make_config(tmp_path)))
    resp = client.get("/status")
    body = resp.text
    assert "op send_mail" in body
    assert "Last successful MS Graph call: no data yet" not in body


def test_status_endpoint_reflects_recorded_cron_tick(tmp_path: Path) -> None:
    """Once record_cron_tick fires, 'Last cron tick' surfaces the job id."""
    runtime_status.reset_for_test()
    runtime_status.record_cron_tick("periodic_summary")

    client = TestClient(build_app(_make_config(tmp_path)))
    resp = client.get("/status")
    body = resp.text
    assert "periodic_summary" in body
    assert "Last cron tick: no data yet" not in body


# ---- Regression: the no-activity alert cron job is gone --------------------


def test_no_activity_alert_cron_job_is_not_registered() -> None:
    """The 60-min 'no activity' iMessage alert was removed 2026-05-26.
    `_invocation_floor_check`, `maybe_alert_invocation_floor`, and the
    `invocation-floor-check` scheduler job must all stay gone.

    Phrasing: this test fails loudly if a future refactor re-introduces
    the path under any of its three names.
    """
    from kavi_runtime import handler_alerts
    from capabilities.realtime_kavi import scheduler

    # Symbol-level checks. None of these should resolve to anything
    # callable after the 2026-05-26 removal.
    assert not hasattr(scheduler, "_invocation_floor_check"), (
        "scheduler._invocation_floor_check came back. The 60-min "
        "no-activity iMessage alert was removed 2026-05-26 because it "
        "generated 75 of 89 persona-eval rows in 7 days, all false "
        "positives. Do not re-add."
    )
    assert not hasattr(handler_alerts, "maybe_alert_invocation_floor"), (
        "handler_alerts.maybe_alert_invocation_floor came back. The "
        "60-min no-activity iMessage alert was removed 2026-05-26. "
        "Silence is not failure. Use the /status page or the external "
        "healthchecks.io watchdog."
    )
    assert not hasattr(handler_alerts, "_send_invocation_floor_imessage"), (
        "handler_alerts._send_invocation_floor_imessage came back. "
        "Removed 2026-05-26 with the invocation-floor alarm itself."
    )

    # Source-level check. Reading scheduler.py is the most reliable way
    # to detect a regression without instantiating the BackgroundScheduler
    # (which would try to fire jobs that contact MS Graph and Anthropic
    # at startup). The substrings below name three independent ways a
    # regression could be introduced; the test fails if any of them
    # appears anywhere in the scheduler module.
    scheduler_src = Path(scheduler.__file__).read_text()
    forbidden_substrings = [
        "invocation-floor-check",
        "_invocation_floor_check",
        "maybe_alert_invocation_floor",
    ]
    for needle in forbidden_substrings:
        assert needle not in scheduler_src, (
            f"scheduler.py contains {needle!r}. The 60-min no-activity "
            "iMessage alert was removed 2026-05-26. Do not re-add. "
            "See kavi-runtime/backlog.md 'Runtime heartbeat' entry."
        )
