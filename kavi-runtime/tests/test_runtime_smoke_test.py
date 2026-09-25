"""test_runtime_smoke_test.py — verifies the post-restart E2E smoke test
(P1.4) catches inbound-chain breaks end-to-end.

The probe builds a synthetic Graph notification using the real
subscription_id (so the runtime's index routes it) and a sentinel
message_id (so fetch_message 404s, handler skips cleanly via P3.10).
POST through the public URL exercises Funnel → loopback → webhook
router. 202 Accepted = chain works.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from capabilities.realtime_kavi import runtime_health


CONFIG_TMPL = {
    "server": {"public_url": "https://kavis-test.example.com"},
}


def _config_with_state(tmp_path: Path) -> dict:
    """Build a config + populate per-account subscription state at the
    canonical path so runtime_smoke_test finds it."""
    sub_dir = tmp_path / "subscriptions"
    sub_dir.mkdir()
    sub_path = sub_dir / "megha@example.com.json"
    sub_path.write_text(json.dumps({
        "subscription_id": "sub-abc-123",
        "client_state": "secret-token",
        "expiration_dt": "2026-05-08T23:24:00Z",
    }))
    return {**CONFIG_TMPL, "_test_sub_dir": sub_dir, "_test_sub_path": sub_path}


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    from kavi_runtime import graph_client
    sub_dir = tmp_path / "subscriptions"
    sub_dir.mkdir()
    monkeypatch.setattr(graph_client, "SUBSCRIPTION_STATE_DIR", sub_dir)
    monkeypatch.setattr(graph_client, "discover_household_accounts",
                        lambda config: ["megha@example.com"])
    sub_path = sub_dir / "megha@example.com.json"
    sub_path.write_text(json.dumps({
        "subscription_id": "sub-abc-123",
        "client_state": "secret-token",
        "expiration_dt": "2026-05-08T23:24:00Z",
    }))
    return tmp_path


@patch("capabilities.realtime_kavi.runtime_health.httpx.post")
def test_smoke_test_passes_on_202(mock_post, isolated_state):
    resp = MagicMock(); resp.status_code = 202
    mock_post.return_value = resp
    result = runtime_health.runtime_smoke_test(CONFIG_TMPL)
    assert result["status"] == 202
    # Verify the POST went to /graph/notifications with a payload that has
    # the right shape (real sub_id, sentinel message_id).
    args, kwargs = mock_post.call_args
    assert "/graph/notifications" in args[0]
    payload = kwargs["json"]
    assert payload["value"][0]["subscriptionId"] == "sub-abc-123"
    assert payload["value"][0]["clientState"] == "secret-token"
    assert payload["value"][0]["resourceData"]["id"].startswith("SMOKE_TEST_")


@patch("capabilities.realtime_kavi.runtime_health.httpx.post")
def test_smoke_test_fails_on_502(mock_post, isolated_state, caplog):
    """Non-202 status logs CRITICAL — proves the inbound chain is broken."""
    resp = MagicMock(); resp.status_code = 502
    mock_post.return_value = resp
    with caplog.at_level("CRITICAL"):
        result = runtime_health.runtime_smoke_test(CONFIG_TMPL)
    assert result["status"] == 502
    assert any("FAIL" in r.message and "502" in r.message
               for r in caplog.records)


@patch("capabilities.realtime_kavi.runtime_health.httpx.post")
def test_smoke_test_fails_on_timeout(mock_post, isolated_state, caplog):
    mock_post.side_effect = httpx.TimeoutException("timeout")
    with caplog.at_level("CRITICAL"):
        result = runtime_health.runtime_smoke_test(CONFIG_TMPL)
    assert result["status"] == "timeout"
    assert any("TIMEOUT" in r.message for r in caplog.records)


def test_smoke_test_skipped_when_public_url_missing(isolated_state):
    result = runtime_health.runtime_smoke_test({"server": {}})
    assert result["skipped_reason"] == "no_public_url"


def test_smoke_test_skipped_when_subscription_state_missing(tmp_path, monkeypatch):
    from kavi_runtime import graph_client
    empty_sub_dir = tmp_path / "subscriptions"
    empty_sub_dir.mkdir()
    monkeypatch.setattr(graph_client, "SUBSCRIPTION_STATE_DIR", empty_sub_dir)
    monkeypatch.setattr(graph_client, "discover_household_accounts",
                        lambda config: ["megha@example.com"])
    result = runtime_health.runtime_smoke_test(CONFIG_TMPL)
    assert result["skipped_reason"] == "no_subscription_state"


def test_smoke_test_skipped_when_subscription_state_incomplete(tmp_path, monkeypatch):
    from kavi_runtime import graph_client
    sub_dir = tmp_path / "subscriptions"
    sub_dir.mkdir()
    monkeypatch.setattr(graph_client, "SUBSCRIPTION_STATE_DIR", sub_dir)
    monkeypatch.setattr(graph_client, "discover_household_accounts",
                        lambda config: ["megha@example.com"])
    sub_path = sub_dir / "megha@example.com.json"
    sub_path.write_text(json.dumps({"subscription_id": "x"}))  # missing client_state
    result = runtime_health.runtime_smoke_test(CONFIG_TMPL)
    assert result["skipped_reason"] == "subscription_state_incomplete"


def test_synthetic_notification_shape():
    """The notification payload must be parseable as a Graph webhook by the
    runtime's existing /graph/notifications handler."""
    n = runtime_health._build_synthetic_notification("sub-1", "client-state")
    assert "value" in n
    assert n["value"][0]["subscriptionId"] == "sub-1"
    assert n["value"][0]["clientState"] == "client-state"
    assert n["value"][0]["changeType"] == "created"
    assert n["value"][0]["resourceData"]["id"].startswith("SMOKE_TEST_")
