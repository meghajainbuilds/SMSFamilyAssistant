"""End-to-end intent-first dispatch (imessage_received) with stub clients.

The 2026-06-10 rebuild: after the non-semantic gates, EVERY inbound goes
through parse -> execute (ALL intents) -> ONE composed reply. These tests
lock the pipeline shape: multi-intent all-execute with a single final
send, provenance tagging, G-A1 grounding, the safe-sentence cold fallback,
and the death of the "Got it." short-text branch.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_intent_dispatch.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.structural_checks import (
    ACTION_VERB_PATTERN,
    LENGTH_CAP_TARGET,
)

MEGHA = "+15555550101"


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": MEGHA,
            "own_email_addresses": ["megha@example.com"],
            "chat_guid_prefix": "any;-;",
        },
        "graph": {"mstodo_shared_list_id": "LIST"},
        "claude": {"model": "claude-sonnet-4-6", "max_tokens": 1024,
                   "enable_prompt_caching": False},
        "paths": {
            "imessage_state": str(tmp_path / "state" / "imessage-state.json"),
            "eval_persona_outbound_judgments_jsonl": str(tmp_path / "persona-outbound.jsonl"),
            "eval_persona_inbound_jsonl": str(tmp_path / "persona-inbound.jsonl"),
            "outbound_blocked_jsonl": str(tmp_path / "outbound-blocked.jsonl"),
            "pending_facts": str(tmp_path / "pending_facts.jsonl"),
            "runtime_events_jsonl": str(tmp_path / "runtime-events.jsonl"),
        },
    }


def _payload(text: str) -> dict[str, Any]:
    return {
        "type": "new-message",
        "data": {
            "isFromMe": False,
            "text": text,
            "guid": "msg-guid-1",
            "handle": {"address": MEGHA},
            "chats": [{"guid": f"any;-;{MEGHA}"}],
        },
    }


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def _send(config, text, *, kind, provenance=None, **kwargs):
        out.append({"text": text, "kind": kind, "provenance": provenance})
        return {"sent": True, "verified": True, "fallback_used": False,
                "blocked": False, "blocked_reason": None, "temp_guid": "g"}

    def _send_ctx(config, text, *, kind, context, provenance=None, **kwargs):
        out.append({"text": text, "kind": kind, "provenance": provenance,
                    "context": context})
        return {"sent": True, "verified": True, "fallback_used": False,
                "blocked": False, "blocked_reason": None, "temp_guid": "g"}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send)
    monkeypatch.setattr(handlers, "_send_imessage_with_fallback_and_context", _send_ctx)
    return out


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch):
    """Stub everything outside the pipeline under test."""
    claude = MagicMock()
    graph = MagicMock()
    graph.list_open_todo_tasks.return_value = [
        {"id": "t-boonli", "title": "MJ Pay Boonli invoice"},
        {"id": "t-maple", "title": "MJ Complete Maple Street camp forms"},
    ]
    graph.mark_task_done.return_value = (True, "title")
    _stub = lambda cfg: (graph, claude, MagicMock())
    monkeypatch.setattr(handlers, "_get_clients", _stub)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub)

    from kavi_runtime.runtime import outbound_scanner
    monkeypatch.setattr(outbound_scanner, "gate_inbound_sender",
                        lambda **kw: (True, None))
    from capabilities.coordination import handler as ch
    monkeypatch.setattr(ch, "lookup_session_by_addressee_handle",
                        lambda h, config=None: None)
    monkeypatch.setattr(ch, "lookup_session_by_requester_handle",
                        lambda h, config=None: None)
    monkeypatch.setattr(ch, "list_open_sessions", lambda config=None: [])
    monkeypatch.setattr(handlers, "list_pending_questions", lambda p: [])
    from kavi_runtime.runtime import weekly_self_check as wsc
    monkeypatch.setattr(wsc, "try_handle_self_check_reply", lambda text, config: None)
    return {"claude": claude, "graph": graph}


def test_multi_intent_all_execute_single_reply(tmp_path, sent, stubs) -> None:
    """Seven close targets in one message: every target executes, ONE
    reply ships, tagged with the composer's provenance and grounded by the
    real execution context."""
    cfg = _config(tmp_path)
    claude, graph = stubs["claude"], stubs["graph"]
    claude.parse_reply_intents.return_value = [{
        "type": "close_task", "target_text": "mark these done",
        "targets": [{"id": f"t{i}", "title": f"MJ Task {i}"} for i in range(7)],
        "confidence": "high",
    }]
    claude.compose_kavi_reply.return_value = "Marked all seven done."

    result = handlers.imessage_received(_payload("mark these done: ..."), cfg)

    assert result["status"] == "intent_dispatch"
    assert graph.mark_task_done.call_count == 7
    assert len(sent) == 1
    assert sent[0]["text"] == "Marked all seven done."
    assert sent[0]["kind"] == "post_action_reply"
    assert sent[0]["provenance"] == {"llm_call": "compose_kavi_reply"}
    # G-A1 grounding context carries the real execution rows.
    assert sent[0]["context"]["actions_executed"]


def test_conversational_no_got_it(tmp_path, sent, stubs) -> None:
    """A short chat message gets a real LLM reply — the 'Got it.' short-text
    branch is dead (matrix case conversational-nothing-pending-no-got-it)."""
    cfg = _config(tmp_path)
    claude = stubs["claude"]
    claude.parse_reply_intents.return_value = [{
        "type": "conversational", "target_text": "hey", "targets": [],
        "confidence": "high",
    }]
    claude.compose_kavi_reply.return_value = "Hey. Anything you need from me?"

    result = handlers.imessage_received(_payload("hey"), cfg)

    assert result["status"] == "intent_dispatch"
    assert sent[-1]["text"] == "Hey. Anything you need from me?"
    assert sent[-1]["kind"] == "conversational"
    assert "Got it." not in [s["text"] for s in sent]


def test_parser_failure_ships_safe_sentence(tmp_path, sent, stubs) -> None:
    """Cold-fallback policy: parser None after its retry → ONE audited safe
    sentence, no action claims, within the target cap, never silence."""
    cfg = _config(tmp_path)
    stubs["claude"].parse_reply_intents.return_value = None

    result = handlers.imessage_received(_payload("close the anita stuff"), cfg)

    assert result["status"] == "intent_parse_failed"
    assert len(sent) == 1
    fb = sent[0]
    assert fb["provenance"] == {"fallback_audit": "2026-06-10"}
    assert len(fb["text"]) <= LENGTH_CAP_TARGET
    assert not ACTION_VERB_PATTERN.search(fb["text"])
    stubs["claude"].compose_kavi_reply.assert_not_called()


def test_composer_failure_ships_safe_sentence(tmp_path, sent, stubs) -> None:
    cfg = _config(tmp_path)
    claude = stubs["claude"]
    claude.parse_reply_intents.return_value = [{
        "type": "conversational", "target_text": "hey", "targets": [],
        "confidence": "high",
    }]
    claude.compose_kavi_reply.return_value = None

    result = handlers.imessage_received(_payload("hey"), cfg)

    assert result["status"] == "intent_dispatch"
    assert len(sent) == 1
    assert sent[0]["provenance"] == {"fallback_audit": "2026-06-10"}
    assert not ACTION_VERB_PATTERN.search(sent[0]["text"])


def test_reaction_skips_parser(tmp_path, sent, stubs) -> None:
    cfg = _config(tmp_path)
    result = handlers.imessage_received(_payload('Liked "Heads up"'), cfg)
    assert result == {"status": "ignored", "reason": "reaction"}
    stubs["claude"].parse_reply_intents.assert_not_called()
    assert sent == []


def test_unknown_sender_discarded_before_parser(tmp_path, sent, stubs, monkeypatch) -> None:
    cfg = _config(tmp_path)
    from kavi_runtime.runtime import outbound_scanner
    monkeypatch.setattr(outbound_scanner, "gate_inbound_sender",
                        lambda **kw: (False, "unknown_sender"))
    result = handlers.imessage_received(_payload("close everything"), cfg)
    assert result["status"] == "discarded_unknown_sender"
    stubs["claude"].parse_reply_intents.assert_not_called()
    assert sent == []


def test_parser_receives_full_context(tmp_path, sent, stubs, monkeypatch) -> None:
    """The parser call carries the contract's context: her own recent
    inbound, Kavi's recent outbound, pending questions/facts, open tasks."""
    cfg = _config(tmp_path)
    # Seed her prior inbound + Kavi's outbound rows.
    Path(cfg["paths"]["eval_persona_inbound_jsonl"]).write_text(
        json.dumps({"sender": MEGHA, "text": "Close the maple tasks"}) + "\n"
    )
    Path(cfg["paths"]["eval_persona_outbound_judgments_jsonl"]).write_text(
        json.dumps({"kind": "periodic_summary", "text": "Want to close Boonli?"}) + "\n"
    )
    question = {"id": "q1", "task_title_rendered": "MJ Decide on PEPS"}
    monkeypatch.setattr(handlers, "list_pending_questions", lambda p: [question])

    claude = stubs["claude"]
    claude.parse_reply_intents.return_value = [{
        "type": "conversational", "target_text": "x", "targets": [],
        "confidence": "high",
    }]
    claude.compose_kavi_reply.return_value = "On it."

    handlers.imessage_received(_payload("what about the other tasks?"), cfg)

    kwargs = claude.parse_reply_intents.call_args.kwargs
    assert kwargs["inbound_text"] == "what about the other tasks?"
    assert kwargs["sender"] == "megha"
    assert kwargs["recent_inbound"] == ["Close the maple tasks"]
    assert kwargs["recent_outbound"][-1]["kind"] == "periodic_summary"
    assert kwargs["pending_questions"] == [question]
    assert kwargs["open_tasks"][0]["id"] == "t-boonli"
    # The composer then gets the same thread context.
    ckwargs = claude.compose_kavi_reply.call_args.kwargs
    assert ckwargs["recent_inbound"] == ["Close the maple tasks"]


def test_g_a1_blocks_ungrounded_claim_from_composer(tmp_path, sent, stubs) -> None:
    """Defense in depth: if the composer fabricates an action claim with no
    execution behind it, the existing G-A1 gate substitutes the alert
    fallback (and the provenance flips to the audited fallback)."""
    cfg = _config(tmp_path)
    claude = stubs["claude"]
    claude.parse_reply_intents.return_value = [{
        "type": "conversational", "target_text": "did you send it?", "targets": [],
        "confidence": "high",
    }]
    claude.compose_kavi_reply.return_value = "Sent the forms to Max already."

    handlers.imessage_received(_payload("did you send it?"), cfg)

    assert len(sent) == 1
    assert sent[0]["kind"] == "alert_fallback"
    assert "Sent the forms" not in sent[0]["text"]
    assert sent[0]["provenance"] == {"fallback_audit": "2026-06-10"}


def test_coordination_plus_create_no_double_message(
    tmp_path, sent, stubs, monkeypatch,
) -> None:
    """'Let Max know X and also add a task for him': the coordination layer
    owns the requester-facing turn (it sent its own ack and will report the
    outcome), so the bundled create_task must NOT trigger a second narration
    on top of it. Regression for the 2026-06-11 NWP/Hollis double-message
    ('Pinged Max... Task added for him too.')."""
    cfg = _config(tmp_path)
    claude, graph = stubs["claude"], stubs["graph"]
    claude.parse_reply_intents.return_value = [
        {"type": "coordination_reply",
         "target_text": "let Max know he needs to contact NWP",
         "targets": [], "confidence": "high"},
        {"type": "create_task", "target_text": "add a task for him",
         "task_title": "Contact NWP to connect Hollis", "owner": "max",
         "targets": [], "confidence": "high"},
    ]
    graph.create_todo_task.return_value = "task-new-1"

    from kavi_runtime.runtime import imessage_dispatch as _disp
    monkeypatch.setattr(
        _disp, "_coordination_dispatch_entry",
        lambda **kw: {"status": "coordination_started"},
    )

    result = handlers.imessage_received(
        _payload("Let Max know he needs to contact NWP and also add a task for him"),
        cfg,
    )

    assert result["status"] == "intent_dispatch"
    assert result.get("reply_owned_by_executor") is True
    # The create_task DID execute (a real Max task) ...
    graph.create_todo_task.assert_called_once()
    # ... but NO second narration shipped on top of the coordination ack.
    claude.compose_kavi_reply.assert_not_called()
