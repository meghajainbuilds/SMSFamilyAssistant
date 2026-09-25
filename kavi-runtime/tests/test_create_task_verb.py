"""Tests for the create-task verb of the iMessage to task capability
(capabilities/imessage-to-task.md, 2026-05-05).

Covers:
- The dispatcher (`_handle_create_task_verb`) calls
  `GraphClient.create_task_in_shared_list` with the expected (title,
  owner_prefix, deadline) when the classifier returns
  intent=create + confidence=high.
- The owner-prefix defaulter maps Megha's phone to MJ and Max's phone to MM.
- A duplicate webhook fire on the same source_imessage_id does NOT create a
  duplicate task (natural-key dedup).
- Cosmetic smoke: the verb function is importable and callable.

These are unit-style tests — the Anthropic API and the MS Graph HTTP layer are
both mocked. We exercise the dispatcher's wiring (does it forward the right
arguments to the right collaborator), not the LLM judgment or the network call
shape.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_create_task_verb.py -v
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers


# ---- cosmetic smoke -------------------------------------------------------


def test_create_task_verb_is_callable() -> None:
    """The create-task verb function exists and is callable. Cosmetic — guards
    against import-time regressions if the dispatcher is renamed in a future
    refactor without updating the test."""
    assert callable(handlers._handle_create_task_verb)
    assert callable(handlers._owner_prefix_from_sender)
    assert callable(handlers._parse_deadline_iso)


# ---- owner-prefix defaulter -----------------------------------------------


def _config_for_test() -> dict[str, Any]:
    """Minimal config dict for the owner-prefix defaulter and create-task verb
    paths. Mirrors the production config.yaml shape for the keys these
    functions read."""
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "max_phone": "+15555550102",
            "own_email_addresses": ["megha@example.com"],
        },
        "graph": {
            "mstodo_shared_list_id": "AQMkADAwTEST==",
        },
        "action_layer": {"dry_run": False},
        "paths": {
            "eval_persona_action_intent_jsonl": "/tmp/test_action_intent.jsonl",
        },
    }


def test_owner_prefix_megha_phone_maps_to_mj() -> None:
    cfg = _config_for_test()
    assert handlers._owner_prefix_from_sender("+15555550101", cfg) == "MJ"


def test_owner_prefix_megha_email_maps_to_mj() -> None:
    cfg = _config_for_test()
    assert handlers._owner_prefix_from_sender("megha@example.com", cfg) == "MJ"


def test_owner_prefix_max_phone_maps_to_mm() -> None:
    cfg = _config_for_test()
    assert handlers._owner_prefix_from_sender("+15555550102", cfg) == "MM"


def test_owner_prefix_unknown_handle_defaults_to_mj() -> None:
    """Default for unknown sender is MJ (Megha is the household default
    requester). Documented in `_owner_prefix_from_sender` docstring as the
    intentional fallback rather than `??`."""
    cfg = _config_for_test()
    assert handlers._owner_prefix_from_sender("+19995550000", cfg) == "MJ"
    assert handlers._owner_prefix_from_sender(None, cfg) == "MJ"


# ---- deadline parser ------------------------------------------------------


def test_parse_deadline_iso_extracts_yyyy_mm_dd() -> None:
    out = handlers._parse_deadline_iso("call pediatrician 2026-05-12")
    assert out == datetime(2026, 5, 12)


def test_parse_deadline_iso_returns_none_for_natural_language() -> None:
    """Acceptance criterion (c): deadline is parsed from the inbound when
    present; left empty otherwise. Natural-language phrases ("tomorrow",
    "by Friday") fall into the "left empty" bucket today — the deadline
    string still survives in the title body so Megha sees it on the task,
    but the structured dueDateTime is None. Documented divergence from
    the most permissive reading of (c)."""
    assert handlers._parse_deadline_iso("call pediatrician tomorrow") is None
    assert handlers._parse_deadline_iso("by Friday") is None
    assert handlers._parse_deadline_iso("") is None


# ---- dispatcher: high-confidence create routes to graph client -----------


def _make_mocks(create_returns: tuple[str, bool] = ("task_abc", True)):
    """Returns (claude_mock, graph_mock) wired to return sane defaults."""
    claude = MagicMock()
    claude.compose_post_action_reply.return_value = "Added 'X' to the shared list."
    graph = MagicMock()
    graph.create_task_in_shared_list.return_value = create_returns
    return claude, graph


def test_dispatcher_invokes_create_with_expected_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Classifier says create + high confidence; dispatcher must call
    graph.create_task_in_shared_list with the title from target_text, the
    owner_prefix derived from sender, deadline parsed from the title, and the
    source_imessage_id passed through.
    """
    cfg = _config_for_test()
    claude, graph = _make_mocks()

    # Stub the iMessage send + log so the verb runs end-to-end without
    # touching BlueBubbles or the outbound log file.
    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback_and_context",
        lambda *a, **kw: {"verified": True, "fallback_used": False},
    )
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)

    intent = {
        "has_action": True,
        "action_type": "create",
        "target_text": "call the pediatrician 2026-05-12",
        "confidence": "high",
    }
    result = handlers._handle_create_task_verb(
        free_text="Add a task to call the pediatrician 2026-05-12",
        intent=intent,
        target_text=intent["target_text"],
        confidence="high",
        sender_handle="+15555550101",  # Megha's phone -> MJ
        source_imessage_id="im_guid_001",
        claude=claude,
        graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"],
        config=cfg,
    )

    assert result is not None
    assert result["status"] == "action_executed"
    assert result["action_type"] == "create_task"
    assert result["result"] == "success"
    assert result["task_id"] == "task_abc"
    assert result["owner_prefix"] == "MJ"
    assert result["dedup_hit"] is False

    graph.create_task_in_shared_list.assert_called_once()
    call_kwargs = graph.create_task_in_shared_list.call_args
    # Positional list_id then keyword args.
    assert call_kwargs.args[0] == cfg["graph"]["mstodo_shared_list_id"]
    assert call_kwargs.kwargs["title"] == "call the pediatrician 2026-05-12"
    assert call_kwargs.kwargs["owner_prefix"] == "MJ"
    assert call_kwargs.kwargs["deadline"] == datetime(2026, 5, 12)
    assert call_kwargs.kwargs["source_imessage_id"] == "im_guid_001"


def test_dispatcher_max_sender_picks_mm_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inbound from Max's phone -> owner_prefix=MM. Same wiring path."""
    cfg = _config_for_test()
    claude, graph = _make_mocks(create_returns=("task_xyz", True))
    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback_and_context",
        lambda *a, **kw: {"verified": True, "fallback_used": False},
    )
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)

    intent = {
        "has_action": True, "action_type": "create",
        "target_text": "pick up dry cleaning", "confidence": "high",
    }
    result = handlers._handle_create_task_verb(
        free_text="Add a task to pick up dry cleaning",
        intent=intent, target_text=intent["target_text"], confidence="high",
        sender_handle="+15555550102",  # Max's phone -> MM
        source_imessage_id="im_guid_002",
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
    )
    assert result["owner_prefix"] == "MM"
    assert graph.create_task_in_shared_list.call_args.kwargs["owner_prefix"] == "MM"


# ---- dispatcher: confidence + empty title fall through --------------------


def test_dispatcher_falls_through_on_medium_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config_for_test()
    claude, graph = _make_mocks()
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)

    intent = {
        "has_action": True, "action_type": "create",
        "target_text": "call someone", "confidence": "medium",
    }
    result = handlers._handle_create_task_verb(
        free_text="maybe add a task", intent=intent,
        target_text=intent["target_text"], confidence="medium",
        sender_handle="+15555550101", source_imessage_id="im_guid_003",
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
    )
    assert result is None
    graph.create_task_in_shared_list.assert_not_called()


def test_dispatcher_falls_through_on_empty_target(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config_for_test()
    claude, graph = _make_mocks()
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)

    intent = {
        "has_action": True, "action_type": "create",
        "target_text": "   ", "confidence": "high",
    }
    result = handlers._handle_create_task_verb(
        free_text="add a task to do the thing", intent=intent,
        target_text="   ", confidence="high",
        sender_handle="+15555550101", source_imessage_id="im_guid_004",
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
    )
    assert result is None
    graph.create_task_in_shared_list.assert_not_called()


# ---- natural-key dedup (idempotency) --------------------------------------


def test_dispatcher_reports_dedup_hit_when_graph_returns_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance criterion (d): a duplicate webhook fire on the same
    source_imessage_id does NOT create a duplicate task. The verb relies on
    `GraphClient.create_task_in_shared_list` to short-circuit and return
    `created=False` when an existing linkedResource matches the dedup key.
    The dispatcher must surface `dedup_hit=True` to the caller and tag the
    eval row decision as `executed_dedup_hit`.
    """
    cfg = _config_for_test()
    # Graph returns an existing task id with created=False — the dedup case.
    claude, graph = _make_mocks(create_returns=("task_existing_99", False))

    decisions: list[dict[str, Any]] = []

    def _capture_decision(*args, **kwargs) -> None:
        decisions.append({"decision": kwargs.get("decision"), "executed": kwargs.get("executed")})

    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback_and_context",
        lambda *a, **kw: {"verified": True, "fallback_used": False},
    )
    monkeypatch.setattr(handlers, "_log_action_intent_decision", _capture_decision)

    intent = {
        "has_action": True, "action_type": "create",
        "target_text": "remind me to confirm camp", "confidence": "high",
    }
    result = handlers._handle_create_task_verb(
        free_text="Add a task to confirm camp", intent=intent,
        target_text=intent["target_text"], confidence="high",
        sender_handle="+15555550101", source_imessage_id="im_guid_dup_001",
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
    )
    assert result is not None
    assert result["dedup_hit"] is True
    assert result["task_id"] == "task_existing_99"
    assert decisions and decisions[0]["decision"] == "executed_dedup_hit"
    assert decisions[0]["executed"] is False  # not a fresh execute on dedup


def test_create_task_in_shared_list_dedup_short_circuits_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure unit test on `GraphClient.create_task_in_shared_list` natural-key
    dedup: when `find_todo_task_by_source_email` returns an existing task id
    for the source_imessage_id, the method must NOT issue a POST. We patch the
    httpx layer to detect any leaked POST and assert it never fires.
    """
    from kavi_runtime import graph_client as gc_mod

    # Bare GraphClient instance bypassing __init__ (we don't need the auth
    # path for this test — we only exercise the dedup short-circuit).
    gc = gc_mod.GraphClient.__new__(gc_mod.GraphClient)

    # Stub the dedup lookup to report an existing match. The real signature
    # accepts an `account=` kwarg added during multi-account onboarding
    # (2026-05-05); the lambda swallows it via **kwargs so the call site
    # `find_todo_task_by_source_email(list_id, source_imessage_id, account=...)`
    # routes through this stub cleanly.
    monkeypatch.setattr(
        gc, "find_todo_task_by_source_email",
        lambda list_id, source_email_id, **kwargs: "task_dedup_match_001",
        raising=False,
    )

    posted: list[Any] = []

    def _fail_post(*args, **kwargs):
        posted.append(("POST", args, kwargs))
        raise AssertionError("create_task_in_shared_list should NOT POST on a dedup hit")

    monkeypatch.setattr(gc_mod.httpx, "post", _fail_post)

    task_id, created = gc.create_task_in_shared_list(
        "list_id_123",
        title="confirm camp",
        owner_prefix="MJ",
        deadline=None,
        source_imessage_id="im_guid_dup_777",
    )
    assert task_id == "task_dedup_match_001"
    assert created is False
    assert posted == []
