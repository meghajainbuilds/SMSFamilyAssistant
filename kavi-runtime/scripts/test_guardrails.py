"""Offline smoke tests for step 12 guardrails (2026-04-29).

Exercises the three guardrails without needing the live Anthropic API or MS Graph.
The pause-intent classifier (test case 2 from specs.md) needs a live Sonnet call;
verify that one against the running runtime.

Run: cd kavi-runtime && python3 scripts/test_guardrails.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the package importable when running directly from scripts/.
sys.path.insert(0, str(Path(__file__).parent.parent))

from kavi_runtime.runtime import guardrails  # noqa: E402
from kavi_runtime.state import (  # noqa: E402
    LOCAL_TZ,
    load_imessage_state,
    save_imessage_state,
)


PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, PASS if condition else FAIL, detail))
    marker = "✓" if condition else "✗"
    print(f"  {marker} {name}{(' — ' + detail) if detail else ''}")


def section(name: str) -> None:
    print(f"\n{name}")


def make_temp_workspace() -> tuple[Path, Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="kavi-guardrail-test-"))
    state_path = tmp / "imessage-state.json"
    runs_path = tmp / "runs.jsonl"
    return tmp, state_path, runs_path


def write_run_row(runs_path: Path, ts_utc: datetime, usage: dict[str, int]) -> None:
    row = {
        "run_id": ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_type": "email_arrived",
        "ts": ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "task",
        "usage": usage,
        "annotations": None,
    }
    with open(runs_path, "a") as f:
        f.write(json.dumps(row) + "\n")


def reset_webhook_state() -> None:
    """The flood counters live in module-level deque/list; reset between tests."""
    with guardrails._lock:
        guardrails._webhook_arrivals.clear()
        guardrails._deferred_queue.clear()
        guardrails._flood_alert_last_sent = 0.0


# ---------- Test 1: Spend cap trip + bypass ----------

def test_spend_cap() -> None:
    section("Test 1 — spend cap trip + bypass logic")
    _, state_path, runs_path = make_temp_workspace()

    # Cap at $0.50 → over cap. Trip path: set pause + alert.
    cap = 0.50
    config = {
        "guardrails": {"monthly_anthropic_spend_usd_cap": cap},
        "paths": {"runs_jsonl": str(runs_path), "imessage_state": str(state_path)},
        "schedule": {"quiet_hours_start": "23:00", "quiet_hours_end": "07:00"},
    }

    # 2026-06-10 rewire: monthly spend reads the persisted running-total
    # counter (spend_state), not runs.jsonl rows. Sonnet 4.6 input @ $3/M:
    # two 100K-input calls = $0.60.
    from kavi_runtime.spend_state import record_spend
    record_spend(config, 0.30, model="claude-sonnet-4-6", call_type="email_to_tasks")
    record_spend(config, 0.30, model="claude-sonnet-4-6", call_type="email_to_tasks")

    spend = guardrails.compute_monthly_spend_usd(config)
    check("spend tracker sums correctly across calls", abs(spend - 0.60) < 0.001,
          f"computed=${spend:.4f}, expected=$0.6000")

    check("not paused before trip", not guardrails.is_paused(state_path))
    check("bypass not active before trip", not guardrails.is_spend_cap_bypassed(state_path))

    # Simulate the trip path manually (don't need to drag bb client into the test).
    if spend >= cap:
        guardrails.set_paused(state_path, reason="spend_cap_exceeded", spend_at_trip=spend)
        guardrails.log_trip(runs_path, "monthly_spend_cap", cap, round(spend, 4),
                            "paused_auto_runs")

    pause_state = guardrails.get_pause_state(state_path)
    check("paused flag set after trip", pause_state["paused"])
    check("pause reason recorded", pause_state["paused_reason"] == "spend_cap_exceeded")

    state_data = load_imessage_state(state_path)
    check("paused_spend_usd recorded", state_data.get("paused_spend_usd") == round(spend, 4))

    # Resume path: clear pause + set monthly bypass.
    drain = guardrails.clear_paused(state_path, set_spend_bypass=True)
    check("clear_paused returns drained queue (empty in this test)", drain["drained_queue"] == [])
    check("paused flag cleared after resume", not guardrails.is_paused(state_path))
    check("spend cap bypass active for this month", guardrails.is_spend_cap_bypassed(state_path))

    # Verify trip telemetry row landed in runs.jsonl.
    rows = [json.loads(l) for l in open(runs_path) if l.strip()]
    trip_rows = [r for r in rows if r.get("event_type") == "guardrail_trip"]
    check("guardrail_trip row written to runs.jsonl", len(trip_rows) == 1,
          f"saw {len(trip_rows)}")


# ---------- Test 2: Backlog enqueue + drain on resume ----------

def test_backlog_enqueue_drain() -> None:
    section("Test 2 — backlog enqueue while paused + drain on resume")
    _, state_path, _ = make_temp_workspace()

    guardrails.set_paused(state_path, reason="spend_cap_exceeded", spend_at_trip=30.5)

    fake_notif_1 = {"resourceData": {"id": "msg-1"}, "subscriptionId": "sub-1"}
    fake_notif_2 = {"resourceData": {"id": "msg-2"}, "subscriptionId": "sub-2"}
    guardrails.enqueue_paused_email(state_path, fake_notif_1)
    guardrails.enqueue_paused_email(state_path, fake_notif_2)

    state = load_imessage_state(state_path)
    queue = state.get("paused_email_queue", [])
    check("queue holds both notifications across save/load", len(queue) == 2,
          f"queue size = {len(queue)}")
    check("notifications stored verbatim",
          queue[0]["resourceData"]["id"] == "msg-1" and queue[1]["resourceData"]["id"] == "msg-2")

    drain = guardrails.clear_paused(state_path, set_spend_bypass=True)
    check("clear_paused drains exactly the queued notifications",
          [n["resourceData"]["id"] for n in drain["drained_queue"]] == ["msg-1", "msg-2"])
    check("queue empty after drain",
          load_imessage_state(state_path).get("paused_email_queue", []) == [])


# ---------- Test 3: Webhook flood — counter, threshold, deferred queue ----------

def test_webhook_flood() -> None:
    section("Test 3 — webhook flood throttle")
    reset_webhook_state()

    config = {"guardrails": {"webhook_flood_per_hour": 5}}

    for i in range(5):
        guardrails.record_webhook_arrival()
    check("flood not active at threshold (==5)", not guardrails.webhook_flood_active(config))

    guardrails.record_webhook_arrival()
    check("flood active above threshold (>5)", guardrails.webhook_flood_active(config))
    check("count_last_hour reflects all arrivals", guardrails.webhook_count_last_hour() == 6,
          f"count={guardrails.webhook_count_last_hour()}")

    guardrails.enqueue_deferred({"resourceData": {"id": "deferred-1"}})
    guardrails.enqueue_deferred({"resourceData": {"id": "deferred-2"}})
    check("deferred queue grew to 2", guardrails.deferred_queue_size() == 2)

    popped = guardrails.pop_deferred()
    check("pop_deferred returns first enqueued", popped["resourceData"]["id"] == "deferred-1")
    check("deferred queue size now 1", guardrails.deferred_queue_size() == 1)

    # Flood-alert debounce: first call returns True, second call within an hour returns False.
    check("flood alert debounce: first call True", guardrails.should_send_flood_alert())
    check("flood alert debounce: second call False (within hour)",
          not guardrails.should_send_flood_alert())


# ---------- Test 4: Token cap pre-check (estimate + threshold) ----------

def test_token_cap_estimate() -> None:
    section("Test 4 — token cap pre-check (estimate)")

    # estimate_input_tokens: char/4 heuristic.
    check("empty input estimates to 0", guardrails.estimate_input_tokens("") == 0)
    check("4 chars estimate to 1 token", guardrails.estimate_input_tokens("abcd") == 1)
    check("200K chars estimate to 50K tokens",
          guardrails.estimate_input_tokens("x" * 200_000) == 50_000)
    check("multi-arg estimate sums lengths",
          guardrails.estimate_input_tokens("abcd", "abcd") == 2)


# ---------- Test 5: Pending alerts queue ----------

def test_pending_alerts() -> None:
    section("Test 5 — quiet-hours pending alerts queue")
    _, state_path, _ = make_temp_workspace()

    guardrails.enqueue_pending_alert(state_path, {
        "text": "Spend cap tripped at $30.42",
        "kind": "spend_cap_trip",
    })
    guardrails.enqueue_pending_alert(state_path, {
        "text": "Token blowup on email subject 'Long thread'",
        "kind": "token_cap_trip",
    })

    state = load_imessage_state(state_path)
    check("pending_alerts queue has 2 items", len(state.get("pending_alerts", [])) == 2)
    check("alerts persist queued_at timestamps",
          all("queued_at" in a for a in state["pending_alerts"]))

    drained = guardrails.drain_pending_alerts(state_path)
    check("drain returns both alerts in order",
          [a["kind"] for a in drained] == ["spend_cap_trip", "token_cap_trip"])
    check("queue empty after drain",
          load_imessage_state(state_path).get("pending_alerts", []) == [])


# ---------- Test 6: State file backfill ----------

def test_state_backfill() -> None:
    section("Test 6 — older state files backfill new fields lazily")
    tmp, state_path, _ = make_temp_workspace()

    # Write a state file in the pre-step-12 shape (no auto_runs_paused / paused_email_queue).
    legacy_state = {
        "questions": [],
        "summary_queue": [],
        "last_send_at": None,
        "last_summary_send_at": None,
    }
    state_path.write_text(json.dumps(legacy_state))

    state = load_imessage_state(state_path)
    check("auto_runs_paused backfilled to False", state["auto_runs_paused"] is False)
    check("paused_since backfilled to None", state["paused_since"] is None)
    check("paused_email_queue backfilled to empty list", state["paused_email_queue"] == [])
    check("pending_alerts backfilled to empty list", state["pending_alerts"] == [])
    check("spend_cap_bypass_month backfilled to None", state["spend_cap_bypass_month"] is None)
    check("legacy fields preserved", state["questions"] == [] and state["summary_queue"] == [])


# ---------- Run all + summary ----------

def main() -> int:
    test_spend_cap()
    test_backlog_enqueue_drain()
    test_webhook_flood()
    test_token_cap_estimate()
    test_pending_alerts()
    test_state_backfill()

    passed = sum(1 for _, r, _ in results if r == PASS)
    failed = sum(1 for _, r, _ in results if r == FAIL)
    print(f"\n{'=' * 60}")
    print(f"RESULT: {passed} passed, {failed} failed")
    if failed:
        print("\nFailures:")
        for name, status, detail in results:
            if status == FAIL:
                print(f"  ✗ {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
