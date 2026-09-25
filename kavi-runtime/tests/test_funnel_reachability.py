"""test_funnel_reachability.py — verifies the Funnel reachability probe
catches today's 2026-05-06 PM root cause class (Tailscale Funnel forward
target drifts from runtime bind address; Microsoft gets 502 BadGateway).

The probe must:
  - Treat 200 and 503 (degraded but reachable) as healthy — both prove
    the Funnel → loopback → runtime path is intact.
  - Treat 502, timeouts, and network errors as unreachable.
  - Require N consecutive failures before flipping to degraded (one
    transient relay blip is not durable enough to alarm on).
  - Restore cleanly to reachable on the next successful probe.
  - Skip cleanly when public_url is missing from config.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from capabilities.realtime_kavi import runtime_health


CONFIG = {"server": {"public_url": "https://kavis-test.example.com"}}


@pytest.fixture(autouse=True)
def _reset():
    runtime_health.reset_funnel_state_for_test()
    yield
    runtime_health.reset_funnel_state_for_test()


@patch("capabilities.realtime_kavi.runtime_health.httpx.get")
def test_200_keeps_reachable(mock_get):
    resp = MagicMock(); resp.status_code = 200
    mock_get.return_value = resp
    result = runtime_health.funnel_reachability_check(CONFIG)
    assert result["reachable"] is True
    assert runtime_health.is_funnel_reachable() is True


@patch("capabilities.realtime_kavi.runtime_health.httpx.get")
def test_503_counts_as_reachable(mock_get):
    """503 means the runtime is alive and answering through Funnel — the
    invariants might be degraded, but the Funnel link itself works."""
    resp = MagicMock(); resp.status_code = 503
    mock_get.return_value = resp
    result = runtime_health.funnel_reachability_check(CONFIG)
    assert result["reachable"] is True
    assert runtime_health.is_funnel_reachable() is True


@patch("capabilities.realtime_kavi.runtime_health.httpx.get")
def test_one_502_does_not_flip_to_degraded(mock_get):
    """Single transient failure stays optimistic. Threshold is 2."""
    resp = MagicMock(); resp.status_code = 502
    mock_get.return_value = resp
    result = runtime_health.funnel_reachability_check(CONFIG)
    assert result["reachable"] is False
    # Module flag still True because we haven't crossed the threshold.
    assert runtime_health.is_funnel_reachable() is True


@patch("capabilities.realtime_kavi.runtime_health.httpx.get")
def test_two_consecutive_502s_flip_to_degraded(mock_get):
    resp = MagicMock(); resp.status_code = 502
    mock_get.return_value = resp
    runtime_health.funnel_reachability_check(CONFIG)
    runtime_health.funnel_reachability_check(CONFIG)
    assert runtime_health.is_funnel_reachable() is False


@patch("capabilities.realtime_kavi.runtime_health.httpx.get")
def test_timeout_counts_as_failure(mock_get):
    mock_get.side_effect = httpx.TimeoutException("timeout")
    runtime_health.funnel_reachability_check(CONFIG)
    runtime_health.funnel_reachability_check(CONFIG)
    assert runtime_health.is_funnel_reachable() is False


@patch("capabilities.realtime_kavi.runtime_health.httpx.get")
def test_recovery_resets_failure_counter(mock_get):
    """One failure followed by success keeps the runtime healthy and the
    counter reset (next single failure won't immediately flip)."""
    resp_fail = MagicMock(); resp_fail.status_code = 502
    resp_ok = MagicMock(); resp_ok.status_code = 200
    mock_get.side_effect = [resp_fail, resp_ok, resp_fail]
    runtime_health.funnel_reachability_check(CONFIG)
    runtime_health.funnel_reachability_check(CONFIG)
    runtime_health.funnel_reachability_check(CONFIG)  # one fail, NOT two
    assert runtime_health.is_funnel_reachable() is True


@patch("capabilities.realtime_kavi.runtime_health.httpx.get")
def test_recovery_after_degraded_state(mock_get):
    """Two failures flip to degraded; one success restores."""
    resp_fail = MagicMock(); resp_fail.status_code = 502
    resp_ok = MagicMock(); resp_ok.status_code = 200
    mock_get.side_effect = [resp_fail, resp_fail, resp_ok]
    runtime_health.funnel_reachability_check(CONFIG)
    runtime_health.funnel_reachability_check(CONFIG)
    assert runtime_health.is_funnel_reachable() is False
    runtime_health.funnel_reachability_check(CONFIG)
    assert runtime_health.is_funnel_reachable() is True


@patch("capabilities.realtime_kavi.runtime_health.httpx.get")
def test_skipped_when_public_url_unconfigured(mock_get):
    result = runtime_health.funnel_reachability_check({"server": {}})
    assert result["skipped_reason"] == "no_public_url_configured"
    assert mock_get.called is False


def test_default_state_is_reachable():
    """Optimistic default — first check verifies. /health doesn't return
    503 just because the runtime hasn't completed its first probe yet."""
    assert runtime_health.is_funnel_reachable() is True
