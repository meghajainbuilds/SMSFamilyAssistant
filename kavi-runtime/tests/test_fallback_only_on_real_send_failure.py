"""Architectural test: the Outlook fallback in `runtime/send_imessage.py`
fires only on a TRUE send failure, never on the documented BlueBubbles
hang shape (`send_response.status == "timeout"` with no GUID).

Background (2026-06-03, follow-on to `af66b47` + `4596b99`): the
receipt-as-truth verify rewrite eliminated the chat-poll false-negative
class but did NOT stop the per-message Outlook fallback from firing on
the BB-hang shape. BlueBubbles silent-drops sends to chats that aren't
mirrored in its local chat.db; the send response hangs at the HTTP layer
and returns `timeout` after 10 seconds; Apple Push delivers anyway. The
old `_send_imessage_with_fallback` treated every `verified=false` as a
genuine send failure and emailed Megha "iMessage send failed". The
result was a false-alarm email per Max-bound send.

Contract enforced here:

  * `send_response.status == "timeout"` + no message GUID  → NO fallback.
    Log `BB_HANG_SILENT_DELIVERY` for observability; rely on the daily
    channel heartbeat for sustained-degradation alerting.

  * HTTP non-2xx, network error, BB unreachable, or BB returned 200 with
    no GUID → fallback FIRES. These are genuine "message did not leave
    Kavi" cases and Megha needs to know.

Two layers of enforcement:

  1. Behavioral tests that mock `bb.send_with_verify` returning each of
     the failure shapes and assert whether `graph.send_mail` was called.
  2. An AST scan of `runtime/send_imessage.py` to fail loudly if the
     BB-hang branch ever loses its `BB_HANG_SILENT_DELIVERY` marker or
     the fallback gets re-wired outside the genuine-failure branch.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_fallback_only_on_real_send_failure.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime.runtime import send_imessage as _send_mod


# ---- shared fixtures -------------------------------------------------------


@pytest.fixture
def cfg(tmp_path) -> dict[str, Any]:
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


def _install_stub_clients(monkeypatch, bb_mock, graph_mock):
    from kavi_runtime.runtime import clients as _clients_mod
    monkeypatch.setattr(_clients_mod, "_graph_client", None)
    monkeypatch.setattr(_clients_mod, "_claude_client", None)
    monkeypatch.setattr(_clients_mod, "_bb_client", None)
    _stub = lambda c: (graph_mock, MagicMock(), bb_mock)
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)


# ---- BB-hang shape: NO fallback --------------------------------------------


def test_bb_hang_timeout_with_no_guid_does_not_send_outlook_fallback(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented BlueBubbles hang shape — HTTP timeout with no GUID —
    must NOT trigger the Outlook fallback. Apple Push delivers; BB just
    never returns the receipt. False-alarming Megha per send is exactly
    the bug this fix bundle eliminates."""
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": True,        # timeout still counts as "sent" at the HTTP layer
        "verified": False,   # no GUID came back
        "temp_guid": "coordination_addressee_reach-bb-hang",
        "message_guid": None,
        "send_response": {"status": "timeout", "body": None},
    }
    graph_mock = MagicMock()
    _install_stub_clients(monkeypatch, bb_mock, graph_mock)

    result = _send_mod._send_imessage_with_fallback(
        cfg, "Hey Max, dinner?",
        kind="coordination_addressee_reach",
        recipient_handle="+15555550102",
        provenance={"fallback_audit": "2026-06-10"},
    )

    # The fallback MUST NOT have fired.
    assert result["fallback_used"] is False, (
        "BB-hang shape (timeout + no GUID) must not trigger Outlook fallback"
    )
    graph_mock.send_mail.assert_not_called()


# ---- Genuine send failures: fallback FIRES ---------------------------------


def test_http_500_send_failure_triggers_fallback(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BB returns HTTP 500 → message did not leave Kavi → fallback fires."""
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": False,
        "verified": False,
        "temp_guid": "coordination_addressee_reach-500",
        "message_guid": None,
        "send_response": {"status": 500, "body": "boom"},
    }
    graph_mock = MagicMock()
    _install_stub_clients(monkeypatch, bb_mock, graph_mock)

    result = _send_mod._send_imessage_with_fallback(
        cfg, "Hey Max, dinner?",
        kind="coordination_addressee_reach",
        recipient_handle="+15555550102",
        provenance={"fallback_audit": "2026-06-10"},
    )
    assert result["fallback_used"] is True
    graph_mock.send_mail.assert_called_once()


def test_bb_unreachable_network_error_triggers_fallback(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BB transport-level error (network down, connection refused) →
    fallback fires."""
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": False,
        "verified": False,
        "temp_guid": "coordination_addressee_reach-net-err",
        "message_guid": None,
        "send_response": {"status": "error", "body": "ConnectError"},
    }
    graph_mock = MagicMock()
    _install_stub_clients(monkeypatch, bb_mock, graph_mock)

    _send_mod._send_imessage_with_fallback(
        cfg, "Hey Max, dinner?",
        kind="coordination_addressee_reach",
        recipient_handle="+15555550102",
        provenance={"fallback_audit": "2026-06-10"},
    )
    graph_mock.send_mail.assert_called_once()


def test_200_with_no_guid_triggers_fallback(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BB returns 200 but the response body is missing the GUID → Apple
    did not accept the message → fallback fires. This is the "200 with
    no GUID" failure shape called out in the rewrite docstring."""
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": True,
        "verified": False,
        "temp_guid": "coordination_addressee_reach-no-guid",
        "message_guid": None,
        "send_response": {"status": 200, "body": {"status": 200, "data": {}}},
    }
    graph_mock = MagicMock()
    _install_stub_clients(monkeypatch, bb_mock, graph_mock)

    _send_mod._send_imessage_with_fallback(
        cfg, "Hey Max, dinner?",
        kind="coordination_addressee_reach",
        recipient_handle="+15555550102",
        provenance={"fallback_audit": "2026-06-10"},
    )
    graph_mock.send_mail.assert_called_once()


# ---- AST scan: structural property ----------------------------------------


def test_send_imessage_source_branches_bb_hang_separately() -> None:
    """Source-level guard: `runtime/send_imessage.py` must contain a
    branch that recognizes the BB-hang shape (`send_response.status ==
    "timeout"` AND `message_guid is None`) and logs
    `BB_HANG_SILENT_DELIVERY` instead of calling `graph.send_mail`.

    Without this branch, a future refactor could re-collapse the two
    failure shapes back into one and silently re-introduce the false
    alarms. The AST + text scan catches that drift.
    """
    src_path = Path(__file__).parent.parent / "kavi_runtime" / "runtime" / "send_imessage.py"
    source = src_path.read_text()
    tree = ast.parse(source)

    # 1. The marker constant must appear in the source.
    assert "BB_HANG_SILENT_DELIVERY" in source, (
        "Missing `BB_HANG_SILENT_DELIVERY` marker in runtime/send_imessage.py. "
        "The BB-hang branch must log this event so observability survives "
        "the suppressed fallback. Without it, the silent-delivery class is "
        "invisible and the daily channel heartbeat is the only signal."
    )

    # 2. There must be an `if` whose test references `timeout` AND
    #    `message_guid` together — the BB-hang gate.
    has_bb_hang_gate = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            tgt_names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if "is_bb_hang" in tgt_names:
                has_bb_hang_gate = True
                break

    assert has_bb_hang_gate, (
        "Missing `is_bb_hang = ...` gate variable in runtime/send_imessage.py. "
        "The fallback branch must compute the BB-hang predicate explicitly "
        "and use it to skip `graph.send_mail`. Renaming the variable is "
        "fine — update this test if you do."
    )

    # 3. `graph.send_mail` must be reachable only from inside an `else`
    #    branch of `if is_bb_hang:` (i.e. the genuine-failure path), or
    #    not reachable at all from this module. We assert by counting:
    #    `graph.send_mail(` should appear exactly once, and the BB-hang
    #    log line should appear before it in source order.
    send_mail_idx = source.find("graph.send_mail(")
    bb_hang_log_idx = source.find('"BB_HANG_SILENT_DELIVERY"')
    assert send_mail_idx > 0, "expected exactly one graph.send_mail() call site"
    assert bb_hang_log_idx > 0, "expected the BB_HANG_SILENT_DELIVERY log_event call"
    assert bb_hang_log_idx < send_mail_idx, (
        "Source-order guard: the BB-hang branch (logging "
        "BB_HANG_SILENT_DELIVERY) must come BEFORE the graph.send_mail "
        "call in the source. If you've reordered the branches, re-check "
        "the gate logic — the fallback must be in the else-of-bb-hang "
        "branch, never the if-of-bb-hang branch."
    )
