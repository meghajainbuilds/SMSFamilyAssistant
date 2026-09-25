"""Tests for the mark-done verb of the iMessage to task capability after the
2026-05-05 evening rework:

  Fix 1 — live execution (dry_run flipped to false; tests assert the PATCH
          fires when match is high-confidence).
  Fix 2 — exact-string match replaced with LLM-driven match
          (`match_target_to_open_task`); tests cover high / medium-or-low /
          no-match outcomes.
  Fix 3 — action-layer always replies for action-implying inbounds; tests
          assert the conversational composer is NEVER invoked when
          has_action=true.

Unit-style tests: the Anthropic API and the MS Graph HTTP layer are mocked.
We exercise the dispatcher's wiring (does it call the right collaborator
with the right shape) and the bypass invariant.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_mark_done_verb.py -v
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers


# ---- shared test helpers --------------------------------------------------


def _config_for_test() -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "graph": {
            "mstodo_shared_list_id": "AQMkADAwTEST==",
        },
        "action_layer": {"dry_run": False},
        "paths": {
            "eval_persona_action_intent_jsonl": "/tmp/test_action_intent_mark_done.jsonl",
        },
    }


def _open_tasks_fixture() -> list[dict[str, Any]]:
    """Two open tasks plus one already-completed task. Mirrors the shape
    list_recent_todo_tasks returns from MS Graph (the fields the dispatcher
    actually reads)."""
    return [
        {"id": "t_uw", "title": "MJ Pay UW Medicine overdue balance ($630.00)", "status": "notStarted"},
        {"id": "t_boonli", "title": "MJ Boonli May menu payment", "status": "notStarted"},
        {"id": "t_joan", "title": "MJ Joan Miller VP role research", "status": "completed"},
    ]


def _patch_send_paths(monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]) -> None:
    """Capture every send call made by the action layer so tests can assert on
    reply_text and confirm the conversational composer was not invoked.

    Patches BOTH `_send_imessage_with_fallback` and
    `_send_imessage_with_fallback_and_context` since the action layer uses both.
    """
    def _send_plain(config: dict, text: str, *, kind: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": None})
        return {"verified": True, "fallback_used": False}

    def _send_with_context(config: dict, text: str, *, kind: str, context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": context})
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send_plain)
    monkeypatch.setattr(handlers, "_send_imessage_with_fallback_and_context", _send_with_context)
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)


_LLM_CLARIFY_FIXTURE = (
    "Got UW Medicine balance ($630). Confirm to mark done?"
)


def _mock_claude_for_intent(
    *, has_action: bool, action_type: str | None, target_text: str | None,
    confidence: str, match_id: str | None, match_conf: str,
    match_reasoning: str = "test reasoning",
    candidate_task_ids: list[str] | None = None,
    clarifying_reply: str | None = _LLM_CLARIFY_FIXTURE,
) -> MagicMock:
    """Build a ClaudeClient mock that returns the given action-intent and
    target-match outcomes. compose_conversational_reply is configured to RAISE
    if it ever gets called — the test asserts the bypass invariant by relying
    on this raise to surface as a test failure.

    `clarifying_reply` controls what `compose_action_clarifying_reply` returns
    when the action layer needs an LLM-composed clarifying message. Set to
    None to simulate LLM unreachable (cold-fallback path).

    `candidate_task_ids` is the matcher's structured-output list (added
    2026-05-08). Defaults to `[match_id]` when match_id is set, else `[]`.
    """
    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": has_action,
        "action_type": action_type,
        "target_text": target_text,
        "confidence": confidence,
    }
    if candidate_task_ids is None:
        candidate_task_ids = [match_id] if match_id else []
    claude.match_target_to_open_task.return_value = {
        "match_id": match_id,
        "candidate_task_ids": candidate_task_ids,
        "confidence": match_conf,
        "reasoning": match_reasoning,
    }
    claude.compose_post_action_reply.return_value = "Marked done. That's closed out."
    claude.compose_action_clarifying_reply.return_value = clarifying_reply

    def _no_conversational(*a, **kw):  # pragma: no cover — assertion path
        raise AssertionError(
            "compose_conversational_reply must NOT be called for action-implying inbounds"
        )
    claude.compose_conversational_reply.side_effect = _no_conversational
    return claude


def _mock_graph(open_tasks: list[dict[str, Any]] | None = None) -> MagicMock:
    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks (server-
    # side $filter on status); list_recent_todo_tasks is no longer the
    # call site. Stub both to keep older tests resilient if they reach
    # via the legacy path; the production path now goes through the new.
    slate = open_tasks if open_tasks is not None else _open_tasks_fixture()
    graph.list_open_todo_tasks.return_value = slate
    graph.list_recent_todo_tasks.return_value = slate
    # mark_task_done returns (success_bool, post_title_str)
    graph.mark_task_done.return_value = (True, "MJ Pay UW Medicine overdue balance ($630.00)")
    return graph


# ---- (a) high-confidence LLM match → PATCH + "Marked done" reply ---------


def test_high_confidence_match_executes_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Classifier says mark_done + high; matcher says high-confidence match on
    t_uw. The dispatcher must call graph.mark_task_done(list_id, t_uw) and
    ship a post-action reply. The conversational composer must NOT be called.
    """
    cfg = _config_for_test()
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    claude = _mock_claude_for_intent(
        has_action=True, action_type="mark_done",
        target_text="UW Medicine balance ($630)", confidence="high",
        match_id="t_uw", match_conf="high",
    )
    graph = _mock_graph()

    result = handlers._try_handle_action_intent(
        free_text="mark UW Medicine balance ($630) done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_001",
    )

    assert result is not None
    assert result["status"] == "action_executed"
    assert result["action_type"] == "mark_done"
    assert result["result"] == "success"
    assert result["task_id"] == "t_uw"

    graph.mark_task_done.assert_called_once_with(cfg["graph"]["mstodo_shared_list_id"], "t_uw")
    assert any(s["kind"] == "post_action_reply" for s in sent)
    # Bypass invariant: conversational composer never invoked.
    claude.compose_conversational_reply.assert_not_called()


# ---- (b) ambiguous match (medium) → no PATCH + clarifying reply ----------


def test_medium_confidence_match_skips_patch_sends_clarifying(monkeypatch: pytest.MonkeyPatch) -> None:
    """Classifier says mark_done + high; matcher says medium-confidence match
    (ambiguous). Dispatcher must NOT call mark_task_done and must ship a
    clarifying reply with up to three candidate titles."""
    cfg = _config_for_test()
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    claude = _mock_claude_for_intent(
        has_action=True, action_type="mark_done",
        target_text="payment", confidence="high",
        match_id="t_uw", match_conf="medium",
        match_reasoning="UW Medicine and Boonli both plausible matches",
    )
    graph = _mock_graph()

    result = handlers._try_handle_action_intent(
        free_text="mark the payment done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_002",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"
    assert result["action_type"] == "mark_done"
    assert result["reason"] == "ambiguous_match"

    graph.mark_task_done.assert_not_called()
    assert sent, "expected at least one outbound from the clarifying reply"
    reply_text = sent[-1]["text"]
    # Per Principle 7, the clarifying reply is LLM-composed. The reply we
    # see should be the LLM fixture, NOT the legacy "Couldn't pin that down"
    # template. Negative assertion guards against regressing to the template.
    assert reply_text == _LLM_CLARIFY_FIXTURE
    assert "Couldn't pin that down" not in reply_text
    claude.compose_action_clarifying_reply.assert_called_once()
    claude.compose_conversational_reply.assert_not_called()


# ---- (c) no match → no PATCH + honest "couldn't find" reply --------------


def test_no_match_skips_patch_sends_honest_couldnt_find(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matcher returns match_id=None, confidence=low. Dispatcher must NOT call
    mark_task_done and must ship a clarifying reply."""
    cfg = _config_for_test()
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    claude = _mock_claude_for_intent(
        has_action=True, action_type="mark_done",
        target_text="the unicorn task", confidence="high",
        match_id=None, match_conf="low",
        match_reasoning="no semantic match in the open list",
    )
    graph = _mock_graph()

    result = handlers._try_handle_action_intent(
        free_text="mark the unicorn task done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_003",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"
    assert result["reason"] == "no_match"
    graph.mark_task_done.assert_not_called()
    assert sent
    claude.compose_conversational_reply.assert_not_called()


# ---- (d) already-completed match → no PATCH + honest "already done" reply -


def test_already_completed_match_skips_patch_sends_already_done(monkeypatch: pytest.MonkeyPatch) -> None:
    """High-confidence match on a task whose status is already 'completed'.
    Dispatcher must NOT call mark_task_done (the PATCH would 200 but be a no-op
    that fabricates a completion claim) and must ship an honest already-done
    reply."""
    cfg = _config_for_test()
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    claude = _mock_claude_for_intent(
        has_action=True, action_type="mark_done",
        target_text="Joan Miller VP role", confidence="high",
        match_id="t_joan", match_conf="high",
    )
    graph = _mock_graph()

    result = handlers._try_handle_action_intent(
        free_text="mark Joan Miller VP role done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_004",
    )

    assert result is not None
    assert result["status"] == "action_executed"
    assert result["result"] == "already_completed"
    graph.mark_task_done.assert_not_called()
    assert sent
    reply_text = sent[-1]["text"]
    assert "already" in reply_text.lower()
    claude.compose_conversational_reply.assert_not_called()


# ---- (e) bypass invariant: conversational composer never invoked ---------


def test_bypass_invariant_for_every_action_implying_inbound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Walks every has_action=true branch (mark_done high/medium/low/none,
    create high/low, update detect-only, cancel detect-only) and asserts that
    none of them invokes compose_conversational_reply. This is the core
    serialization fix: action-implying inbounds always reply via the action
    layer."""
    cfg = _config_for_test()

    scenarios: list[dict[str, Any]] = [
        # mark_done branches
        {"intent": {"has_action": True, "action_type": "mark_done",
                    "target_text": "UW Medicine balance", "confidence": "high"},
         "match": {"match_id": "t_uw", "confidence": "high", "reasoning": "ok"}},
        {"intent": {"has_action": True, "action_type": "mark_done",
                    "target_text": "payment", "confidence": "high"},
         "match": {"match_id": "t_uw", "confidence": "medium", "reasoning": "ambig"}},
        {"intent": {"has_action": True, "action_type": "mark_done",
                    "target_text": "the unicorn task", "confidence": "high"},
         "match": {"match_id": None, "confidence": "low", "reasoning": "none"}},
        {"intent": {"has_action": True, "action_type": "mark_done",
                    "target_text": "fuzzy thing", "confidence": "medium"},
         "match": {"match_id": None, "confidence": "low", "reasoning": "skipped"}},
        {"intent": {"has_action": True, "action_type": "mark_done",
                    "target_text": None, "confidence": "high"},
         "match": {"match_id": None, "confidence": "low", "reasoning": "skipped"}},
        # create branches — low confidence must still bypass conversational composer
        {"intent": {"has_action": True, "action_type": "create",
                    "target_text": "buy milk", "confidence": "medium"},
         "match": {"match_id": None, "confidence": "low", "reasoning": "n/a"}},
        # update / cancel detect-only
        {"intent": {"has_action": True, "action_type": "update",
                    "target_text": "Boonli", "confidence": "high"},
         "match": {"match_id": None, "confidence": "low", "reasoning": "n/a"}},
        {"intent": {"has_action": True, "action_type": "cancel",
                    "target_text": "dry cleaning", "confidence": "high"},
         "match": {"match_id": None, "confidence": "low", "reasoning": "n/a"}},
    ]

    for sc in scenarios:
        sent: list[dict[str, Any]] = []
        _patch_send_paths(monkeypatch, sent)

        claude = MagicMock()
        claude.classify_action_intent.return_value = sc["intent"]
        claude.match_target_to_open_task.return_value = sc["match"]
        claude.compose_post_action_reply.return_value = "Marked done."
        claude.compose_action_clarifying_reply.return_value = (
            "Got it. Want to confirm which task?"
        )

        def _no_conversational(*a, **kw):  # pragma: no cover
            raise AssertionError(
                f"compose_conversational_reply called for scenario {sc['intent']}"
            )
        claude.compose_conversational_reply.side_effect = _no_conversational

        graph = _mock_graph()

        result = handlers._try_handle_action_intent(
            free_text="some inbound",
            recent_outbound=[],
            claude=claude, graph=graph,
            list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
            sender_handle="+15555550101", source_imessage_id=f"im_e_{id(sc)}",
        )
        # Bypass invariant: action-implying inbound MUST return a non-None
        # dict so the caller does NOT fall through to the conversational reply.
        assert result is not None, f"action-implying scenario returned None: {sc}"
        # And of course, compose_conversational_reply was never invoked.
        claude.compose_conversational_reply.assert_not_called()
        # And at least one reply was shipped via the action-layer send path.
        assert sent, f"no reply shipped for scenario {sc}"


# ---- has_action=false still falls through (regression guard) -------------


def test_no_action_returns_none_for_conversational_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the classifier says has_action=false, _try_handle_action_intent
    must return None so the caller can run the conversational reply path.
    This is the ONE case that does NOT bypass the conversational composer."""
    cfg = _config_for_test()
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": False, "action_type": None,
        "target_text": None, "confidence": "high",
    }
    graph = _mock_graph()

    result = handlers._try_handle_action_intent(
        free_text="how are you doing?",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_999",
    )
    assert result is None
    # No send happens here — the caller will compose the conversational reply.
    assert sent == []


# ---- Principle 7: clarifying reply is LLM-composed, not templated --------


def test_low_confidence_intent_uses_llm_clarifying_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """When classify_action_intent returns confidence != high (or empty
    target_text), the clarifying reply path is used. Per Principle 7, that
    path MUST go through compose_action_clarifying_reply (the LLM), NOT the
    legacy `_compose_clarifying_reply` template that leaked the literal word
    "that" into Megha's iMessage.

    Regression guard: the multi-item case from 2026-05-07 where Megha sent
    four tasks in one message. Classifier returned has_action=true,
    confidence=low, target_text=''. Old code emitted "Couldn't pin that down
    on 'that'." This test asserts the LLM clarifying composer is invoked
    with the full free_text + open_tasks so it can compose a real reply."""
    cfg = _config_for_test()
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    free_text = (
        "Mark the following to do items done MJ decide on possible meeting "
        "with Kelly UDub medical bill all Maple Street newsletters MJ "
        "can access Dana Lim's content library"
    )
    expected_llm_reply = (
        "Four items, no exact matches. Read those as Kelly Brandt, UW "
        "Medicine, Dana Park, May 5 Maple. Confirm to mark all four?"
    )
    # Matcher surfaces UW Medicine as a candidate (multi-item paraphrase).
    claude = _mock_claude_for_intent(
        has_action=True, action_type="mark_done",
        target_text="", confidence="low",
        match_id=None, match_conf="medium",
        candidate_task_ids=["t_uw"],
        clarifying_reply=expected_llm_reply,
    )
    graph = _mock_graph()

    result = handlers._try_handle_action_intent(
        free_text=free_text,
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_multi",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"
    assert result["action_type"] == "mark_done"

    # Critical: the LLM clarifying composer was invoked with the matcher's
    # surfaced candidates (paraphrase-grounded subset, NOT the full slate).
    # The matcher's `candidate_task_ids` is the source of truth for what
    # the composer sees — see Fix B in the 2026-05-08 LLM-first revert.
    claude.compose_action_clarifying_reply.assert_called_once()
    call = claude.compose_action_clarifying_reply.call_args
    assert call.kwargs["free_text"] == free_text
    assert call.kwargs["action_type"] == "mark_done"
    assert call.kwargs["open_tasks"] is None  # NOT the full slate any more
    assert call.kwargs["candidates"]  # matcher's identified subset
    assert any(
        "UW Medicine" in (c.get("title") or "")
        for c in call.kwargs["candidates"]
    )

    # The reply Megha sees is the LLM output.
    assert sent and sent[-1]["text"] == expected_llm_reply

    # And it does NOT contain the legacy template's placeholder symptoms.
    reply = sent[-1]["text"]
    assert "Couldn't pin that down" not in reply
    assert "on 'that'" not in reply
    assert "Which task did you mean?" not in reply

    claude.compose_conversational_reply.assert_not_called()


def test_clarifying_reply_cold_fallback_when_llm_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the LLM clarifying composer returns None (LLM unreachable, parse
    error, over-length, etc.), the action layer falls back to a visibly canned
    template that announces itself as a degraded fallback. The old template
    would have leaked "Couldn't pin that down on 'that'" with no signal to
    Megha that the LLM was down."""
    cfg = _config_for_test()
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    claude = _mock_claude_for_intent(
        has_action=True, action_type="mark_done",
        target_text="", confidence="low",
        match_id=None, match_conf="low",
        clarifying_reply=None,  # simulate LLM unreachable
    )
    graph = _mock_graph()

    result = handlers._try_handle_action_intent(
        free_text="mark the four things done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_cold",
    )

    assert result is not None
    assert sent
    reply = sent[-1]["text"]
    # 2026-05-29 (Phase 1 cold-fallback audit): the old template
    # ("⚠️ degraded reply: ... Possible matches: (a) X; (b) Y. (Auto-
    # fallback.)") violated g_v2_prose (enumeration) and g_p1_no_status_
    # board ("degraded reply", "Auto-fallback"). Deleted. Replaced with
    # one safe deterministic sentence: "Couldn't compose a clarifying
    # reply [on 'X']. Try again?"
    assert "Couldn't compose" in reply
    assert "Try again?" in reply
    assert "(a)" not in reply  # no enumeration
    assert "degraded" not in reply  # no status board
    assert "Auto-fallback" not in reply  # no parenthetical tag
    # And it never invokes the conversational composer.
    claude.compose_conversational_reply.assert_not_called()


def test_update_verb_uses_llm_clarifying_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """update / cancel verbs are not yet wired but still send a reply.
    Per Principle 7, that reply is LLM-composed, not the old f-string
    template that exposed action_type strings ("you want to update X")
    and the literal word "that"."""
    cfg = _config_for_test()
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    expected_reply = (
        "Found 'Boonli May menu payment'. I can't update tasks yet — "
        "want me to mark it done so it drops off the list?"
    )
    claude = _mock_claude_for_intent(
        has_action=True, action_type="update",
        target_text="Boonli", confidence="high",
        match_id=None, match_conf="low",
        clarifying_reply=expected_reply,
    )
    graph = _mock_graph()

    result = handlers._try_handle_action_intent(
        free_text="update the Boonli task to next week",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_update",
    )

    assert result is not None
    assert result["status"] == "action_detect_only"
    assert result["action_type"] == "update"

    claude.compose_action_clarifying_reply.assert_called_once()
    assert sent and sent[-1]["text"] == expected_reply
    # Old template signature must be gone.
    reply = sent[-1]["text"]
    assert "I see you want to update 'that'" not in reply
    assert "verb isn't wired yet" not in reply  # specifically the old template phrase
    claude.compose_conversational_reply.assert_not_called()
