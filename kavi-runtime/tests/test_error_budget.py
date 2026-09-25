"""Tests for kavi_runtime.error_budget — daily 24h rollup of structured log."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kavi_runtime.error_budget import compute_rollup, render_markdown


def _write_log(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_rollup_computes_success_rate_and_p95(tmp_path: Path) -> None:
    log_path = tmp_path / "kavi.json.log"
    now = datetime(2026, 5, 6, 7, 0, tzinfo=timezone.utc)
    one_hour = now - timedelta(hours=1)

    # 4 email_arrived: 3 done, 1 failed. Latency varies.
    events = [
        {"ts": (one_hour + timedelta(seconds=0)).isoformat(),
         "category": "email", "event": "email_arrived_start"},
        {"ts": (one_hour + timedelta(seconds=2)).isoformat(),
         "category": "email", "event": "email_arrived_done"},
        {"ts": (one_hour + timedelta(seconds=10)).isoformat(),
         "category": "email", "event": "email_arrived_start"},
        {"ts": (one_hour + timedelta(seconds=15)).isoformat(),
         "category": "email", "event": "email_arrived_done"},
        {"ts": (one_hour + timedelta(seconds=20)).isoformat(),
         "category": "email", "event": "email_arrived_start"},
        {"ts": (one_hour + timedelta(seconds=120)).isoformat(),
         "category": "email", "event": "email_arrived_failed"},
        {"ts": (one_hour + timedelta(seconds=130)).isoformat(),
         "category": "email", "event": "email_arrived_start"},
        {"ts": (one_hour + timedelta(seconds=131)).isoformat(),
         "category": "email", "event": "email_arrived_done"},
        # iMessage
        {"ts": (one_hour + timedelta(seconds=200)).isoformat(),
         "category": "imessage", "event": "imessage_received_start"},
        {"ts": (one_hour + timedelta(seconds=205)).isoformat(),
         "category": "imessage", "event": "imessage_received_done"},
        # Alert
        {"ts": (one_hour + timedelta(seconds=300)).isoformat(),
         "category": "alert", "event": "spend_cap_tripped", "spend_usd": 31.4},
        # Anthropic usage
        {"ts": (one_hour + timedelta(seconds=400)).isoformat(),
         "category": "anthropic", "event": "compose_call_done",
         "input_tokens": 1000, "output_tokens": 200,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    ]
    _write_log(log_path, events)

    rollup = compute_rollup(log_path, now=now, window_hours=24)
    assert rollup["email_starts"] == 4
    assert rollup["email_done"] == 3
    assert rollup["email_failed"] == 1
    assert rollup["email_success_rate"] == 0.75
    assert rollup["imessage_starts"] == 1
    assert rollup["imessage_done"] == 1
    assert rollup["imessage_success_rate"] == 1.0
    # P95 of [2, 5, 100, 1] = 100 (well, it's of the sorted list with k=0.95*3=2.85)
    assert rollup["email_p95_latency_sec"] >= 50  # The big 100s spike dominates p95.
    assert rollup["alerts_count"] == 1
    # Anthropic spend: 1000*$3/M + 200*$15/M = 0.003 + 0.003 = $0.006
    assert rollup["anthropic_spend_usd"] is not None
    assert abs(rollup["anthropic_spend_usd"] - 0.006) < 1e-6


def test_rollup_goodhart_flag_and_render(tmp_path: Path) -> None:
    """Goodhart watch fires when an error event today >3x the 7-day daily avg.
    Render output is a non-empty string with the expected sections."""
    log_path = tmp_path / "kavi.json.log"
    now = datetime(2026, 5, 6, 7, 0, tzinfo=timezone.utc)

    events: list[dict] = []
    # 7-day baseline: 1 graph_call_failed per day for 7 days = 7 total.
    # Today (last 24h): 5 events. Daily avg = 1.0; today=5 >= 3 and >3x baseline.
    for d in range(1, 8):
        events.append({
            "ts": (now - timedelta(days=d, hours=2)).isoformat(),
            "category": "error", "event": "graph_call_failed",
        })
    # Today's 5 errors:
    for i in range(5):
        events.append({
            "ts": (now - timedelta(minutes=10 + i)).isoformat(),
            "category": "error", "event": "graph_call_failed",
        })
    _write_log(log_path, events)

    rollup = compute_rollup(log_path, now=now, window_hours=24)
    assert any("graph_call_failed" in f for f in rollup["goodhart_flags"])
    md = render_markdown(rollup)
    assert "Kavi runtime" in md
    assert "Goodhart watch" in md
    assert "graph_call_failed" in md


def test_rollup_renders_cost_summary_line(tmp_path: Path) -> None:
    """Item 1b (2026-05-06 batch 6): the 24h rollup email body must
    carry one line with last-24h spend + 7-day total + daily average."""
    log_path = tmp_path / "kavi.json.log"
    now = datetime(2026, 5, 6, 7, 0, tzinfo=timezone.utc)

    events = []
    # Today: 1 row, 100k input tokens => $0.30.
    events.append({
        "ts": (now - timedelta(hours=2)).isoformat(),
        "category": "anthropic", "event": "call_done",
        "input_tokens": 100_000, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    # 6 days ago: 1 row, 200k input => $0.60. Together with today: 7-day = $0.90.
    events.append({
        "ts": (now - timedelta(days=6, hours=1)).isoformat(),
        "category": "anthropic", "event": "call_done",
        "input_tokens": 200_000, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    _write_log(log_path, events)

    rollup = compute_rollup(log_path, now=now, window_hours=24)
    assert rollup["anthropic_spend_usd"] is not None
    assert abs(rollup["anthropic_spend_usd"] - 0.30) < 1e-6
    assert rollup["anthropic_spend_7day_usd"] is not None
    assert abs(rollup["anthropic_spend_7day_usd"] - 0.90) < 1e-6
    assert rollup["anthropic_spend_daily_avg_usd"] is not None
    assert abs(rollup["anthropic_spend_daily_avg_usd"] - (0.90 / 7.0)) < 1e-6

    md = render_markdown(rollup)
    assert "Anthropic spend last 24h:" in md
    assert "7-day total:" in md
    assert "Daily average:" in md
    assert "$0.30" in md
    assert "$0.90" in md


def test_rollup_spend_spike_goodhart_fires_above_seven_dollars(tmp_path: Path) -> None:
    """Item 5 (2026-05-06 batch 6): when 24h spend exceeds $7, the
    Goodhart-watch section appends a flag line so Megha sees the spike
    in the 7am email even before the dashboard tells her."""
    log_path = tmp_path / "kavi.json.log"
    now = datetime(2026, 5, 6, 7, 0, tzinfo=timezone.utc)

    # 3M input tokens at $3/M = $9.00 today. Above the $7 threshold.
    events = [{
        "ts": (now - timedelta(hours=2)).isoformat(),
        "category": "anthropic", "event": "call_done",
        "input_tokens": 3_000_000, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }]
    _write_log(log_path, events)

    rollup = compute_rollup(log_path, now=now, window_hours=24)
    flags = rollup["goodhart_flags"]
    assert any("anthropic_spend_24h" in f for f in flags), flags
    md = render_markdown(rollup)
    assert "anthropic_spend_24h" in md
