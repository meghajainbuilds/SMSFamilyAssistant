"""Tests for the coordination course-correction retry path (Bug 2, 2026-05-07).

Background: when an active coordination session has fired addressee_reach but
the requester (e.g., Megha) sends a follow-up BEFORE the addressee replies
("you sent it to me, not Max"), the inbound handler used to route through the
conversational composer, which fabricated an unverified "resending now"
string without firing a real retry.

The fix:
  - lookup_session_by_requester_handle finds the active session.
  - classify_coordination_course_correction decides whether the follow-up is
    a course-correction.
  - handle_requester_course_correction re-fires addressee_reach with the
    original composed text and replies to the requester with verified-from-
    tool-result language only.

Run with:
    cd kavi-runtime && uv run pytest tests/test_coordination_course_correction.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from capabilities.coordination import handler as coordination_handler


# ---- shared fixtures ------------------------------------------------------


def _config_for_test(tmp_path: Path) -> dict[str, Any]:
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
    coordination_handler._reset_for_tests()
    yield
    coordination_handler._reset_for_tests()


def _make_clients() -> tuple[MagicMock, MagicMock, MagicMock]:
    claude = MagicMock()
    claude.compose_coordination_ack.return_value = {
        "text": "Got it, checking with Max now.",
        "char_count": 30, "_usage": None,
    }
    claude.compose_coordination_addressee_message.return_value = {
        "text": "Hey Max, do you have cash for Rosa tomorrow morning?",
        "char_count": 50, "attribution_applied": False, "_usage": None,
    }
    # The default course-correction classifier returns true with high
    # confidence — individual tests override.
    claude.classify_coordination_course_correction.return_value = {
        "is_course_correction": True,
        "reason": "addressee_routing",
        "confidence": "high",
        "_usage": None,
    }
    graph = MagicMock()
    graph.create_task_in_shared_list.return_value = ("task_abc", True)
    bb = MagicMock()
    return claude, graph, bb


def _stub_send(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace _send_with_scanner with a recorder that returns a verified send.
    Mirrors the shape used by test_coordination_handler.py."""
    sends: list[dict[str, Any]] = []

    def _record(config, *, bb, text, recipient_handle, kind, chat_guid_override=None, **kwargs):
        sends.append({
            "text": text, "recipient": recipient_handle, "kind": kind,
            "blocked": False,
        })
        return {
            "sent": True, "verified": True, "blocked": False,
            "blocked_reason": None, "temp_guid": f"{kind}-fake",
        }

    monkeypatch.setattr(coordination_handler, "_send_with_scanner", _record)
    return sends


# ---- lookup_session_by_requester_handle ----------------------------------


def test_lookup_by_requester_finds_active_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """An active session whose requester is Megha is returned by
    lookup_session_by_requester_handle once addressee_reach has fired."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="ask Max if he has cash for Rosa tomorrow",
        requester_handle="+15555550101",
        addressee_handle="+15555550102",
        addressee_name="Max",
        coordination_ask="Does Max have cash for Rosa tomorrow?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    found = coordination_handler.lookup_session_by_requester_handle("+15555550101")
    assert found is not None
    assert found["session_id"] == started["session_id"]


def test_lookup_by_requester_returns_none_for_non_requester(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Looking up a handle that isn't the requester of any active session
    returns None — the course-correction path doesn't fire."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)

    coordination_handler.start_coordination(
        inbound_text="ask Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="ask",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    # Some unrelated handle.
    assert coordination_handler.lookup_session_by_requester_handle("+19990001111") is None


# ---- handle_requester_course_correction: happy path ----------------------


def test_course_correction_retries_addressee_reach_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """High-confidence course-correction → re-fires addressee_reach with the
    SAME composed text, then sends Megha a verified-from-tool-result ack.
    This is the Bug 2 fix verified end-to-end through the handler."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="ask Max if he has cash for Rosa tomorrow",
        requester_handle="+15555550101",
        addressee_handle="+15555550102",
        addressee_name="Max",
        coordination_ask="Does Max have cash for Rosa tomorrow?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]

    # Initial sequence: ack to Megha + addressee_reach to Max.
    assert [s["kind"] for s in sends] == ["ack", "addressee_reach"]
    initial_addressee_text = sends[1]["text"]

    # Megha course-corrects.
    cc_result = coordination_handler.handle_requester_course_correction(
        session_id=sid,
        follow_up_text="you sent it to me, not Max",
        config=cfg, claude=claude, bb=bb,
    )
    assert cc_result is not None
    assert cc_result["status"] == "coordination_retry_addressee_reach_verified"
    assert cc_result["session_id"] == sid

    # Sequence now: ack, addressee_reach, addressee_reach_retry, course_correction_ack.
    kinds = [s["kind"] for s in sends]
    assert kinds == [
        "ack", "addressee_reach", "addressee_reach_retry",
        "course_correction_ack",
    ]
    # Retry uses the SAME composed text — we don't recompose; the bug was
    # routing, not content.
    retry_send = sends[2]
    assert retry_send["recipient"] == "+15555550102"
    assert retry_send["text"] == initial_addressee_text

    # The ack to Megha is grounded in tool-result language — never an LLM
    # "resending" hallucination. The exact phrase comes from the deterministic
    # send-result branch in handle_requester_course_correction.
    ack_send = sends[3]
    assert ack_send["recipient"] == "+15555550101"
    assert "Retried" in ack_send["text"]
    assert "Max" in ack_send["text"]


def test_course_correction_skips_when_classifier_says_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """When the classifier returns is_course_correction=false, the handler
    returns None and DOES NOT re-fire addressee_reach. The caller falls
    through to the conversational layer."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    claude.classify_coordination_course_correction.return_value = {
        "is_course_correction": False,
        "reason": "other",
        "confidence": "high",
        "_usage": None,
    }
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="ask Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="ask",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]
    initial_send_count = len(sends)

    cc_result = coordination_handler.handle_requester_course_correction(
        session_id=sid, follow_up_text="thanks!",
        config=cfg, claude=claude, bb=bb,
    )
    assert cc_result is None
    # No retry, no extra sends.
    assert len(sends) == initial_send_count


def test_course_correction_skips_when_confidence_is_medium(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Conservative bias: medium-confidence course-corrections are ignored.
    We only act on high. Anything else falls through."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    claude.classify_coordination_course_correction.return_value = {
        "is_course_correction": True,
        "reason": "addressee_routing",
        "confidence": "medium",
        "_usage": None,
    }
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="ask Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="ask",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    cc_result = coordination_handler.handle_requester_course_correction(
        session_id=started["session_id"],
        follow_up_text="hmm not sure that worked",
        config=cfg, claude=claude, bb=bb,
    )
    assert cc_result is None
    assert "addressee_reach_retry" not in [s["kind"] for s in sends]


def test_course_correction_caps_at_one_retry_per_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """A mis-classified course-correction can't loop: a second high-confidence
    follow-up after a retry is treated as no-op. Defensive cap."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    sends = _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="ask Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="ask",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    sid = started["session_id"]

    # First retry succeeds.
    first = coordination_handler.handle_requester_course_correction(
        session_id=sid, follow_up_text="you sent it to me, not Max",
        config=cfg, claude=claude, bb=bb,
    )
    assert first is not None
    sends_after_first = [s["kind"] for s in sends]

    # Second course-correction is a no-op.
    second = coordination_handler.handle_requester_course_correction(
        session_id=sid, follow_up_text="seriously, retry",
        config=cfg, claude=claude, bb=bb,
    )
    assert second is None
    # No new sends after the cap.
    assert [s["kind"] for s in sends] == sends_after_first


def test_course_correction_returns_none_for_unknown_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """An unknown session_id returns None safely. The cap matters because
    the inbound handler does its own lookup before calling here, but defensive
    coverage avoids a KeyError on a stale session_id."""
    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)

    cc = coordination_handler.handle_requester_course_correction(
        session_id="s_does_not_exist",
        follow_up_text="you sent it to me, not Max",
        config=cfg, claude=claude, bb=bb,
    )
    assert cc is None


# ---- audit log row written for the retry phase ---------------------------


def test_course_correction_writes_audit_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """The retry appends one audit row with phase=addressee_reach_retry tagged
    with the course-correction reason + confidence + send-verified state."""
    import json as _json

    cfg = _config_for_test(tmp_path)
    claude, graph, bb = _make_clients()
    _stub_send(monkeypatch)

    started = coordination_handler.start_coordination(
        inbound_text="ask Max", requester_handle="+15555550101",
        addressee_handle="+15555550102", addressee_name="Max",
        coordination_ask="ask",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    coordination_handler.handle_requester_course_correction(
        session_id=started["session_id"],
        follow_up_text="you sent it to me, not Max",
        config=cfg, claude=claude, bb=bb,
    )

    log_path = Path(cfg["paths"]["eval_coordinates_judgments_jsonl"])
    rows = [_json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    retry_rows = [r for r in rows if r.get("phase") == "addressee_reach_retry"]
    assert len(retry_rows) == 1
    extras = retry_rows[0].get("extras") or {}
    assert extras.get("course_correction_reason") == "addressee_routing"
    assert extras.get("course_correction_confidence") == "high"
    assert extras.get("retry_count") == 1
    assert extras.get("send_verified") is True


# ---- integration: handler routes the inbound to the course-correction path -


def test_imessage_received_routes_requester_followup_to_course_correction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Full-loop integration: imessage_received → coordination_handler. When
    Megha sends a course-correction mid-coordination, the handler runs the
    course-correction path INSTEAD of the conversational composer.

    We assert via the return-shape: course-correction returns
    `coordination_retry_addressee_reach_verified`, conversational returns
    `conversational_reply`. Verifying the bypass invariant for Bug 2 directly.
    """
    from kavi_runtime import handlers

    cfg = _config_for_test(tmp_path)
    cfg["paths"]["imessage_state"] = str(tmp_path / "imessage-state.json")
    cfg["paths"]["corrections_jsonl"] = str(tmp_path / "corrections.jsonl")

    # Reset cached handler clients.
    from kavi_runtime.runtime import clients as _clients
    monkeypatch.setattr(_clients, "_graph_client", None)
    monkeypatch.setattr(_clients, "_claude_client", None)
    monkeypatch.setattr(_clients, "_bb_client", None)

    claude, graph, bb = _make_clients()
    # The conversational composer must NEVER be called on the course-correction
    # path. Wire AssertionError so a leak fails loudly.
    claude.compose_conversational_reply.side_effect = AssertionError(
        "compose_conversational_reply was invoked on a course-correction inbound"
    )
    monkeypatch.setattr(
        handlers, "_get_clients", lambda cfg: (graph, claude, bb),
    )
    sends = _stub_send(monkeypatch)

    # Seed an active coordination session.
    started = coordination_handler.start_coordination(
        inbound_text="ask Max if he has cash for Rosa tomorrow",
        requester_handle="+15555550101",
        addressee_handle="+15555550102",
        addressee_name="Max",
        coordination_ask="Does Max have cash for Rosa tomorrow?",
        config=cfg, claude=claude, graph=graph, bb=bb,
    )
    assert started["status"] == "coordination_started"

    # Now Megha sends a course-correction.
    payload = {
        "type": "new-message",
        "data": {
            "isFromMe": False,
            "text": "you sent it to me, not Max",
            "handle": {"address": "+15555550101"},
            "chats": [{"guid": "any;-;+15555550101"}],
        },
    }
    result = handlers.imessage_received(payload, cfg)
    assert result["status"] == "coordination_retry_addressee_reach_verified"

    # The retry + ack landed; conversational composer never ran.
    kinds = [s["kind"] for s in sends]
    assert "addressee_reach_retry" in kinds
    assert "course_correction_ack" in kinds
    claude.compose_conversational_reply.assert_not_called()
