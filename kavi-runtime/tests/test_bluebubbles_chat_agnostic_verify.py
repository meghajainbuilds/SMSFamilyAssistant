"""Tests for per-message receipt verification (rewritten 2026-06-03).

Background (history): prior to 2026-05-08, `verify_send_landed` polled
`fetch_recent_messages()` hardcoded to Megha's chat — a Max-bound send was
indexed in Max's chat, the Megha-chat poll never saw it, the 30s window
expired with `verified=False`, and every Max-bound coordination triggered
a misleading Outlook fallback to Megha. The 2026-05-08 fix added a
chat-agnostic guid-lookup path (`GET /api/v1/message/<guid>`) but kept
the chat-poll path as a backwards-compat fallback. That kept the bug
alive: any recipient whose chat had never been mirrored in BlueBubbles'
chat.db (every new household member) still tripped the polling path and
falsely reported silent-send.

The 2026-06-03 rewrite collapses the verify flow to a single signal: the
HTTP response from `POST /api/v1/message/text`. A 2xx response carrying
a message GUID means Apple accepted the message and Kavi handed it off
to the iMessage layer. No further polling. The fallback fires only on a
true send error (HTTP non-2xx, network failure, BlueBubbles unreachable,
or 200 with no GUID).

The chat-history polling path is gone from the verify flow. It remains
only for `bb_heartbeat` (chat.db keep-warm on Megha's chat) and the new
daily channel heartbeat (per-recipient round-trip health probe). The
architectural test `tests/test_no_chat_poll_verify.py` enforces this
property: no callsite of `fetch_recent_messages` or any equivalent
chat-mirror poll may appear inside the verify functions or the
Outlook-fallback decision path.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_bluebubbles_chat_agnostic_verify.py -v
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.bluebubbles_client import BlueBubblesClient, VERIFY_TIMEOUT_SEC


@pytest.fixture
def bb_client(monkeypatch: pytest.MonkeyPatch) -> BlueBubblesClient:
    """Build a BlueBubblesClient without hitting keyring or env."""
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


# ---- verify_send_landed: receipt is the ground truth ----------------------


def test_verify_returns_true_on_2xx_with_guid(bb_client: BlueBubblesClient) -> None:
    """A 2xx response with a non-empty data.guid is the verification signal.
    No polling, no chat lookup — the send response itself proves the message
    left Kavi for Apple."""
    send_response = {
        "status": 200,
        "body": {"status": 200, "data": {"guid": "msg-max-1"}},
    }
    verified, gid = bb_client.verify_send_landed(
        send_response=send_response,
        recipient_handle="+15555550102",
    )
    assert verified is True
    assert gid == "msg-max-1"


def test_verify_returns_true_for_megha_same_path(bb_client: BlueBubblesClient) -> None:
    """The verify path is recipient-agnostic. Megha goes through the exact
    same gate as Max — no chat-asymmetry possible."""
    send_response = {
        "status": 200,
        "body": {"status": 200, "data": {"guid": "msg-megha-1"}},
    }
    verified, gid = bb_client.verify_send_landed(
        send_response=send_response,
        recipient_handle="+15555550101",
    )
    assert verified is True
    assert gid == "msg-megha-1"


def test_verify_returns_false_on_non_2xx(bb_client: BlueBubblesClient) -> None:
    """HTTP 5xx from BlueBubbles is a true send failure. verified=False
    triggers the Outlook fallback so Megha doesn't lose the message."""
    send_response = {"status": 500, "body": "Internal Server Error"}
    verified, gid = bb_client.verify_send_landed(
        send_response=send_response,
        recipient_handle="+15555550102",
    )
    assert verified is False
    assert gid is None


def test_verify_returns_false_on_network_error(bb_client: BlueBubblesClient) -> None:
    """send_message converts httpx.HTTPError to {"status": "error", "body": <msg>}.
    That shape is a true send failure."""
    send_response = {"status": "error", "body": "Connection refused"}
    verified, gid = bb_client.verify_send_landed(
        send_response=send_response,
        recipient_handle="+15555550102",
    )
    assert verified is False
    assert gid is None


def test_verify_returns_false_on_timeout(bb_client: BlueBubblesClient) -> None:
    """A BlueBubbles HTTP timeout is also a true send failure for verify
    purposes. (`send_with_verify` separately marks `sent=True` on timeout to
    preserve the legacy "BB sometimes hangs while delivering" branch, but
    `verified` is the stricter signal and stays False without a GUID.)"""
    send_response = {"status": "timeout", "body": None}
    verified, gid = bb_client.verify_send_landed(
        send_response=send_response,
        recipient_handle="+15555550102",
    )
    assert verified is False
    assert gid is None


def test_verify_returns_false_on_2xx_without_guid(bb_client: BlueBubblesClient) -> None:
    """BlueBubbles returning 200 with no GUID in the body means Apple did
    not accept the message. Treat as a true send failure — the fallback
    fires so Megha doesn't lose the message."""
    send_response = {
        "status": 200,
        "body": {"status": 200, "data": {}},
    }
    verified, gid = bb_client.verify_send_landed(
        send_response=send_response,
        recipient_handle="+15555550102",
    )
    assert verified is False
    assert gid is None


def test_verify_returns_false_on_2xx_without_data_block(bb_client: BlueBubblesClient) -> None:
    """Defensive: 2xx with no `data` block at all is still a true send
    failure. We require the GUID; if BB doesn't give us one, we don't
    claim verified."""
    send_response = {
        "status": 200,
        "body": {"status": 200, "message": "ok"},
    }
    verified, gid = bb_client.verify_send_landed(
        send_response=send_response,
        recipient_handle="+15555550101",
    )
    assert verified is False
    assert gid is None


# ---- send_with_verify: end-to-end -----------------------------------------


def test_send_with_verify_returns_verified_true_for_max(
    bb_client: BlueBubblesClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a Max-bound send through the canonical wrapper returns
    verified=True when BlueBubbles returns a 200 with a GUID. No chat-poll
    is involved on the path — the receipt is the signal."""
    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self) -> dict[str, Any]:
            return {"status": 200, "data": {"guid": "msg-max-99"}}

    def _fake_post(url, params=None, json=None, timeout=None):  # noqa: A002
        captured.setdefault("posts", []).append(url)
        return _FakeResp()

    monkeypatch.setattr("kavi_runtime.bluebubbles_client.httpx.post", _fake_post)

    result = bb_client.send_with_verify(
        "Hey Max, anything?",
        temp_guid="addressee_reach-99",
        recipient_handle="+15555550102",
    )
    assert result["sent"] is True
    assert result["verified"] is True
    assert result["message_guid"] == "msg-max-99"
    # The only POST that fired was the send itself — no /api/v1/message/query
    # poll for verification.
    assert all("/api/v1/message/text" in u for u in captured["posts"]), captured["posts"]


def test_send_with_verify_no_chat_query_poll_fires(
    bb_client: BlueBubblesClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Architectural property: a successful send must NOT trigger any
    `/api/v1/message/query` poll. The chat-poll false-negative class is
    gone; this test pins the wire-level behavior."""
    posts: list[str] = []

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self) -> dict[str, Any]:
            return {"status": 200, "data": {"guid": "msg-anchor"}}

    def _fake_post(url, params=None, json=None, timeout=None):  # noqa: A002
        posts.append(url)
        return _FakeResp()

    monkeypatch.setattr("kavi_runtime.bluebubbles_client.httpx.post", _fake_post)

    bb_client.send_with_verify(
        "ping",
        temp_guid="ping-1",
        recipient_handle="+15555550102",
    )
    assert all("/api/v1/message/query" not in u for u in posts), posts


def test_send_with_verify_returns_verified_false_on_send_failure(
    bb_client: BlueBubblesClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When BlueBubbles returns 500, send_with_verify reports
    verified=False so the caller can fall back to email."""

    class _FakeResp:
        status_code = 500
        headers = {"content-type": "text/plain"}

        def json(self) -> dict[str, Any]:  # not used because content-type is not json
            return {}

    def _fake_post(url, params=None, json=None, timeout=None):  # noqa: A002
        # Mimic httpx response with .text attribute available.
        r = _FakeResp()
        r.text = "boom"  # type: ignore[attr-defined]
        return r

    monkeypatch.setattr("kavi_runtime.bluebubbles_client.httpx.post", _fake_post)

    result = bb_client.send_with_verify(
        "ping",
        temp_guid="ping-2",
        recipient_handle="+15555550102",
    )
    assert result["sent"] is False
    assert result["verified"] is False
    assert result["message_guid"] is None


# ---- Production-shape: no Outlook fallback fires when verified=True --------


def test_send_to_max_with_guid_logs_verified_true_and_no_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Production-shape regression test for the canonical wrapper.

    Sends to Max via `_send_imessage_with_fallback`. BlueBubbles returns a
    GUID in the send response → verified=True → no Outlook fallback fires.
    Even if a subsequent `fetch_recent_messages` would return empty (which
    it always does for Max — chat.db has never mirrored his chat), the
    eval log records verified=true, fallback_used=false."""
    cfg: dict[str, Any] = {
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

    # Reset cached client singletons.
    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_graph_client", None)
    monkeypatch.setattr(_clients, "_claude_client", None)
    monkeypatch.setattr(_clients, "_bb_client", None)

    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": True,
        "verified": True,
        "temp_guid": "coordination_addressee_reach-99",
        "message_guid": "msg-max-99",
        "send_response": {"status": 200, "body": {"status": 200, "data": {"guid": "msg-max-99"}}},
    }
    # Simulate the production reality on Kavi: BB's chat.db has never
    # mirrored Max's chat, so a poll would always return empty. The new
    # verify flow MUST NOT consult this method, so the empty mock is here
    # to prove the test would have failed under the old chat-poll logic.
    bb_mock.fetch_recent_messages.return_value = []

    graph_mock = MagicMock()
    _stub = lambda cfg: (graph_mock, MagicMock(), bb_mock)
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)

    result = handlers._send_imessage_with_fallback(
        cfg, "Hey Max, can you pick up groceries on your way home?",
        kind="coordination_addressee_reach",
        recipient_handle="+15555550102",
        provenance={"fallback_audit": "2026-06-10"},
    )

    # Outcome: verified, no fallback, no Outlook email sent to Megha.
    assert result["sent"] is True
    assert result["verified"] is True
    assert result["fallback_used"] is False
    graph_mock.send_mail.assert_not_called()
    # And critically: the verify flow never touched fetch_recent_messages.
    bb_mock.fetch_recent_messages.assert_not_called()

    # Eval log row reflects the truth: verified=true, fallback_used=false.
    out_path = tmp_path / "outbound.jsonl"
    assert out_path.exists(), "outbound eval log should have been written"
    import json
    rows = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    verified_in_row = (
        row.get("verified") is True
        or row.get("send_result", {}).get("verified") is True
    )
    assert verified_in_row, f"expected verified=true in eval row, got: {row}"


def test_send_to_max_with_send_failure_falls_back_to_outlook(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """When the send call itself fails (BB returns 500), the Outlook
    fallback fires. This is the ONLY shape that triggers the fallback in
    the rewritten flow."""
    cfg: dict[str, Any] = {
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

    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_graph_client", None)
    monkeypatch.setattr(_clients, "_claude_client", None)
    monkeypatch.setattr(_clients, "_bb_client", None)

    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": False,
        "verified": False,
        "temp_guid": "coordination_addressee_reach-100",
        "message_guid": None,
        "send_response": {"status": 500, "body": "boom"},
    }
    graph_mock = MagicMock()
    _stub = lambda cfg: (graph_mock, MagicMock(), bb_mock)
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)

    result = handlers._send_imessage_with_fallback(
        cfg, "Hey Max, status?",
        kind="coordination_addressee_reach",
        recipient_handle="+15555550102",
        provenance={"fallback_audit": "2026-06-10"},
    )
    assert result["verified"] is False
    assert result["fallback_used"] is True
    graph_mock.send_mail.assert_called_once()


# ---- Sanity: the timeout constant is exported for legacy callers ----------


def test_verify_timeout_constant_is_exported() -> None:
    """`VERIFY_TIMEOUT_SEC` is retained as a module-level constant for
    backwards compatibility with the SILENT_SEND_DETECTED-style log lines.
    Verification no longer polls, so the constant is vestigial — but the
    import surface must stay stable."""
    assert isinstance(VERIFY_TIMEOUT_SEC, int)
    assert VERIFY_TIMEOUT_SEC >= 30
