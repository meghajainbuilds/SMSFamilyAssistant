"""Tests for the passive-observation daily channel heartbeat
(rewritten 2026-06-04).

The 2026-06-03 design synthesized a ping-and-poll probe; that re-introduced
the same chat-mirror false-negative class the receipt-as-truth verify
rewrite (SHA `af66b47`) had just removed. The 2026-06-04 rewrite replaces
the synthetic probe with passive observation:

    healthy = (last_outbound_receipt_at within 24h)
           OR (last_inbound_at within 24h)

Two write-through hooks elsewhere in the runtime keep those timestamps
fresh:

  - `record_outbound_receipt(config, handle)` from `runtime/send_imessage.py`
    when `bb.send_with_verify` returns verified=True with a message_guid.
  - `record_inbound(config, handle)` from the BlueBubbles webhook handler
    in `runtime/imessage_dispatch.py`.

The daily 08:00 PT cron is a pure READ. No BlueBubbles calls, no sleep.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_channel_heartbeat.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import channel_heartbeat
from kavi_runtime.channel_heartbeat import (
    DEGRADATION_THRESHOLD_HOURS,
    RECIPIENT_GRACE_PERIOD_HOURS,
    discover_recipients,
    record_inbound,
    record_outbound_receipt,
    run_daily_channel_heartbeat,
)


# ---- Fixtures --------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "max_phone": "+15555550102",
            "own_email_addresses": ["megha@example.com"],
        },
        "paths": {
            "imessage_state": str(state_dir / "imessage-state.json"),
        },
    }


@pytest.fixture
def cfg_megha_only(tmp_path: Path) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "paths": {
            "imessage_state": str(state_dir / "imessage-state.json"),
        },
    }


@pytest.fixture(autouse=True)
def _reset_clients(monkeypatch: pytest.MonkeyPatch):
    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_graph_client", None)
    monkeypatch.setattr(_clients, "_claude_client", None)
    monkeypatch.setattr(_clients, "_bb_client", None)
    yield


def _install_clients(monkeypatch: pytest.MonkeyPatch, graph_mock):
    """Install a stub `_get_clients` that returns the graph mock. The bb
    client is a MagicMock since the new heartbeat never touches it but the
    `_get_clients` contract still returns the tuple."""
    bb_mock = MagicMock()
    claude_mock = MagicMock()
    _stub = lambda cfg: (graph_mock, claude_mock, bb_mock)
    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_get_clients", _stub)


def _state_path_for(cfg: dict[str, Any]) -> Path:
    return Path(cfg["paths"]["imessage_state"]).parent / "channel_heartbeat.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


# ---- discover_recipients ---------------------------------------------------


def test_discover_recipients_returns_megha_and_max(cfg) -> None:
    assert discover_recipients(cfg) == ["+15555550101", "+15555550102"]


def test_discover_recipients_skips_missing_max(cfg_megha_only) -> None:
    assert discover_recipients(cfg_megha_only) == ["+15555550101"]


def test_discover_recipients_dedupes_when_same_handle_in_both_slots(tmp_path) -> None:
    cfg = {
        "imessage": {"megha_phone": "+15555550101", "max_phone": "+15555550101"},
        "paths": {"imessage_state": str(tmp_path / "imessage-state.json")},
    }
    assert discover_recipients(cfg) == ["+15555550101"]


# ---- Write-through hooks ---------------------------------------------------


def test_record_outbound_receipt_creates_entry_and_sets_timestamp(cfg) -> None:
    """First outbound to a recipient lazily creates the entry, sets
    `last_outbound_receipt_at`, and seeds `recipient_added_at` so the grace
    period applies from now."""
    record_outbound_receipt(cfg, "+15555550101")
    state = json.loads(_state_path_for(cfg).read_text())
    assert "+15555550101" in state
    entry = state["+15555550101"]
    assert entry["last_outbound_receipt_at"] is not None
    assert entry["recipient_added_at"] is not None
    assert entry["last_inbound_at"] is None
    assert entry["last_alert_sent_at"] is None


def test_record_inbound_creates_entry_and_sets_timestamp(cfg) -> None:
    record_inbound(cfg, "+15555550102")
    state = json.loads(_state_path_for(cfg).read_text())
    assert "+15555550102" in state
    entry = state["+15555550102"]
    assert entry["last_inbound_at"] is not None
    assert entry["recipient_added_at"] is not None
    assert entry["last_outbound_receipt_at"] is None


def test_record_outbound_receipt_no_op_on_empty_handle(cfg) -> None:
    """Defensive: no handle → no state file write."""
    record_outbound_receipt(cfg, "")
    assert not _state_path_for(cfg).exists()


def test_record_inbound_no_op_on_empty_handle(cfg) -> None:
    record_inbound(cfg, "")
    assert not _state_path_for(cfg).exists()


# ---- Healthy paths (no alert) ----------------------------------------------


def test_healthy_via_outbound_only(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recent outbound receipt is sufficient to mark the channel healthy
    even if no inbound has been seen in 24h."""
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550101": {
            "recipient_added_at": _hours_ago_iso(96),  # past grace
            "last_outbound_receipt_at": _hours_ago_iso(2),
            "last_inbound_at": None,
            "last_alert_sent_at": None,
        },
        "+15555550102": {
            "recipient_added_at": _hours_ago_iso(96),
            "last_outbound_receipt_at": _hours_ago_iso(5),
            "last_inbound_at": None,
            "last_alert_sent_at": None,
        },
    }))

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    summary = run_daily_channel_heartbeat(cfg)

    assert sorted(summary["healthy"]) == ["+15555550101", "+15555550102"]
    assert summary["alerts_sent"] == []
    graph_mock.send_mail.assert_not_called()


def test_healthy_via_inbound_only(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """Recent inbound is sufficient even when no outbound has happened."""
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550101": {
            "recipient_added_at": _hours_ago_iso(96),
            "last_outbound_receipt_at": None,
            "last_inbound_at": _hours_ago_iso(1),
            "last_alert_sent_at": None,
        },
        "+15555550102": {
            "recipient_added_at": _hours_ago_iso(96),
            "last_outbound_receipt_at": None,
            "last_inbound_at": _hours_ago_iso(8),
            "last_alert_sent_at": None,
        },
    }))

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    summary = run_daily_channel_heartbeat(cfg)

    assert sorted(summary["healthy"]) == ["+15555550101", "+15555550102"]
    assert summary["alerts_sent"] == []
    graph_mock.send_mail.assert_not_called()


def test_healthy_via_both_directions(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550101": {
            "recipient_added_at": _hours_ago_iso(96),
            "last_outbound_receipt_at": _hours_ago_iso(1),
            "last_inbound_at": _hours_ago_iso(2),
            "last_alert_sent_at": None,
        },
        "+15555550102": {
            "recipient_added_at": _hours_ago_iso(96),
            "last_outbound_receipt_at": _hours_ago_iso(3),
            "last_inbound_at": _hours_ago_iso(4),
            "last_alert_sent_at": None,
        },
    }))

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    summary = run_daily_channel_heartbeat(cfg)

    assert sorted(summary["healthy"]) == ["+15555550101", "+15555550102"]
    graph_mock.send_mail.assert_not_called()


# ---- Alert fires on dual silence -------------------------------------------


def test_alert_fires_on_dual_silence_past_grace(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both signals stale, past grace window, no prior alert: alert fires
    for each recipient. This is the SRE safety-net firing the way it's
    supposed to."""
    stale = _hours_ago_iso(DEGRADATION_THRESHOLD_HOURS + 2)
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550101": {
            "recipient_added_at": _hours_ago_iso(RECIPIENT_GRACE_PERIOD_HOURS + 24),
            "last_outbound_receipt_at": stale,
            "last_inbound_at": stale,
            "last_alert_sent_at": None,
        },
        "+15555550102": {
            "recipient_added_at": _hours_ago_iso(RECIPIENT_GRACE_PERIOD_HOURS + 24),
            "last_outbound_receipt_at": stale,
            "last_inbound_at": stale,
            "last_alert_sent_at": None,
        },
    }))

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    summary = run_daily_channel_heartbeat(cfg)

    assert sorted(summary["alerts_sent"]) == ["+15555550101", "+15555550102"]
    assert graph_mock.send_mail.call_count == 2

    # Alert body must NOT contain a raw 10-12-digit phone-number string
    # that would trip the outbound scanner's account_number regex.
    # The body uses "ending NNNN" instead — only 4 digits, not standalone.
    for call in graph_mock.send_mail.call_args_list:
        body = call.args[2]
        # The full phone "+15555550101" or "+15555550102" must not appear
        # as a 10+-digit standalone substring in the body.
        digits_only_megha = "15555550101"
        digits_only_max = "15555550102"
        assert digits_only_megha not in body, (
            f"alert body must not contain raw phone digits; got: {body[:200]}"
        )
        assert digits_only_max not in body, (
            f"alert body must not contain raw phone digits; got: {body[:200]}"
        )


def test_alert_fires_only_for_unhealthy_recipient(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Megha healthy via recent inbound; Max silent in both directions."""
    stale = _hours_ago_iso(DEGRADATION_THRESHOLD_HOURS + 5)
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550101": {
            "recipient_added_at": _hours_ago_iso(RECIPIENT_GRACE_PERIOD_HOURS + 24),
            "last_outbound_receipt_at": None,
            "last_inbound_at": _hours_ago_iso(1),
            "last_alert_sent_at": None,
        },
        "+15555550102": {
            "recipient_added_at": _hours_ago_iso(RECIPIENT_GRACE_PERIOD_HOURS + 24),
            "last_outbound_receipt_at": stale,
            "last_inbound_at": stale,
            "last_alert_sent_at": None,
        },
    }))

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    summary = run_daily_channel_heartbeat(cfg)

    assert summary["alerts_sent"] == ["+15555550102"]
    assert "+15555550101" in summary["healthy"]
    graph_mock.send_mail.assert_called_once()


# ---- Grace period ----------------------------------------------------------


def test_no_alert_during_grace_period_regardless_of_silence(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A freshly-added recipient with NO signal at all does NOT alert
    during the 72h grace window. Cold-start does not false-fire."""
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550101": {
            "recipient_added_at": _hours_ago_iso(1),  # well within grace
            "last_outbound_receipt_at": None,
            "last_inbound_at": None,
            "last_alert_sent_at": None,
        },
        "+15555550102": {
            "recipient_added_at": _hours_ago_iso(2),
            "last_outbound_receipt_at": None,
            "last_inbound_at": None,
            "last_alert_sent_at": None,
        },
    }))

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    summary = run_daily_channel_heartbeat(cfg)

    assert summary["alerts_sent"] == []
    assert sorted(summary["alerts_skipped_grace"]) == ["+15555550101", "+15555550102"]
    graph_mock.send_mail.assert_not_called()


def test_no_alert_for_recipient_freshly_lazy_initialized(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recipient that has never been seen in state (no file, or no entry)
    falls into the grace period the moment the daily check lazily
    initializes it. This is the "fresh state file after migration" case
    on the first 08:00 PT tick post-deploy."""
    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)

    # No state file at all → both recipients fresh-initialized → grace.
    summary = run_daily_channel_heartbeat(cfg)
    assert summary["alerts_sent"] == []
    assert sorted(summary["alerts_skipped_grace"]) == ["+15555550101", "+15555550102"]
    graph_mock.send_mail.assert_not_called()


# ---- Dedupe ----------------------------------------------------------------


def test_alert_dedupes_until_recipient_becomes_healthy(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once an alert has fired for a recipient, subsequent runs that
    still find them silent must NOT re-alert."""
    stale = _hours_ago_iso(DEGRADATION_THRESHOLD_HOURS + 5)
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550102": {
            "recipient_added_at": _hours_ago_iso(RECIPIENT_GRACE_PERIOD_HOURS + 24),
            "last_outbound_receipt_at": stale,
            "last_inbound_at": stale,
            "last_alert_sent_at": _hours_ago_iso(12),  # alert already fired
        },
        "+15555550101": {
            "recipient_added_at": _hours_ago_iso(RECIPIENT_GRACE_PERIOD_HOURS + 24),
            "last_outbound_receipt_at": _hours_ago_iso(1),
            "last_inbound_at": None,
            "last_alert_sent_at": None,
        },
    }))

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    summary = run_daily_channel_heartbeat(cfg)

    assert summary["alerts_sent"] == []
    assert "+15555550102" in summary["alerts_skipped_dedupe"]
    graph_mock.send_mail.assert_not_called()


def test_health_recovery_clears_dedupe_so_future_degradation_re_alerts(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a recipient becomes healthy again, `last_alert_sent_at` is
    cleared. The next degradation cycle gets a fresh alert."""
    stale = _hours_ago_iso(DEGRADATION_THRESHOLD_HOURS + 5)
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550102": {
            "recipient_added_at": _hours_ago_iso(RECIPIENT_GRACE_PERIOD_HOURS + 24),
            "last_outbound_receipt_at": _hours_ago_iso(1),  # fresh outbound
            "last_inbound_at": stale,
            "last_alert_sent_at": _hours_ago_iso(2),  # prior alert
        },
    }))

    cfg_max_only = {
        "imessage": {
            "max_phone": "+15555550102",
            "own_email_addresses": ["megha@example.com"],
        },
        "paths": {"imessage_state": cfg["paths"]["imessage_state"]},
    }

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    run_daily_channel_heartbeat(cfg_max_only)

    state = json.loads(_state_path_for(cfg).read_text())
    assert state["+15555550102"]["last_alert_sent_at"] is None


# ---- Schema migration ------------------------------------------------------


def test_old_schema_state_file_migrates_cleanly_preserving_dedupe(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-06-04 rewrite changes the on-disk schema. Migration must:
      - drop `last_ping_temp_guid`, `last_ping_at`, `last_round_trip_at`
      - keep `last_alert_sent_at` so the dedupe state survives
      - add `recipient_added_at = now` (treat migration as fresh) so the
        grace period applies and the first post-migration check doesn't
        fire on a still-stale state file
      - add `last_outbound_receipt_at = None`, `last_inbound_at = None`
    """
    old_alert = _hours_ago_iso(4)
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550101": {
            "last_ping_temp_guid": "channel_hb-abc",
            "last_ping_at": _hours_ago_iso(26),
            "last_round_trip_at": None,
            "last_alert_sent_at": old_alert,
        },
        "+15555550102": {
            "last_ping_temp_guid": "channel_hb-def",
            "last_ping_at": _hours_ago_iso(26),
            "last_round_trip_at": None,
            "last_alert_sent_at": old_alert,
        },
    }))

    graph_mock = MagicMock()
    _install_clients(monkeypatch, graph_mock)
    summary = run_daily_channel_heartbeat(cfg)

    # No alert should fire — grace period plus existing dedupe both apply.
    assert summary["alerts_sent"] == []
    graph_mock.send_mail.assert_not_called()

    # New on-disk schema for both recipients.
    state = json.loads(_state_path_for(cfg).read_text())
    for handle in ("+15555550101", "+15555550102"):
        entry = state[handle]
        # Old keys gone.
        assert "last_ping_temp_guid" not in entry
        assert "last_ping_at" not in entry
        assert "last_round_trip_at" not in entry
        # New keys present.
        assert "recipient_added_at" in entry
        assert "last_outbound_receipt_at" in entry
        assert "last_inbound_at" in entry
        # Dedupe preserved verbatim.
        assert entry["last_alert_sent_at"] == old_alert


def test_migration_via_write_through_hook_also_drops_old_keys(cfg) -> None:
    """The write-through hooks (`record_outbound_receipt`, `record_inbound`)
    must also migrate the on-disk schema, not silently leave old keys in
    place. Otherwise the schema would only flip the first time the daily
    cron ran, leaving a stale on-disk shape between deploy and 08:00 PT."""
    _state_path_for(cfg).parent.mkdir(exist_ok=True)
    _state_path_for(cfg).write_text(json.dumps({
        "+15555550101": {
            "last_ping_temp_guid": "channel_hb-old",
            "last_ping_at": _hours_ago_iso(26),
            "last_round_trip_at": None,
            "last_alert_sent_at": None,
        },
    }))

    record_outbound_receipt(cfg, "+15555550101")

    state = json.loads(_state_path_for(cfg).read_text())
    entry = state["+15555550101"]
    assert "last_ping_temp_guid" not in entry
    assert "last_ping_at" not in entry
    assert "last_round_trip_at" not in entry
    assert entry["last_outbound_receipt_at"] is not None


# ---- Scheduler wiring (unchanged from 2026-06-03) --------------------------


def test_scheduler_registers_channel_heartbeat_job() -> None:
    """The daily heartbeat is wired into the scheduler. Without the entry,
    the heartbeat module would sit unused and degradation would go
    undetected."""
    import inspect
    from capabilities.realtime_kavi import scheduler as sched
    src = inspect.getsource(sched.start_scheduler)
    assert "channel-heartbeat-daily" in src, (
        "Scheduler is missing the channel-heartbeat-daily job."
    )
    assert "run_daily_channel_heartbeat" in src, (
        "Scheduler must call run_daily_channel_heartbeat from the daily "
        "cron entry."
    )
