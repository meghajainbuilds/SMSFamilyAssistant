"""test_fetch_message_404.py — verifies fetch_message returns None on
Graph 404 instead of raising HTTPStatusError.

Why: 2026-05-06 PM. After the runtime recovered from a multi-hour silence,
Microsoft replayed buffered notifications; each referenced an email that
no longer existed in the inbox. fetch_message raised on every 404, the
email_arrived handler counted each as a failure, and the rolling
failure-rate alarm fired a false-alarm 100% rate alert. The fix:
fetch_message returns None on 404; caller treats None as 'skip cleanly'.
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


@patch("kavi_runtime.graph_client.httpx.get")
def test_fetch_message_returns_none_on_404(mock_get):
    """A 404 from Graph must surface as None, not raise."""
    resp = MagicMock()
    resp.status_code = 404
    mock_get.return_value = resp
    g = _client()
    assert g.fetch_message("stale-msg-id") is None


@patch("kavi_runtime.graph_client.httpx.get")
def test_fetch_message_returns_dict_on_200(mock_get):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"id": "real-msg", "subject": "hi"}
    mock_get.return_value = resp
    g = _client()
    assert g.fetch_message("real-msg") == {"id": "real-msg", "subject": "hi"}


@patch("kavi_runtime.graph_client.httpx.get")
def test_fetch_message_raises_on_500(mock_get):
    """Non-404 errors still raise — 404 is the only stale-notification class."""
    resp = MagicMock()
    resp.status_code = 500
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "server error", request=MagicMock(), response=resp,
    )
    mock_get.return_value = resp
    g = _client()
    with pytest.raises(httpx.HTTPStatusError):
        g.fetch_message("real-msg")


@patch("kavi_runtime.graph_client.httpx.get")
def test_fetch_message_raises_on_403(mock_get):
    """Auth errors still raise — they are real failures the handler must see."""
    resp = MagicMock()
    resp.status_code = 403
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "forbidden", request=MagicMock(), response=resp,
    )
    mock_get.return_value = resp
    g = _client()
    with pytest.raises(httpx.HTTPStatusError):
        g.fetch_message("real-msg")
