"""Tests for per-handle chat routing in BlueBubblesClient (Bug 1, 2026-05-07).

Background: prior to 2026-05-07, BlueBubblesClient.send_message hardcoded the
chat_guid to Megha's chat, which meant every coordination addressee_reach
intended for Max was silently delivered to Megha. The fix introduces:

  - `chat_guid_for(handle)` resolving the right chat per handle.
  - `recipient_handle` on send_message + send_with_verify, plumbed to the
    chatGuid wire payload.
  - `handlers._send_imessage_with_fallback` passes recipient_handle through to
    `bb.send_with_verify`.

These tests cover the unit + plumbing layers without hitting BlueBubbles HTTP.

Run with:
    cd kavi-runtime && uv run pytest tests/test_bluebubbles_recipient_routing.py -v
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kavi_runtime import handlers
from kavi_runtime.bluebubbles_client import BlueBubblesClient


# ---- chat_guid_for unit tests -----------------------------------------------


@pytest.fixture
def bb_client(monkeypatch: pytest.MonkeyPatch) -> BlueBubblesClient:
    """Build a BlueBubblesClient without hitting the keyring or env. The
    BLUEBUBBLES_PASSWORD env var read happens in __init__ — fake it before
    construction."""
    monkeypatch.setenv("BLUEBUBBLES_PASSWORD", "fake-password")
    config = {
        "bluebubbles": {"base_url": "http://127.0.0.1:1234"},
        "imessage": {
            "megha_phone": "+15555550101",
            "max_phone": "+15555550102",
            "chat_guid_prefix": "any;-;",
        },
    }
    return BlueBubblesClient(config)


def test_chat_guid_for_megha_uses_legacy_prefix(bb_client: BlueBubblesClient) -> None:
    """Megha's chat preserves the legacy `any;-;<phone>` form. Every existing
    send to Megha must continue routing to her chat unchanged."""
    assert bb_client.chat_guid_for("+15555550101") == "any;-;+15555550101"


def test_chat_guid_for_max_uses_imessage_address_form(bb_client: BlueBubblesClient) -> None:
    """A non-Megha handle (Max) routes to BlueBubbles' address-based DM form
    `iMessage;-;<handle>`. This is the wire fix for Bug 1: the addressee_reach
    now lands in Max's chat instead of Megha's."""
    assert bb_client.chat_guid_for("+15555550102") == "iMessage;-;+15555550102"


def test_chat_guid_for_unknown_handle_uses_imessage_form(bb_client: BlueBubblesClient) -> None:
    """A handle Kavi has never messaged before still resolves to the
    `iMessage;-;<handle>` form. The chat-seed requirement (one manual send
    from Messages.app on Kavi's Mac) is documented in the chat_guid_for
    docstring; this test verifies the routing logic, not the seed state."""
    assert bb_client.chat_guid_for("+15555550123") == "iMessage;-;+15555550123"


def test_chat_guid_for_none_falls_back_to_megha(bb_client: BlueBubblesClient) -> None:
    """None / empty handle falls back to Megha's chat — defensive default for
    any existing caller that doesn't pass a recipient."""
    assert bb_client.chat_guid_for(None) == "any;-;+15555550101"
    assert bb_client.chat_guid_for("") == "any;-;+15555550101"


def test_chat_guid_for_strips_whitespace(bb_client: BlueBubblesClient) -> None:
    """Whitespace around the handle is stripped before resolution. Defensive
    against payloads that arrive with stray spaces."""
    assert bb_client.chat_guid_for("  +15555550102  ") == "iMessage;-;+15555550102"


# ---- send_message wires recipient through to chatGuid -----------------------


def test_send_message_routes_to_addressee_chat(
    bb_client: BlueBubblesClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_message(recipient_handle=max_phone) hits the BlueBubbles wire with
    chatGuid=`iMessage;-;<max_phone>`, NOT Megha's chat. This is the bug fix
    verified at the HTTP-payload boundary."""
    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self) -> dict[str, Any]:
            return {"status": 200, "data": {"guid": "msg-guid-abc"}}

    def _fake_post(url, params=None, json=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        captured["params"] = params
        return _FakeResp()

    monkeypatch.setattr("kavi_runtime.bluebubbles_client.httpx.post", _fake_post)

    result = bb_client.send_message(
        "Hey Max, do you have cash?",
        temp_guid="addressee_reach-abc",
        recipient_handle="+15555550102",
    )
    assert result["status"] == 200
    assert captured["json"]["chatGuid"] == "iMessage;-;+15555550102"
    assert captured["json"]["tempGuid"] == "addressee_reach-abc"
    assert captured["json"]["message"] == "Hey Max, do you have cash?"


def test_send_message_with_no_recipient_routes_to_megha(
    bb_client: BlueBubblesClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy default (no recipient_handle) keeps routing to Megha's chat.
    Every existing call site that passes only (body, temp_guid) must continue
    working without behavioral change."""
    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self) -> dict[str, Any]:
            return {"status": 200, "data": {"guid": "msg-guid-def"}}

    def _fake_post(url, params=None, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        return _FakeResp()

    monkeypatch.setattr("kavi_runtime.bluebubbles_client.httpx.post", _fake_post)

    bb_client.send_message("Hi Megha", temp_guid="periodic-1")
    assert captured["json"]["chatGuid"] == "any;-;+15555550101"


# ---- send_with_verify plumbs recipient into the addressee chat --------------


def test_send_with_verify_plumbs_recipient_to_send_message(
    bb_client: BlueBubblesClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_with_verify(recipient_handle=...) forwards the handle to
    send_message, which routes the chatGuid to the addressee. We mock the
    HTTP layer + the verify poll so this stays a unit test."""
    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self) -> dict[str, Any]:
            return {"status": 200, "data": {"guid": "msg-guid-xyz"}}

    def _fake_post(url, params=None, json=None, timeout=None):  # noqa: A002
        captured["json"] = json
        return _FakeResp()

    monkeypatch.setattr("kavi_runtime.bluebubbles_client.httpx.post", _fake_post)
    # Verify-after-send is now per-message receipt (2026-06-03 rewrite):
    # the send response itself carries the GUID, no poll needed. The fake
    # POST above returns the GUID in its body, so verify will succeed
    # without further mocking.

    result = bb_client.send_with_verify(
        "Hey Max, anything?",
        temp_guid="addressee_reach-99",
        recipient_handle="+15555550102",
    )
    assert result["sent"] is True
    assert result["verified"] is True
    assert captured["json"]["chatGuid"] == "iMessage;-;+15555550102"


# ---- handlers._send_imessage_with_fallback plumbs recipient through --------


@pytest.fixture(autouse=True)
def _reset_runtime_clients(monkeypatch: pytest.MonkeyPatch):
    """Reset cached handler client singletons so each test gets a fresh
    set of mocks."""
    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_graph_client", None)
    monkeypatch.setattr(_clients, "_claude_client", None)
    monkeypatch.setattr(_clients, "_bb_client", None)
    yield


def _config_for_handler_test(tmp_path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "max_phone": "+15555550102",
            "own_email_addresses": ["megha@example.com"],
            "chat_guid_prefix": "any;-;",
        },
        "graph": {"mstodo_shared_list_id": "AQMkADAwTEST=="},
        "bluebubbles": {
            "base_url": "http://127.0.0.1:1234",
            "inbound_webhook_path": "/imessage",
        },
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "enable_prompt_caching": False},
        "paths": {
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "inbound.jsonl"),
        },
    }


def test_send_with_fallback_passes_recipient_to_bb(monkeypatch, tmp_path):
    """The canonical send wrapper plumbs `recipient_handle` to
    `bb.send_with_verify`. This is the integration point for Bug 1: until
    this wire was added, coordination addressee sends silently routed to
    Megha's chat."""
    cfg = _config_for_handler_test(tmp_path)
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": True, "verified": True, "temp_guid": "addressee_reach-aa",
        "message_guid": "msg-aa", "send_response": {},
    }
    _stub = lambda cfg: (MagicMock(), MagicMock(), bb_mock)
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)

    result = handlers._send_imessage_with_fallback(
        cfg, "Hey Max, do you have cash?", kind="coordination_addressee_reach",
        recipient_handle="+15555550102",
        provenance={"fallback_audit": "2026-06-10"},
    )
    assert result["sent"] is True
    bb_mock.send_with_verify.assert_called_once()
    call_kwargs = bb_mock.send_with_verify.call_args.kwargs
    assert call_kwargs["recipient_handle"] == "+15555550102"


def test_send_with_fallback_to_megha_passes_megha_handle(monkeypatch, tmp_path):
    """Sending to Megha (the default) still passes Megha's handle through.
    This is the no-regression assertion for the 99% path: every existing
    persona/action outbound to Megha continues to receive the right
    recipient_handle on the wire."""
    cfg = _config_for_handler_test(tmp_path)
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": True, "verified": True, "temp_guid": "ack-aa",
        "message_guid": "msg-bb", "send_response": {},
    }
    _stub = lambda cfg: (MagicMock(), MagicMock(), bb_mock)
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)

    handlers._send_imessage_with_fallback(
        cfg, "Got it.", kind="ack_short",
        provenance={"fallback_audit": "2026-06-10"},
    )
    call_kwargs = bb_mock.send_with_verify.call_args.kwargs
    # Default recipient is Megha's phone (resolved from config when the caller
    # didn't pass one explicitly).
    assert call_kwargs["recipient_handle"] == "+15555550101"
