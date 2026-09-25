"""Tests for the Kavi coordinates capability handler
(capabilities/kavi-coordinates.md, 2026-05-05).

Covers:
- One test per branch (4a / 4b / 4c / 4d) verifying the composer + tool-call
  sequence.
- Idempotency: same inbound fired twice → only one coordination session
  created.
- Bypass invariant: when a coordination-implying inbound is received, the
  conversational composer is NOT invoked. We mock compose_conversational_reply
  with side_effect=AssertionError and walk all 4 branches.
- Security guardrail integration:
    * inbound from a sender NOT in `household.md` triggers the inbound
      allowlist gate and never reaches the coordination handler.
    * outbound coordination message containing a credit-card pattern is
      blocked; logs to outbound_blocked.jsonl.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_coordination_handler.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from capabilities.coordination import handler as coordination_handler
from kavi_runtime import handlers
from kavi_runtime.runtime import outbound_scanner


# ---- shared fixtures -------------------------------------------------------


def _config_for_test(tmp_path: Path) -> dict[str, Any]:
    """Minimal config dict that satisfies every path the coordination handler
    reads. Writes go to tmp_path so each test has an isolated audit log."""
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "max_phone": "+15555550102",
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
        "action_layer": {"dry_run": False},
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "enable_prompt_caching": False},
        "paths": {
            "eval_persona_action_intent_jsonl": str(tmp_path / "action_intent.jsonl"),
            "eval_coordinates_judgments_jsonl": str(tmp_path / "coord-judgments.jsonl"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "persona-outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "persona-inbound.jsonl"),
            "outbound_blocked_jsonl": str(tmp_path / "outbound-blocked.jsonl"),
            "learned_facts": str(tmp_path / "learned_facts.jsonl"),
        },
    }


@pytest.fixture(autouse=True)
def _isolate_session_state():
    """Reset coordination_handler in-memory state between tests so a session
    leaked from one test never contaminates the next."""
    coordination_handler._reset_for_tests()
    yield
    coordination_handler._reset_for_tests()


def _make_clients(
    *,
    classify_coord: dict[str, Any] | None = None,
    ack: dict[str, Any] | None = None,
    addressee_msg: dict[str, Any] | None = None,
    parse_reply: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    create_returns: tuple[str, bool] = ("task_abc", True),
):
    """Wire MagicMock claude + graph + bb with sane defaults. Each return
    can be overridden per test."""
    claude = MagicMock()
    claude.classify_coordination_intent.return_value = classify_coord or {
        "is_coordination": True,
        "addressee_name": "Max",
        "addressee_handle": "+15555550102",
        "coordination_ask": "Does Max have cash for Rosa tomorrow morning?",
        "confidence": "high",
        "_usage": None,
    }
    claude.compose_coordination_ack.return_value = ack or {
        "text": "Got it, checking with Max now. I'll let you know what he says.",
        "char_count": 60,
        "_usage": None,
    }
    claude.compose_coordination_addressee_message.return_value = addressee_msg or {
        "text": "Hey Max, Megha mentioned cash for Rosa tomorrow morning. Do you have it on hand, or should one of you withdraw?",
        "char_count": 110,
        "attribution_applied": True,
        "_usage": None,
    }
    claude.parse_coordination_reply.return_value = parse_reply or {
        "branch": "4b_will_grab",
        "commitment_text": "Max will withdraw cash for Rosa tomorrow",
        "task_title_proposal": "Withdraw cash for Rosa",
        "deadline": "tomorrow",
        "confidence": "high",
        "reasoning": "explicit forward commitment",
        "_usage": None,
    }
    claude.compose_coordination_outcome.return_value = outcome or {
        "text": "Max said he'll grab it.",
        "char_count": 22,
        "_usage": None,
    }
    # Conversational composer should NEVER be called from a coordination flow.
    # Wire AssertionError so any leak fails the test loudly.
    claude.compose_conversational_reply.side_effect = AssertionError(
        "compose_conversational_reply was invoked from a coordination flow — bypass invariant violated"
    )

    graph = MagicMock()
    graph.create_task_in_shared_list.return_value = create_returns

    bb = MagicMock()
    return claude, graph, bb


def _stub_send(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace `_send_with_scanner` with a recorder so tests can assert on
    the sequence of outbound sends without actually hitting BlueBubbles. The
    scanner gates still run on the real `outbound_scanner` calls, so any
    blocked content surfaces in the test."""
    sends: list[dict[str, Any]] = []

    def _record(config, *, bb, text, recipient_handle, kind, chat_guid_override=None, **kwargs):
        # Run the real allowlist + content gates so the security tests catch
        # blocks correctly.
        allowed_recipient, recipient_reason = outbound_scanner.gate_outbound_recipient(
            config=config, recipient=recipient_handle, text=text,
        )
        if not allowed_recipient:
            sends.append({"text": text, "recipient": recipient_handle, "kind": kind,
                          "blocked": True, "reason": recipient_reason})
            return {"sent": False, "verified": False, "blocked": True,
                    "blocked_reason": recipient_reason, "temp_guid": None}
        allowed_content, content_reason = outbound_scanner.gate_outbound_content(
            config=config, text=text, surface="imessage",
            recipient=recipient_handle,
        )
        if not allowed_content:
            sends.append({"text": text, "recipient": recipient_handle, "kind": kind,
                          "blocked": True, "reason": content_reason})
            return {"sent": False, "verified": False, "blocked": True,
                    "blocked_reason": content_reason, "temp_guid": None}
        sends.append({"text": text, "recipient": recipient_handle, "kind": kind,
                      "blocked": False, "reason": None})
        return {"sent": True, "verified": True, "blocked": False,
                "blocked_reason": None, "temp_guid": "fake-guid"}

    monkeypatch.setattr(coordination_handler, "_send_with_scanner", _record)
    return sends


# ---- branch 4a: addressee already has it (no task created) -----------------


def test_branch_4a_yes_have_it_no_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Max replies 'yeah I have $100' → branch 4a → outcome only, no task,
    no conversational composer involvement."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients(parse_reply={
        "branch": "4a_yes_have_it",
        "commitment_text": "Max already has $100",
        "task_title_proposal": None,
        "deadline": None,
        "confidence": "high",
        "reasoning": "explicit yes-have-it",
        "_usage": None,
    }, outcome={
        "text": "Max said he has it.",
        "char_count": 19,
        "_usage": None,
    })
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="Hey Kavi, we need cash for Rosa tomorrow morning, can you check with Max if he already has it?",
        requester_handle="+15555550101",
        addressee_handle="+15555550102",
        addressee_name="Max",
        coordination_ask="Does Max have cash for Rosa tomorrow morning?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert started["status"] == "coordination_started"
    sid = started["session_id"]

    result = coordination_handler.handle_addressee_reply(
        session_id=sid, reply_text="Yeah I have $100",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert result["status"] == "coordination_closed"
    assert result["branch"] == "4a_yes_have_it"
    assert result["task_created"] is False
    assert result["task_id"] is None

    # Sequence: ack to requester, addressee message, outcome to requester. No
    # task creation call.
    kinds = [s["kind"] for s in sends]
    assert kinds == ["ack", "addressee_reach", "outcome_report"]
    graph.create_task_in_shared_list.assert_not_called()
    # Bypass invariant: conversational composer never invoked.
    claude.compose_conversational_reply.assert_not_called()


# ---- branch 4b: addressee commits → create task ----------------------------


def test_branch_4b_will_grab_creates_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Max replies 'Nope, I'll grab it tomorrow' → branch 4b → create task
    with MM prefix, then outcome."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()  # default = 4b_will_grab
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="check with Max if he has cash for Rosa tomorrow",
        requester_handle="+15555550101",
        addressee_handle="+15555550102",
        addressee_name="Max",
        coordination_ask="Does Max have cash for Rosa tomorrow morning?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]

    result = coordination_handler.handle_addressee_reply(
        session_id=sid, reply_text="Nope, I'll grab it tomorrow",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert result["status"] == "coordination_closed"
    assert result["branch"] == "4b_will_grab"
    assert result["task_created"] is True
    assert result["task_id"] == "task_abc"

    # Task creation: MM prefix because Max committed.
    graph.create_task_in_shared_list.assert_called_once()
    call_kwargs = graph.create_task_in_shared_list.call_args
    assert call_kwargs.kwargs["owner_prefix"] == "MM"
    title_passed = call_kwargs.kwargs["title"]
    assert "Withdraw cash for Rosa" in title_passed
    # Deadline phrase survived in title body per spec.
    assert "tomorrow" in title_passed.lower()

    # Sequence + bypass invariant.
    kinds = [s["kind"] for s in sends]
    assert kinds == ["ack", "addressee_reach", "outcome_report"]
    claude.compose_conversational_reply.assert_not_called()


# ---- F3 (2026-05-07): branch 4b with task-create failure -----------------


def test_branch_4b_task_create_failure_surfaces_in_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """F3 invariant: when the addressee committed (4b shape) but the
    create_task POST raised, the outcome composer must receive branch=
    `4b_will_grab_task_create_failed` (not the silent `4b_will_grab`) and
    must produce a heads-up reply naming the missing task. The default
    "Max said he'll grab it" message would silently drop a task Megha
    believes is in the system."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients(outcome={
        "text": "Max said he'll grab it. Heads up, I couldn't add a task to track it. Want me to retry?",
        "char_count": 92,
        "_usage": None,
    })
    # Make the task POST raise.
    graph.create_task_in_shared_list.side_effect = Exception("Graph 503")

    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="check with Max if he has cash for Rosa tomorrow",
        requester_handle="+15555550101",
        addressee_handle="+15555550102",
        addressee_name="Max",
        coordination_ask="Does Max have cash for Rosa tomorrow morning?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]

    result = coordination_handler.handle_addressee_reply(
        session_id=sid, reply_text="Nope, I'll grab it tomorrow",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )

    assert result["status"] == "coordination_closed"
    # Branch flipped to the failure-aware shape.
    assert result["branch"] == "4b_will_grab_task_create_failed"
    assert result["task_created"] is False
    assert result["task_id"] is None

    # Composer was called with the new branch + the failure reason.
    claude.compose_coordination_outcome.assert_called_once()
    composer_kwargs = claude.compose_coordination_outcome.call_args.kwargs
    assert composer_kwargs["branch"] == "4b_will_grab_task_create_failed"
    assert composer_kwargs["task_create_failure_reason"] == "Graph 503"
    assert composer_kwargs["task_id_if_created"] is None
    assert composer_kwargs["task_title_if_created"] is None

    # Outcome reply named the failure (NOT the silent "Max said he'll grab it").
    outcome_send = [s for s in sends if s["kind"] == "outcome_report"][0]
    msg_lower = outcome_send["text"].lower()
    assert "couldn't add" in msg_lower or "couldn't track" in msg_lower or "heads up" in msg_lower

    # Bypass invariant still holds.
    claude.compose_conversational_reply.assert_not_called()


# ---- branch 4c: ambiguous → clarify with addressee, then escalate ---------


def test_branch_4c_ambiguous_clarifies_addressee(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Max replies 'lol maybe' → branch 4c → clarifying message to Max, NOT
    an immediate escalation to Megha (per principle 3)."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients(parse_reply={
        "branch": "4c_ambiguous",
        "commitment_text": "unclear",
        "task_title_proposal": None,
        "deadline": None,
        "confidence": "high",
        "reasoning": "no commitment signal",
        "_usage": None,
    })
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="check with Max if he has cash for Rosa tomorrow",
        requester_handle="+15555550101",
        addressee_handle="+15555550102",
        addressee_name="Max",
        coordination_ask="Does Max have cash for Rosa tomorrow morning?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]

    result = coordination_handler.handle_addressee_reply(
        session_id=sid, reply_text="lol maybe",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert result["status"] == "coordination_clarifying_addressee"
    assert result["clarification_count"] == 1

    # Sequence: ack (Megha) → addressee_reach (Max) → clarify (Max). No
    # outcome report yet — the requester hasn't been re-engaged. Per
    # principle 3, escalation to requester is last resort.
    kinds = [s["kind"] for s in sends]
    assert kinds == ["ack", "addressee_reach", "clarify"]
    # Last clarify went to Max, not Megha.
    assert sends[-1]["recipient"] == "+15555550102"
    graph.create_task_in_shared_list.assert_not_called()
    claude.compose_conversational_reply.assert_not_called()


def test_branch_4c_escalates_after_two_clarifications(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """After 2 clarification iterations still ambiguous, the handler
    escalates to the requester."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients(parse_reply={
        "branch": "4c_ambiguous", "commitment_text": "unclear",
        "task_title_proposal": None, "deadline": None,
        "confidence": "high", "reasoning": "no signal", "_usage": None,
    }, outcome={
        "text": "Max replied 'lol maybe' — couldn't pin it down. Want me to follow up or you take it?",
        "char_count": 90, "_usage": None,
    })
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="check with Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="Does Max have cash?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]

    # Three ambiguous replies: clarify x2, then escalate.
    coordination_handler.handle_addressee_reply(
        session_id=sid, reply_text="lol maybe", config=cfg,
        claude=claude, graph=graph, bb=bb,
    )
    coordination_handler.handle_addressee_reply(
        session_id=sid, reply_text="we'll see", config=cfg,
        claude=claude, graph=graph, bb=bb,
    )
    final = coordination_handler.handle_addressee_reply(
        session_id=sid, reply_text="depends", config=cfg,
        claude=claude, graph=graph, bb=bb,
    )
    assert final["status"] == "coordination_closed"
    assert final["branch"] == "4c_ambiguous_escalated"

    # Last send is outcome_report to the requester (Megha).
    assert sends[-1]["kind"] == "outcome_report"
    assert sends[-1]["recipient"] == "+15555550101"
    claude.compose_conversational_reply.assert_not_called()


# ---- branch 4d: no reply → judged follow-up + escalate --------------------


def test_branch_4d_no_reply_escalates_after_two_follow_ups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """When no addressee reply lands, follow-up #1 sends a re-ping; #2
    escalates to the requester. Today the timeout policy is 30-min fixed;
    this test exercises the dispatch shape, not the wall-clock wait."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients(outcome={
        "text": "Haven't heard from Max about Rosa cash. Want me to follow up or take it from here?",
        "char_count": 90, "_usage": None,
    })
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="check with Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="Does Max have cash?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]

    # Simulate the scheduler firing the follow-up handler twice.
    first = coordination_handler._branch_4d_judge_follow_up(
        sid, cfg, claude, bb,
    )
    assert first["status"] == "coordination_follow_up_sent"
    assert first["follow_up_count"] == 1

    second = coordination_handler._branch_4d_judge_follow_up(
        sid, cfg, claude, bb,
    )
    assert second["status"] == "coordination_closed"
    assert second["branch"] == "4d_no_reply_escalated"

    # Sequence: ack, addressee_reach, follow_up (re-ping Max), outcome (escalate).
    kinds = [s["kind"] for s in sends]
    assert kinds == ["ack", "addressee_reach", "follow_up", "outcome_report"]
    assert sends[-1]["recipient"] == "+15555550101"
    claude.compose_conversational_reply.assert_not_called()


# ---- idempotency -----------------------------------------------------------


def test_idempotency_same_inbound_twice_one_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Same inbound + requester within the idempotency window → second call
    returns the existing session_id, doesn't create a new one."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    sends = _stub_send(monkeypatch)

    inbound = "check with Max if he has cash for Rosa tomorrow"
    first = coordination_handler.start_coordination(
        inbound_text=inbound, requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="Does Max have cash?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    second = coordination_handler.start_coordination(
        inbound_text=inbound, requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="Does Max have cash?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert second["status"] == "coordination_idempotency_hit"
    assert second["session_id"] == first["session_id"]
    # Only one ack + one addressee_reach were sent.
    kinds = [s["kind"] for s in sends]
    assert kinds == ["ack", "addressee_reach"]


# ---- security: inbound allowlist -------------------------------------------


def test_inbound_allowlist_discards_unknown_sender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An iMessage from a non-household sender is discarded BEFORE the
    coordination handler (and the action layer, classifier, persona) runs."""
    cfg = _config_for_test(tmp_path)

    # Stub _get_clients so handlers.imessage_received can run without the
    # real Anthropic / Graph / BB connections. We verify the gate trips
    # before the classifier is even constructed.
    claude_mock = MagicMock()
    graph_mock = MagicMock()
    bb_mock = MagicMock()
    _stub = lambda cfg: (graph_mock, claude_mock, bb_mock)
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)

    payload = {
        "type": "new-message",
        "data": {
            "isFromMe": False,
            "text": "ask Max if he has cash",
            "handle": {"address": "+19995550000"},  # NOT a household member
            "chats": [{"guid": "any;-;+19995550000"}],
        },
    }
    result = handlers.imessage_received(payload, cfg)
    assert result["status"] == "discarded_unknown_sender"

    # Coordination handler must NOT have been invoked.
    claude_mock.classify_coordination_intent.assert_not_called()
    claude_mock.classify_action_intent.assert_not_called()
    claude_mock.compose_conversational_reply.assert_not_called()


# ---- security: content scanner --------------------------------------------


def test_outbound_content_scanner_blocks_credit_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """An addressee message containing a credit-card pattern is blocked by
    the content scanner; the block is recorded in outbound_blocked.jsonl.

    Updated 2026-05-05: the coordination-side `_send_with_scanner` now
    delegates to `handlers._send_imessage_with_fallback` (the canonical
    send wrapper). To avoid constructing a real GraphClient inside the
    test (which would need full graph config), we stub `_get_clients` so
    the wrapper sees a mock bb but the recipient + content gates still
    run for real."""
    cfg = _config_for_test(tmp_path)
    # Coerce the addressee composer to return a credit-card-shaped string.
    bad_text = "Hey Max, the new card number is 4111-1111-1111-1111. Use it."
    claude, graph, bb = _make_clients(addressee_msg={
        "text": bad_text,
        "char_count": len(bad_text),
        "attribution_applied": False,
        "_usage": None,
    })
    # Stub the cached client constructors in handlers so the canonical
    # wrapper (`_send_imessage_with_fallback`) doesn't try to build a real
    # GraphClient with the (intentionally minimal) test config.
    from kavi_runtime import handlers as _handlers
    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_graph_client", None)
    monkeypatch.setattr(_clients, "_claude_client", None)
    monkeypatch.setattr(_clients, "_bb_client", None)

    bb_mock = MagicMock()
    bb_mock.send_with_verify.return_value = {
        "sent": True, "verified": True,
        "temp_guid": "fake-guid", "message_guid": None, "send_response": {},
    }
    _stub = lambda config: (MagicMock(), MagicMock(), bb_mock)
    monkeypatch.setattr(_handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)

    coordination_handler.start_coordination(
        inbound_text="ask Max about the card",
        requester_handle="+15555550101",
        addressee_handle="+15555550102",
        addressee_name="Max",
        coordination_ask="Anything we should know about the new card?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )

    # The credit-card-laden addressee_reach send must NOT have hit BlueBubbles.
    # The ack to Megha (no sensitive content) is allowed through and gets
    # one bb.send_with_verify call.
    blocked_path = Path(cfg["paths"]["outbound_blocked_jsonl"])
    assert blocked_path.exists(), "expected outbound_blocked.jsonl to be written"
    rows = [json.loads(l) for l in blocked_path.read_text().splitlines() if l]
    assert any(r["reason"].startswith("sensitive_pattern_credit_card") for r in rows), \
        f"expected a credit-card block row; got {rows}"

    # The bb mock should only have been invoked for the (clean) ack to
    # Megha; the credit-card-laden addressee message must NOT have hit
    # bb.send_with_verify.
    sent_texts = [
        c.kwargs.get("body") if c.kwargs else (c.args[0] if c.args else None)
        for c in bb_mock.send_with_verify.call_args_list
    ]
    # Defensive: support both positional and keyword variants of the bb
    # call. We assert by content because the wrapper signature is
    # `send_with_verify(text, temp_guid=...)`.
    sent_texts_str = " ".join(str(t or "") for t in sent_texts)
    assert "4111-1111-1111-1111" not in sent_texts_str, \
        "credit-card-laden message leaked to BlueBubbles"


# ---- bypass invariant: walk all 4 branches -----------------------------------


def test_bypass_invariant_walks_all_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """For each of branches 4a / 4b / 4c / 4d, verify
    compose_conversational_reply is NEVER invoked. The bypass invariant says:
    coordination-implying inbounds reply via the coordination handler, never
    via the conversational composer."""
    branches: list[dict[str, Any]] = [
        {  # 4a
            "parse_reply": {
                "branch": "4a_yes_have_it", "commitment_text": "Max already has it",
                "task_title_proposal": None, "deadline": None,
                "confidence": "high", "reasoning": "yes", "_usage": None,
            },
            "reply": "yeah I have it",
        },
        {  # 4b
            "parse_reply": {
                "branch": "4b_will_grab", "commitment_text": "Max will grab it",
                "task_title_proposal": "Grab cash", "deadline": "tomorrow",
                "confidence": "high", "reasoning": "commit", "_usage": None,
            },
            "reply": "yeah I'll grab it",
        },
        {  # 4c
            "parse_reply": {
                "branch": "4c_ambiguous", "commitment_text": "unclear",
                "task_title_proposal": None, "deadline": None,
                "confidence": "high", "reasoning": "ambiguous", "_usage": None,
            },
            "reply": "lol maybe",
        },
    ]
    for case in branches:
        cfg = _config_for_test(tmp_path)
        coordination_handler._reset_for_tests()
        claude, graph, bb = _make_clients(parse_reply=case["parse_reply"])
        _stub_send(monkeypatch)

        started = coordination_handler.start_coordination(
            inbound_text="ask Max", requester_handle="+15555550101",
            addressee_handle="+15555550102", addressee_name="Max",
            coordination_ask="ask",
            config=cfg, claude=claude, graph=graph, bb=bb,
        )
        coordination_handler.handle_addressee_reply(
            session_id=started["session_id"], reply_text=case["reply"],
            config=cfg, claude=claude, graph=graph, bb=bb,
        )
        # Bypass invariant — fail loudly via the side_effect.
        claude.compose_conversational_reply.assert_not_called()

    # 4d (timeout-driven, not reply-driven): same invariant.
    cfg = _config_for_test(tmp_path)
    coordination_handler._reset_for_tests()
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)
    started = coordination_handler.start_coordination(
        inbound_text="ask Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="ask",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    coordination_handler._branch_4d_judge_follow_up(started["session_id"], cfg, claude, bb)
    coordination_handler._branch_4d_judge_follow_up(started["session_id"], cfg, claude, bb)
    claude.compose_conversational_reply.assert_not_called()


# ---- audit log: every phase appends a row ----------------------------------


def test_audit_log_phases_per_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A successful 4b session writes ack / addressee_reach / addressee_reply /
    branch_decision / outcome_report rows, all sharing one session_id."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="ask Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="ask",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    coordination_handler.handle_addressee_reply(
        session_id=started["session_id"], reply_text="yeah I'll grab it",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )

    log_path = Path(cfg["paths"]["eval_coordinates_judgments_jsonl"])
    rows = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    phases = [r["phase"] for r in rows]
    assert phases == [
        "ack", "addressee_reach", "addressee_reply",
        "branch_decision", "outcome_report",
    ]
    sids = {r["session_id"] for r in rows}
    assert len(sids) == 1
    # Last row carries closed=True.
    assert rows[-1]["closed"] is True


# ---- requester-facing follow-up sweep (2026-06-22) -------------------------


def test_infer_followup_window_urgency() -> None:
    """Urgent asks get a short window; default asks get soft 4h / hard 24h;
    the hard window is always <= 24h."""
    soft_u, hard_u = coordination_handler._infer_followup_window(
        "can you pick up Theo today",
    )
    assert soft_u == 2 * 3600 and hard_u == 6 * 3600
    soft_d, hard_d = coordination_handler._infer_followup_window(
        "check with Max about the cleaner cash",
    )
    assert soft_d == 4 * 3600 and hard_d == 24 * 3600


def test_followup_sweep_soft_then_hard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The sweep fires the soft 'still waiting' checkpoint to the REQUESTER
    when the soft window elapses (once), then the hard 'haven't heard back —
    follow up or leave it?' checkpoint at the hard window, which closes the
    session. Neither re-narrates the addressee's to-dos."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients(outcome={
        "text": "Still waiting to hear back from Max — I'll let you know.",
        "_usage": None,
    })
    sends = _stub_send(monkeypatch)

    from kavi_runtime.runtime import clients as _clients_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", lambda c: (graph, claude, bb))
    from kavi_runtime import state as _state_mod
    monkeypatch.setattr(_state_mod, "is_quiet_hours", lambda c: False)

    started = coordination_handler.start_coordination(
        inbound_text="check with Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="Does Max have cash?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]

    # Force the soft window due now, hard far away.
    coordination_handler._sessions[sid]["soft_window_sec"] = 0
    coordination_handler._sessions[sid]["hard_window_sec"] = 10 ** 9

    r1 = coordination_handler.run_coordination_followup_sweep(cfg)
    assert r1["fired"] == 1
    assert sends[-1]["kind"] == "follow_up_status"
    assert sends[-1]["recipient"] == "+15555550101"

    # Soft fires at most once.
    r1b = coordination_handler.run_coordination_followup_sweep(cfg)
    assert r1b["fired"] == 0

    # Now force the hard window due → escalate + close.
    coordination_handler._sessions[sid]["hard_window_sec"] = 0
    r2 = coordination_handler.run_coordination_followup_sweep(cfg)
    assert r2["fired"] == 1
    assert sends[-1]["kind"] == "outcome_report"
    assert sends[-1]["recipient"] == "+15555550101"

    # Session is closed → no further sweeps fire.
    r3 = coordination_handler.run_coordination_followup_sweep(cfg)
    assert r3["fired"] == 0
    claude.compose_conversational_reply.assert_not_called()


def test_followup_sweep_skips_during_quiet_hours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    sends = _stub_send(monkeypatch)
    from kavi_runtime.runtime import clients as _clients_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", lambda c: (graph, claude, bb))
    from kavi_runtime import state as _state_mod
    monkeypatch.setattr(_state_mod, "is_quiet_hours", lambda c: True)

    started = coordination_handler.start_coordination(
        inbound_text="check with Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="Does Max have cash?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    coordination_handler._sessions[started["session_id"]]["soft_window_sec"] = 0
    before = len(sends)
    result = coordination_handler.run_coordination_followup_sweep(cfg)
    assert result["status"] == "quiet_hours_skip"
    assert len(sends) == before  # nothing sent overnight
