"""test_fetch_message_smoke_sentinel.py — verifies fetch_message skips
cleanly (returns None) when Graph answers 4xx for a smoke-test sentinel
message id, instead of raising HTTPStatusError.

Why: the post-restart E2E smoke probe (runtime_health.runtime_smoke_test)
posts a synthetic notification whose message id starts with SMOKE_TEST_.
The design docstring assumed Graph would 404 on the unknown id and the
handler would skip via P3.10 — in practice Graph answers 400 Bad Request
(malformed id, not not-found), fetch_message raised, and every single
restart left one ERROR traceback in Kavi's err log (confirmed on every
boot since at least 2026-05-13), burying real tracebacks.

Mirrors tests/test_fetch_message_404.py. The probe's 202-assertion
behavior (tests/test_runtime_smoke_test.py) is unchanged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from kavi_runtime import graph_client


def _client():
    """Minimal GraphClient that bypasses construction (avoids token + config).
    fetch_message only needs _headers to be callable."""
    g = graph_client.GraphClient.__new__(graph_client.GraphClient)
    g._headers = MagicMock(return_value={"Authorization": "Bearer x"})
    g._accounts = ["megha@example.com"]
    g._default_account = "megha@example.com"
    return g


def _resp(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"{status} error", request=MagicMock(), response=resp,
    )
    return resp


@patch("kavi_runtime.graph_client.httpx.get")
def test_smoke_sentinel_400_returns_none(mock_get):
    """Graph's actual answer for a sentinel id: 400 Bad Request → clean skip."""
    mock_get.return_value = _resp(400)
    g = _client()
    assert g.fetch_message("SMOKE_TEST_abc123def456") is None


@patch("kavi_runtime.graph_client.httpx.get")
def test_smoke_sentinel_404_still_returns_none(mock_get):
    """The originally-designed 404 path keeps working for sentinel ids."""
    resp = MagicMock()
    resp.status_code = 404
    mock_get.return_value = resp
    g = _client()
    assert g.fetch_message("SMOKE_TEST_abc123def456") is None


@patch("kavi_runtime.graph_client.httpx.get")
def test_non_sentinel_400_still_raises(mock_get):
    """A 400 on a REAL message id is a real error and must still raise —
    only the sentinel prefix earns the clean-skip treatment."""
    mock_get.return_value = _resp(400)
    g = _client()
    with pytest.raises(httpx.HTTPStatusError):
        g.fetch_message("real-message-id-400")


@patch("kavi_runtime.graph_client.httpx.get")
def test_sentinel_500_still_raises(mock_get):
    """5xx on a sentinel id is NOT the malformed-id class — a server error
    during the smoke probe should stay loud."""
    mock_get.return_value = _resp(500)
    g = _client()
    with pytest.raises(httpx.HTTPStatusError):
        g.fetch_message("SMOKE_TEST_abc123def456")


def test_probe_builds_sentinel_from_shared_constant():
    """The probe's synthetic notification id must carry the exact prefix
    fetch_message recognizes — locks the two against silent drift."""
    from capabilities.realtime_kavi import runtime_health

    payload = runtime_health._build_synthetic_notification("sub-x", "cs-y")
    sentinel = payload["value"][0]["resourceData"]["id"]
    assert sentinel.startswith(graph_client.SMOKE_TEST_MESSAGE_ID_PREFIX)
