"""Tests for staging_mode (capability-build pipeline, 2026-06-10).

A staging instance (config.staging_mode true — see config-staging.yaml)
is the LLM-replay sandbox on port 8081. Four gates, each tested here for
flag-on AND flag-off behavior:

1. SEND: kavi_runtime/runtime/send_imessage.py hard-blocks every outbound
   as its FIRST check — no client construction, no BlueBubbles, no
   Outlook fallback.
2. SCHEDULER: capabilities/realtime_kavi/scheduler.py registers NO jobs.
3. WEBHOOKS: /graph/notifications and the BlueBubbles inbound route
   return 503 "staging".
4. SYNTHETIC + HEALTH: /synthetic/* routes, /health and /status keep
   working (matrix replays need the real composer path).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from capabilities.realtime_kavi import scheduler as sched_module
from capabilities.realtime_kavi import server as server_module
from capabilities.realtime_kavi.runtime_health import reset_funnel_state_for_test
from capabilities.realtime_kavi.server import build_app
from kavi_runtime import runtime_status
from kavi_runtime.runtime import send_imessage as send_module


# ---- helpers ----------------------------------------------------------------

def _server_config(tmp_path: Path, *, staging: bool) -> dict:
    config: dict[str, Any] = {
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
    if staging:
        config["staging_mode"] = True
    return config


class _FakeScheduler:
    """Records add_job calls without spinning real APScheduler threads."""

    def __init__(self, timezone=None):
        self.timezone = timezone
        self.job_ids: list[str] = []
        self.started = False

    def add_listener(self, *args, **kwargs) -> None:
        pass

    def add_job(self, *args, **kwargs) -> None:
        self.job_ids.append(kwargs.get("id", "?"))

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = True) -> None:
        self.started = False


def _scheduler_config() -> dict:
    return {
        "schedule": {
            "periodic_summary_times": ["07:00"],
            "end_of_day_rollup": "21:00",
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "07:00",
            "correction_pattern_check_every_hours": 6,
            "weekly_self_check_send_time": "14:00",
            "weekly_self_check_timeout_hours": 24,
        },
        "graph": {"subscription_renewal_buffer_minutes": 60},
        "server": {"public_url": None},
        "healthcheck": {"url": None},
    }


# ---- Gate 1: outbound send hard block ----------------------------------------

def test_staging_blocks_send_before_any_client_touch(monkeypatch) -> None:
    """staging_mode=true: the send wrapper returns blocked WITHOUT ever
    constructing clients (the staging check is first)."""
    def _explode(config):
        raise AssertionError("staging send must never construct clients")

    monkeypatch.setattr(send_module, "_get_clients", _explode)
    result = send_module._send_imessage_with_fallback(
        {"staging_mode": True}, "hello from staging", kind="test_kind",
    )
    assert result["blocked"] is True
    assert result["blocked_reason"] == "staging_outbound_disabled"
    assert result["sent"] is False
    assert result["verified"] is False
    assert result["fallback_used"] is False


def test_staging_blocks_raw_send_too(monkeypatch) -> None:
    """send_imessage_raw routes through the canonical wrapper, so the
    staging block covers the degraded-mode hand-coded sender as well."""
    monkeypatch.setattr(
        send_module, "_get_clients",
        lambda config: (_ for _ in ()).throw(AssertionError("never")),
    )
    result = send_module.send_imessage_raw(
        {"staging_mode": True}, "alert text", recipient_handle="+15555550101",
    )
    assert result["blocked"] is True
    assert result["blocked_reason"] == "staging_outbound_disabled"


def test_non_staging_send_proceeds_past_staging_gate(monkeypatch) -> None:
    """Flag off (absent): the wrapper reaches the client-construction step
    — proven by a sentinel raised from _get_clients."""
    class _Sentinel(Exception):
        pass

    def _raise(config):
        raise _Sentinel()

    monkeypatch.setattr(send_module, "_get_clients", _raise)
    with pytest.raises(_Sentinel):
        send_module._send_imessage_with_fallback(
            {"imessage": {"megha_phone": "+15555550101"}}, "hi", kind="test_kind",
            provenance={"fallback_audit": "2026-06-10"},
        )


# ---- Gate 2: scheduler registers no jobs --------------------------------------

def test_staging_scheduler_registers_no_jobs(monkeypatch) -> None:
    monkeypatch.setattr(sched_module, "BackgroundScheduler", _FakeScheduler)
    config = _scheduler_config()
    config["staging_mode"] = True
    scheduler = sched_module.start_scheduler(config)
    assert scheduler.started is True  # main.py shutdown wiring unchanged
    assert scheduler.job_ids == []


def test_non_staging_scheduler_registers_jobs(monkeypatch) -> None:
    monkeypatch.setattr(sched_module, "BackgroundScheduler", _FakeScheduler)
    scheduler = sched_module.start_scheduler(_scheduler_config())
    assert scheduler.started is True
    # The production job set is registered (spot-check the cron spine).
    assert "summary-07:00" in scheduler.job_ids
    assert "rollup" in scheduler.job_ids
    assert "graph-subscription-renewal" in scheduler.job_ids


# ---- Gate 3: webhooks 503 ------------------------------------------------------

def test_staging_graph_webhook_returns_503(tmp_path: Path) -> None:
    client = TestClient(build_app(_server_config(tmp_path, staging=True)))
    resp = client.post("/graph/notifications", json={"value": []})
    assert resp.status_code == 503
    assert resp.json()["error"] == "staging"


def test_staging_graph_validation_token_also_refused(tmp_path: Path) -> None:
    """Even the subscription validation echo is refused — staging must
    never bind a Graph subscription to itself."""
    client = TestClient(build_app(_server_config(tmp_path, staging=True)))
    resp = client.post("/graph/notifications?validationToken=tok123")
    assert resp.status_code == 503


def test_staging_bluebubbles_webhook_returns_503(tmp_path: Path) -> None:
    client = TestClient(build_app(_server_config(tmp_path, staging=True)))
    resp = client.post("/bluebubbles/inbound", json={"type": "new-message"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "staging"


def test_non_staging_webhooks_not_503(tmp_path: Path, monkeypatch) -> None:
    """Flag off: the graph webhook accepts (202) as before."""
    monkeypatch.setattr(server_module, "_get_clients",
                        lambda config: (type("G", (), {"accounts": lambda self: []})(), None, None))
    client = TestClient(build_app(_server_config(tmp_path, staging=False)))
    resp = client.post("/graph/notifications", json={"value": []})
    assert resp.status_code == 202


# ---- Gate 4: synthetic + health + status still work ----------------------------

def test_staging_synthetic_verify_route_still_works(tmp_path: Path, monkeypatch) -> None:
    from kavi_runtime import synthetic_compose

    def _fake_verify(config, body):
        return {"output": "ok", "verdict": "PASS", "failures": [],
                "model": "test", "input_payload": body}

    monkeypatch.setattr(
        synthetic_compose, "verify_periodic_summary_selection", _fake_verify)
    client = TestClient(build_app(_server_config(tmp_path, staging=True)))
    resp = client.post("/synthetic/verify/kavi-persona",
                       json={"time_of_day": "morning", "is_rollup": False})
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "PASS"


def test_staging_synthetic_compose_route_still_works(tmp_path: Path, monkeypatch) -> None:
    from kavi_runtime import synthetic_compose

    def _fake_replay(config, body):
        return {"output": "composed", "model": "test", "input_payload": body}

    monkeypatch.setattr(
        synthetic_compose, "replay_periodic_summary", _fake_replay)
    client = TestClient(build_app(_server_config(tmp_path, staging=True)))
    resp = client.post("/synthetic/compose/kavi-persona",
                       json={"time_of_day": "morning"})
    assert resp.status_code == 200
    assert resp.json()["output"] == "composed"


def test_staging_health_returns_200_and_skips_invariants(tmp_path: Path, monkeypatch) -> None:
    """enforce_invariants=True (the production main.py path) must not run
    the production state-invariant probe on staging, and /health is 200."""
    reset_funnel_state_for_test()
    called = []
    monkeypatch.setattr(server_module, "discover_household_accounts",
                        lambda config: called.append(True) or [])
    client = TestClient(
        build_app(_server_config(tmp_path, staging=True), enforce_invariants=True))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert called == []  # invariant probe never ran in staging


def test_staging_status_page_still_works(tmp_path: Path) -> None:
    runtime_status.reset_for_test()
    client = TestClient(build_app(_server_config(tmp_path, staging=True)))
    resp = client.get("/status")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
