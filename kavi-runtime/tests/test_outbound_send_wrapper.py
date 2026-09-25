"""Tests for the canonical send-iMessage wrapper
(`handlers._send_imessage_with_fallback`).

Verifies that:
- The recipient allowlist gate trips on a non-household handle.
- The content scanner gate trips on a credit-card pattern in the body.
- A blocked send writes a row to outbound_blocked.jsonl AND does NOT call
  BlueBubbles' send_with_verify.
- A clean send proceeds through bb.send_with_verify and logs the outbound.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_outbound_send_wrapper.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers


from kavi_runtime.runtime import outbound_scanner as _outbound_scanner

# Test-owned fictional household handles. Pinned here so these tests do not
# depend on the real handles the runtime allowlist is configured with.
_TEST_HOUSEHOLD_HANDLES = {
    "+15555550101", "+15555550102", "megha@example.com", "max@example.com",
}


@pytest.fixture(autouse=True)
def _test_household_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _outbound_scanner, "is_household_handle",
        lambda h: bool(h) and h.strip().lower() in _TEST_HOUSEHOLD_HANDLES,
    )


def _config_for_test(tmp_path: Path) -> dict[str, Any]:
    """Minimal config for the send wrapper test path. Writes go to tmp_path
    so each test has an isolated audit log."""
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
            "chat_guid_prefix": "any;-;",
        },
        "graph": {
            "mstodo_shared_list_id": "AQMkADAwTEST==",
        },
        "bluebubbles": {
            "base_url": "http://127.0.0.1:1234",
            "inbound_webhook_path": "/imessage",
        },
        "claude": {
            "model": "claude-sonnet-4-6", "max_tokens": 1024,
            "enable_prompt_caching": False,
        },
        "paths": {
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "inbound.jsonl"),
        },
    }


@pytest.fixture(autouse=True)
def _reset_runtime_clients(monkeypatch: pytest.MonkeyPatch):
    """Ensure each test gets a fresh client cache. handlers caches the
    GraphClient / ClaudeClient / BlueBubblesClient singletons across calls,
    which would otherwise leak mocks between tests."""
    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_graph_client", None)
    monkeypatch.setattr(_clients, "_claude_client", None)
    monkeypatch.setattr(_clients, "_bb_client", None)
    yield


def _wire_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Replace handlers._get_clients with a stub that returns mocks. Returns
    the mocks for assertions."""
    graph = MagicMock()
    claude = MagicMock()
    bb = MagicMock()
    bb.send_with_verify.return_value = {
        "sent": True, "verified": True,
        "temp_guid": "fake-guid", "message_guid": None, "send_response": {},
    }
    _stub_get_clients = lambda cfg: (graph, claude, bb)
    monkeypatch.setattr(handlers, "_get_clients", _stub_get_clients)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub_get_clients)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub_get_clients)
    return {"graph": graph, "claude": claude, "bb": bb}


# ---- recipient allowlist --------------------------------------------------


def test_send_blocked_when_recipient_not_in_household(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Sending to a non-household handle blocks the SEND, logs to
    outbound_blocked.jsonl, and does NOT invoke bb.send_with_verify."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    result = handlers._send_imessage_with_fallback(
        cfg, "Hello there", kind="test_unknown_recipient",
        recipient_handle="+19995550000",
        provenance={"fallback_audit": "2026-06-10"},
    )
    assert result["blocked"] is True
    assert result["blocked_reason"] == "unknown_recipient"
    assert result["sent"] is False
    mocks["bb"].send_with_verify.assert_not_called()

    blocked_path = Path(cfg["paths"]["outbound_blocked_jsonl"])
    assert blocked_path.exists()
    rows = [json.loads(l) for l in blocked_path.read_text().splitlines() if l.strip()]
    assert any(r["reason"] == "unknown_recipient" for r in rows)


# ---- content scanner ------------------------------------------------------


def test_send_blocked_when_text_contains_credit_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Sending text containing a credit-card pattern is blocked by the
    content scanner. The scanner runs AFTER the recipient gate (which
    accepts Megha's phone) and BEFORE the send."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    bad_text = "Heads up, the new card is 4111-1111-1111-1111 — use it for tomorrow's order."
    result = handlers._send_imessage_with_fallback(
        cfg, bad_text, kind="periodic_summary",
        provenance={"fallback_audit": "2026-06-10"},
        # default recipient = Megha's phone (allowed)
    )
    assert result["blocked"] is True
    assert result["blocked_reason"].startswith("sensitive_pattern_credit_card")
    assert result["sent"] is False
    mocks["bb"].send_with_verify.assert_not_called()

    blocked_path = Path(cfg["paths"]["outbound_blocked_jsonl"])
    rows = [json.loads(l) for l in blocked_path.read_text().splitlines() if l.strip()]
    assert any(
        r["reason"].startswith("sensitive_pattern_credit_card") for r in rows
    )


# ---- clean send proceeds --------------------------------------------------


def test_send_proceeds_when_recipient_and_content_are_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A clean message to Megha goes through bb.send_with_verify and
    returns blocked=False."""
    cfg = _config_for_test(tmp_path)
    mocks = _wire_clients(monkeypatch)

    result = handlers._send_imessage_with_fallback(
        cfg, "Got it.", kind="ack_short",
        provenance={"fallback_audit": "2026-06-10"},
    )
    assert result["blocked"] is False
    assert result["sent"] is True
    assert result["verified"] is True
    mocks["bb"].send_with_verify.assert_called_once()
