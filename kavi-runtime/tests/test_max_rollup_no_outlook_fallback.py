"""Max's rollup must NEVER fall back to Outlook (2026-06-10).

The Outlook fallback path in `runtime/send_imessage.py` resolves its
recipient to Megha's inbox regardless of the iMessage addressee (known
bug, tracked in backlog — NOT fixed here). If Max's rollup falls back, it
lands in Megha's inbox: misdelivery. The per-person split therefore sends
Max's rollup with `suppress_outlook_fallback=True` — a genuine BlueBubbles
send failure logs a warning and stays off the email channel.

Also locked here: the outbound eval rows
(eval-persona-outbound-judgments.jsonl) carry the new top-level
`recipient` field (additive; old fields unchanged).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_max_rollup_no_outlook_fallback.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.runtime import send_imessage as _send_mod
from capabilities.kavi_persona.composers import periodic_summary as _ps_module

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


MEGHA_PHONE = "+15555550101"
MAX_PHONE = "+15555550102"


@pytest.fixture
def cfg(tmp_path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": MEGHA_PHONE,
            "max_phone": MAX_PHONE,
            "own_email_addresses": ["megha@example.com"],
            "chat_guid_prefix": "any;-;",
        },
        "graph": {"mstodo_shared_list_id": "AQTEST=="},
        "paths": {
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "runs_jsonl": str(tmp_path / "runs.jsonl"),
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
            "pending_facts": str(tmp_path / "pending_facts.jsonl"),
            "outbound_blocked_jsonl": str(tmp_path / "outbound_blocked.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "inbound.jsonl"),
            "runtime_events_jsonl": str(tmp_path / "runtime-events.jsonl"),
        },
    }


_GENUINE_FAILURE = {
    "sent": False,
    "verified": False,
    "temp_guid": "periodic_summary-fail",
    "message_guid": None,
    "send_response": {"status": 500, "body": "boom"},
}


def _install_stub_clients(monkeypatch, bb_mock, graph_mock, claude_mock=None):
    from kavi_runtime.runtime import clients as _clients_mod
    _stub = lambda c: (graph_mock, claude_mock or MagicMock(), bb_mock)
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    monkeypatch.setattr(_ps_module, "_get_clients", _stub)


# ---- wrapper-level contract ---------------------------------------------------


def test_suppress_flag_skips_outlook_on_genuine_send_failure(
    cfg, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = dict(_GENUINE_FAILURE)
    graph_mock = MagicMock()
    _install_stub_clients(monkeypatch, bb_mock, graph_mock)

    with caplog.at_level("WARNING"):
        result = _send_mod._send_imessage_with_fallback(
            cfg, "Cleaner cash is due today.",
            kind="periodic_summary",
            recipient_handle=MAX_PHONE,
            suppress_outlook_fallback=True,
            provenance={"fallback_audit": "2026-06-10"},
        )

    graph_mock.send_mail.assert_not_called()
    assert result["fallback_used"] is False
    assert any("SUPPRESSED" in rec.message for rec in caplog.records), (
        "a skipped fallback must log a loud warning so the failure stays visible"
    )


def test_default_behavior_still_falls_back_for_megha(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Megha's sends keep the fallback: a genuine failure on her digest
    still reaches her by email."""
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = dict(_GENUINE_FAILURE)
    graph_mock = MagicMock()
    _install_stub_clients(monkeypatch, bb_mock, graph_mock)

    result = _send_mod._send_imessage_with_fallback(
        cfg, "Quiet day. 0 added today.",
        kind="periodic_summary",
        provenance={"fallback_audit": "2026-06-10"},
    )

    graph_mock.send_mail.assert_called_once()
    assert result["fallback_used"] is True


# ---- end-to-end: Max's rollup through the real wrapper -------------------------


def test_max_rollup_send_failure_never_reaches_outlook(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fire the real periodic_summary loop with the REAL send wrapper and
    a BlueBubbles stub that fails every send. Megha's failure falls back
    to Outlook once; Max's does not — his rollup never lands in Megha's
    inbox."""
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = dict(_GENUINE_FAILURE)
    graph_mock = MagicMock()
    today = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).strftime("%Y-%m-%dT08:00:00Z")
    graph_mock.list_open_todo_tasks.return_value = [
        {"id": "t-mj", "title": "MJ Book dentist", "createdDateTime": today},
        {"id": "t-mm", "title": "MM Leave cleaner cash", "createdDateTime": today},
    ]
    graph_mock.list_completed_todo_tasks.return_value = []
    claude_mock = MagicMock()
    claude_mock.compose_periodic_summary.return_value = "Stub digest."
    _install_stub_clients(monkeypatch, bb_mock, graph_mock, claude_mock)

    monkeypatch.setattr(
        handlers, "drain_summary_queue",
        lambda p: [
            {"task_id": "t-mj", "title": "MJ Book dentist"},
            {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
        ],
    )
    monkeypatch.setattr(
        _ps_module, "drain_summary_queue", handlers.drain_summary_queue,
    )
    monkeypatch.setattr(handlers, "list_pending_questions", lambda p: [])
    monkeypatch.setattr(_ps_module, "list_pending_questions", lambda p: [])

    handlers.periodic_summary(cfg, is_rollup=True)

    # Both sends were attempted over BlueBubbles...
    assert bb_mock.send_with_verify.call_count == 2
    # ...but only Megha's failure produced an Outlook fallback email.
    graph_mock.send_mail.assert_called_once()
    fallback_body = graph_mock.send_mail.call_args.args
    assert all("cleaner cash" not in str(part).lower() for part in fallback_body), (
        "Max's rollup content must never appear in the Outlook fallback email"
    )


# ---- eval rows carry recipient -------------------------------------------------


def test_outbound_eval_rows_stamp_recipient_field(
    cfg, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": True, "verified": True,
        "temp_guid": "periodic_summary-ok", "message_guid": "guid-1",
        "send_response": {"status": 200},
    }
    graph_mock = MagicMock()
    _install_stub_clients(monkeypatch, bb_mock, graph_mock)

    _send_mod._send_imessage_with_fallback(
        cfg, "Stub digest for Max.",
        kind="periodic_summary",
        recipient_handle=MAX_PHONE,
        suppress_outlook_fallback=True,
        provenance={"fallback_audit": "2026-06-10"},
    )
    _send_mod._send_imessage_with_fallback(
        cfg, "Stub digest for Megha.",
        kind="periodic_summary",
        provenance={"fallback_audit": "2026-06-10"},
    )

    rows = [
        json.loads(line)
        for line in Path(cfg["paths"]["eval_persona_outbound_judgments_jsonl"])
        .read_text().splitlines()
        if line.strip()
    ]
    assert [r["recipient"] for r in rows] == [MAX_PHONE, MEGHA_PHONE]
    # Additive schema: the long-standing fields are still present.
    for r in rows:
        for key in ("decision_id", "ts", "capability", "kind", "text",
                    "structural_checks", "fallback_used", "verified"):
            assert key in r
