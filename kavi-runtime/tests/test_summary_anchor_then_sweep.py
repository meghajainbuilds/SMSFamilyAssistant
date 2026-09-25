"""Fix 2 (2026-05-07): periodic_summary anchor → action layer's anchor-then-sweep
flow. Closes the user-visible regression where Megha replied "Yes on elders
tea" to a 9 PM summary that anchored on ONE specific Elders' Tea task, and
got back a flat clarification listing every Elders' Tea task with no count
grounding.

Tests cover:
  - State helpers (save / load / clear / TTL).
  - periodic_summary picks the anchor deterministically (priority queued
    first, then any queued, then first pending Q&A).
  - Action layer's ambiguous-match branch fires anchor-then-sweep when an
    anchor exists for the sender and is among open tasks.
  - The anchor PATCH runs and the LLM-composed reply names exactly the
    sibling count from the runtime's verified list, not a hallucinated
    count from the LLM's view of open_tasks.
  - Sweep siblings get persisted as pending_action_clarification so a
    follow-up "Yes" / "Yea" resolves through Fix 1.
  - When no anchor in state, behavior unchanged (flat clarification).
  - Anchor unrelated to the matched topic → fall-through to flat clarifier.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_summary_anchor_then_sweep.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.state import (
    SUMMARY_ANCHOR_TTL_SECONDS,
    clear_summary_anchor,
    load_pending_clarification,
    load_summary_anchor,
    save_imessage_state,
    save_summary_anchor,
)


# ---- shared fixtures ------------------------------------------------------


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "graph": {"mstodo_shared_list_id": "AQMkADAwTEST=="},
        "action_layer": {"dry_run": False},
        "paths": {
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "eval_persona_action_intent_jsonl": str(tmp_path / "action_intent.jsonl"),
        },
    }


def _patch_send_paths(monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]) -> None:
    def _send_plain(config: dict, text: str, *, kind: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": None})
        return {"verified": True, "fallback_used": False}

    def _send_with_context(config: dict, text: str, *, kind: str, context: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": context})
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send_plain)
    monkeypatch.setattr(handlers, "_send_imessage_with_fallback_and_context", _send_with_context)
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)


def _elders_tea_open_tasks() -> list[dict[str, Any]]:
    """Four Elders' Tea tasks open in MS To Do, mirroring the 2026-05-07
    failure exchange. The 'Zoom link' anchor is the one the periodic_summary
    foregrounded; the other three are siblings the sweep should offer."""
    return [
        {"id": "t_zoom", "title": "MJ Confirm guest got Zoom link for Elders' Tea",
         "status": "notStarted"},
        {"id": "t_theo", "title": "MJ Pack Theo's fancy clothes for Elders' Tea",
         "status": "notStarted"},
        {"id": "t_baked", "title": "MJ Order gluten-free baked goods for Elders' Tea",
         "status": "notStarted"},
        {"id": "t_forward", "title": "MJ Forward Elders' Tea Zoom link to attendees",
         "status": "notStarted"},
        # Unrelated tasks in the open list, must NOT appear in sweep.
        {"id": "t_unrelated_1", "title": "MJ Pay UW Medicine overdue balance",
         "status": "notStarted"},
        {"id": "t_unrelated_2", "title": "MJ Bayview summer registration",
         "status": "notStarted"},
    ]


# ---- state helpers --------------------------------------------------------


def test_save_load_summary_anchor_roundtrip(tmp_path: Path) -> None:
    state_path = tmp_path / "imessage-state.json"
    save_imessage_state(state_path, {})
    save_summary_anchor(
        state_path, "+15555550101",
        anchor_task_id="t_zoom",
        anchor_task_title="MJ Confirm guest got Zoom link for Elders' Tea",
        summary_message="Quick: confirm your guest got the Zoom link?",
    )
    loaded = load_summary_anchor(state_path, "+15555550101")
    assert loaded is not None
    assert loaded["anchor_task_id"] == "t_zoom"
    assert "Zoom link" in loaded["anchor_task_title"]


def test_summary_anchor_expires_after_ttl(tmp_path: Path) -> None:
    state_path = tmp_path / "imessage-state.json"
    save_imessage_state(state_path, {})
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=SUMMARY_ANCHOR_TTL_SECONDS + 60)
    save_summary_anchor(
        state_path, "+15555550101",
        anchor_task_id="t_zoom", anchor_task_title="x",
        summary_message="x", now=long_ago,
    )
    assert load_summary_anchor(state_path, "+15555550101") is None


def test_clear_summary_anchor_removes_entry(tmp_path: Path) -> None:
    state_path = tmp_path / "imessage-state.json"
    save_imessage_state(state_path, {})
    save_summary_anchor(state_path, "+15555550101",
                        anchor_task_id="t_zoom", anchor_task_title="x",
                        summary_message="x")
    assert load_summary_anchor(state_path, "+15555550101") is not None
    clear_summary_anchor(state_path, "+15555550101")
    assert load_summary_anchor(state_path, "+15555550101") is None


# ---- _pick_summary_anchor heuristic --------------------------------------


def test_pick_anchor_prefers_priority_queued() -> None:
    queued = [
        {"task_id": "t_low", "title": "low priority", "is_priority": False},
        {"task_id": "t_high", "title": "high priority", "is_priority": True},
        {"task_id": "t_low2", "title": "another low", "is_priority": False},
    ]
    pending: list[dict[str, Any]] = []
    out = handlers._pick_summary_anchor(queued, pending)
    assert out is not None
    assert out["task_id"] == "t_high"


def test_pick_anchor_falls_back_to_first_queued() -> None:
    queued = [
        {"task_id": "t_first", "title": "first", "is_priority": False},
        {"task_id": "t_second", "title": "second", "is_priority": False},
    ]
    out = handlers._pick_summary_anchor(queued, [])
    assert out["task_id"] == "t_first"


def test_pick_anchor_falls_back_to_first_pending_when_no_queued() -> None:
    pending = [
        {"task_id": "tq_pending", "task_title_rendered": "MJ pending one"},
    ]
    out = handlers._pick_summary_anchor([], pending)
    assert out["task_id"] == "tq_pending"
    assert "pending one" in out["title"]


def test_pick_anchor_returns_none_when_empty() -> None:
    assert handlers._pick_summary_anchor([], []) is None


# ---- _share_topic_keyword -------------------------------------------------


def test_share_topic_keyword_matches_elders_tea() -> None:
    a = "MJ Confirm guest got Zoom link for Elders' Tea"
    b = "MJ Pack Theo's fancy clothes for Elders' Tea"
    assert handlers._share_topic_keyword(a, b) is True


def test_share_topic_keyword_unrelated_fails() -> None:
    a = "MJ Confirm guest got Zoom link for Elders' Tea"
    b = "MJ Pay UW Medicine overdue balance"
    assert handlers._share_topic_keyword(a, b) is False


def test_share_topic_keyword_handles_owner_prefix() -> None:
    a = "[?] MJ Bayview registration"
    b = "MJ Bayview summer tennis"
    assert handlers._share_topic_keyword(a, b) is True


# ---- action-layer integration --------------------------------------------


def test_anchor_then_sweep_executes_anchor_and_offers_siblings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The 2026-05-07 9 PM regression: 9 PM summary mentions ONE Elders'
    Tea task ('confirm Zoom link'). Megha replies 'Yes on elders tea'.
    Expected: Kavi marks the Zoom-link task done immediately, then asks
    'I see 3 more like it, those too?' with the count grounded in what
    actually got marked, not in the LLM's view of open_tasks."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    state_path = Path(cfg["paths"]["imessage_state"])
    save_imessage_state(state_path, {})
    save_summary_anchor(
        state_path, "+15555550101",
        anchor_task_id="t_zoom",
        anchor_task_title="MJ Confirm guest got Zoom link for Elders' Tea",
        summary_message="Quick: confirm your guest got the Zoom link?",
    )

    open_tasks = _elders_tea_open_tasks()

    claude = MagicMock()
    # Matcher returns the anchor task as a medium-confidence match.
    claude.classify_action_intent.return_value = {
        "has_action": True, "action_type": "mark_done",
        "target_text": "Elders' Tea", "confidence": "high",
    }
    claude.match_target_to_open_task.return_value = {
        "match_id": "t_zoom", "confidence": "medium",
        "reasoning": "matches one of multiple Elders' Tea tasks",
    }
    # The composer should be called with needs_clarification=True + sweep
    # siblings populated by the runtime.
    claude.compose_batch_action_reply.return_value = (
        "Marked the Zoom link confirm done. 3 more Elders' Tea tasks "
        "open — those too?"
    )
    claude.compose_action_clarifying_reply.side_effect = AssertionError(
        "anchor-then-sweep must not fall through to the flat clarifier"
    )
    claude.compose_conversational_reply.side_effect = AssertionError(
        "compose_conversational_reply must NOT be called from action layer"
    )

    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks.
    graph.list_open_todo_tasks.return_value = open_tasks
    graph.list_recent_todo_tasks.return_value = open_tasks
    graph.mark_task_done.return_value = (
        True, "MJ Confirm guest got Zoom link for Elders' Tea",
    )

    result = handlers._try_handle_action_intent(
        free_text="Yes on elders tea",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_anchor",
    )

    assert result is not None
    assert result["status"] == "anchor_then_sweep"
    assert result["anchor_executed"] is True
    # Exactly 3 siblings (4 Elders' Tea tasks open total, anchor is one of
    # them). Unrelated tasks (UW, Bayview) must NOT be in the sweep.
    assert result["sweep_count"] == 3

    # Anchor PATCH ran exactly once (sweep is offered, not executed yet).
    assert graph.mark_task_done.call_count == 1
    assert graph.mark_task_done.call_args.args[1] == "t_zoom"

    # Composer was given the verified results and the runtime-grounded
    # sibling list. The proposed_matches kwarg should be 3 items, all
    # Elders'-Tea tasks, none unrelated.
    claude.compose_batch_action_reply.assert_called_once()
    kwargs = claude.compose_batch_action_reply.call_args.kwargs
    assert kwargs["needs_clarification"] is True
    assert len(kwargs["actions_executed"]) == 1
    assert kwargs["actions_executed"][0]["result"] == "success"
    assert len(kwargs["proposed_matches"]) == 3
    sweep_titles = " ".join(m["title"] for m in kwargs["proposed_matches"])
    assert "Elders' Tea" in sweep_titles
    assert "UW" not in sweep_titles
    assert "Bayview" not in sweep_titles

    # Sweep proposal got persisted so a follow-up "Yes" resolves through
    # Fix 1's pending-clarification path.
    pending = load_pending_clarification(state_path, "+15555550101")
    assert pending is not None
    assert {m["id"] for m in pending["proposed_matches"]} == {
        "t_theo", "t_baked", "t_forward",
    }

    # Anchor cleared after consumption.
    assert load_summary_anchor(state_path, "+15555550101") is None

    # Outbound carries tool_grounded=True (anchor PATCH succeeded).
    assert sent[-1]["context"]["tool_grounded"] is True


def test_no_anchor_falls_through_to_flat_clarifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """No anchor in state → existing flat-clarifier behavior. Anchor-then-
    sweep is a no-op when there is no recent summary anchor."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": True, "action_type": "mark_done",
        "target_text": "Elders' Tea", "confidence": "high",
    }
    claude.match_target_to_open_task.return_value = {
        "match_id": "t_zoom", "confidence": "medium", "reasoning": "ambig",
    }
    claude.compose_action_clarifying_reply.return_value = (
        "Four Elders' Tea tasks open — which one?"
    )
    # batch composer must NOT fire.
    claude.compose_batch_action_reply.side_effect = AssertionError(
        "anchor-then-sweep must not fire when no anchor exists"
    )

    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks.
    _et_slate = _elders_tea_open_tasks()
    graph.list_open_todo_tasks.return_value = _et_slate
    graph.list_recent_todo_tasks.return_value = _et_slate
    graph.mark_task_done.side_effect = AssertionError(
        "no PATCH should run when there's no anchor"
    )

    result = handlers._try_handle_action_intent(
        free_text="Yes on elders tea",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_no_anchor",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"
    # The flat-clarifier reply shipped, not an anchor-then-sweep reply.
    assert sent and "Four" in sent[-1]["text"]


def test_anchor_unrelated_to_target_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Anchor exists but is unrelated to the topic Megha confirmed
    ('Bayview' vs anchor about Elders' Tea). The flat clarifier fires;
    no PATCH runs against the unrelated anchor."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    state_path = Path(cfg["paths"]["imessage_state"])
    save_imessage_state(state_path, {})
    save_summary_anchor(
        state_path, "+15555550101",
        anchor_task_id="t_zoom",
        anchor_task_title="MJ Confirm guest got Zoom link for Elders' Tea",
        summary_message="x",
    )

    open_tasks = _elders_tea_open_tasks()

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": True, "action_type": "mark_done",
        "target_text": "Bayview", "confidence": "high",
    }
    claude.match_target_to_open_task.return_value = {
        "match_id": "t_unrelated_2", "confidence": "medium",
        "reasoning": "ambig",
    }
    claude.compose_action_clarifying_reply.return_value = "Bayview — which?"

    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks.
    graph.list_open_todo_tasks.return_value = open_tasks
    graph.list_recent_todo_tasks.return_value = open_tasks
    graph.mark_task_done.side_effect = AssertionError(
        "no PATCH when anchor is unrelated to target"
    )

    result = handlers._try_handle_action_intent(
        free_text="Yes for Bayview",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_unrelated",
    )

    assert result["status"] == "action_clarifying"
    # Anchor still persists — wasn't consumed because it was unrelated.
    assert load_summary_anchor(state_path, "+15555550101") is not None


def test_anchor_already_completed_clears_and_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Anchor in state but the task was already completed (Megha closed it
    elsewhere). Anchor-then-sweep clears the anchor and falls through; no
    PATCH runs."""
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)

    state_path = Path(cfg["paths"]["imessage_state"])
    save_imessage_state(state_path, {})
    save_summary_anchor(
        state_path, "+15555550101",
        anchor_task_id="t_zoom",
        anchor_task_title="MJ Confirm guest got Zoom link for Elders' Tea",
        summary_message="x",
    )

    # Anchor task is now status=completed.
    open_tasks = _elders_tea_open_tasks()
    open_tasks[0]["status"] = "completed"

    claude = MagicMock()
    claude.classify_action_intent.return_value = {
        "has_action": True, "action_type": "mark_done",
        "target_text": "Elders' Tea", "confidence": "high",
    }
    claude.match_target_to_open_task.return_value = {
        "match_id": "t_theo", "confidence": "medium", "reasoning": "ambig",
    }
    claude.compose_action_clarifying_reply.return_value = (
        "3 Elders' Tea tasks — which?"
    )

    graph = MagicMock()
    # 2026-05-08 fix: action layer fetches via list_open_todo_tasks.
    graph.list_open_todo_tasks.return_value = open_tasks
    graph.list_recent_todo_tasks.return_value = open_tasks
    graph.mark_task_done.side_effect = AssertionError(
        "no PATCH against an already-completed anchor"
    )

    result = handlers._try_handle_action_intent(
        free_text="Yes on elders tea",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101", source_imessage_id="im_completed_anchor",
    )

    assert result["status"] == "action_clarifying"
    # Anchor cleared.
    assert load_summary_anchor(state_path, "+15555550101") is None
