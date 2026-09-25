"""Regression test: the working two-turn flow after all six fix commits
landed. This is the happy-path fixture; the deploy verifier in
Investment 2 uses this same scenario as its smoke test.

User-visible flow:
  Turn 1 (19:06:26Z): Megha texts "Mark all elders tea items done";
                      Kavi asks "Mark all four?".
  Turn 2 (19:07:21Z): Megha texts "Yes"; Kavi PATCHes 4x and
                      confirms with a tool-grounded reply.

Asserts on observable behavior at each turn so any regression in
either step (matcher, pending state, resolver, mark_task_done loop,
post-action reply) trips a single test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.state import (
    load_pending_clarification,
    save_imessage_state,
)

from ._helpers import claude_with_action_responses, make_config, patch_send_paths


@pytest.mark.regression_fixture_id("2026-05-08_happy_path_mark_all_elders_tea")
def test_happy_path_turn1_clarifying_then_turn2_yes_executes_4(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    regression_fixture,
) -> None:
    fx = regression_fixture
    cfg = make_config(tmp_path)
    sent: list[dict] = []
    patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    sender = fx["turn1"]["inbound"]["sender"]

    # ---- Turn 1: clarifying question -----------------------------------
    open_tasks = fx["turn1"]["graph_state"]["open_tasks"]
    expected_candidates = fx["turn1"]["mock_llm_responses"][
        "match_target_to_open_task"
    ]["candidate_task_ids"]

    claude = claude_with_action_responses(fx["turn1"]["mock_llm_responses"])

    graph = MagicMock()
    graph.list_open_todo_tasks.return_value = open_tasks
    graph.list_recent_todo_tasks.return_value = open_tasks
    graph.mark_task_done.side_effect = AssertionError(
        "mark_task_done must NOT fire on turn 1 (clarifying-only)"
    )

    result1 = handlers._try_handle_action_intent(
        free_text=fx["turn1"]["inbound"]["text"],
        recent_outbound=[],
        claude=claude,
        graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"],
        config=cfg,
        sender_handle=sender,
        source_imessage_id=f"im_{fx['id']}_t1",
    )

    assert result1 is not None
    assert result1["status"] == fx["turn1"]["expected_outcome"]["result_status"]

    saved = load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), sender,
    )
    assert saved is not None
    assert {m["id"] for m in saved["proposed_matches"]} == set(expected_candidates)
    assert sent
    turn1_reply = sent[-1]["text"]
    # Reply names actual tasks (composer drew from candidates).
    assert "Elders" in turn1_reply or "Mark all four" in turn1_reply

    # ---- Turn 2: "Yes" -> execute on all 4 -----------------------------
    sent.clear()
    claude2 = MagicMock()
    claude2.resolve_pending_action_clarification.return_value = (
        fx["turn2"]["mock_llm_responses"]["resolve_pending_action_clarification"]
    )
    claude2.compose_batch_action_reply.return_value = (
        fx["turn2"]["mock_llm_responses"]["compose_batch_action_reply"]
    )
    # If we fall through to classify_action_intent or compose_conversational_reply
    # on turn 2, that's a regression: a "Yes" reply must route through the resolver.
    claude2.classify_action_intent.side_effect = AssertionError(
        "classify_action_intent must NOT run when resolver returned execute"
    )
    claude2.compose_conversational_reply.side_effect = AssertionError(
        "compose_conversational_reply must NOT run for a confirmation reply"
    )

    titles_by_id = {m["id"]: m["title"] for m in saved["proposed_matches"]}
    confirmed_ids = fx["turn2"]["mock_llm_responses"][
        "resolve_pending_action_clarification"
    ]["confirmed_match_ids"]

    graph2 = MagicMock()
    graph2.mark_task_done.side_effect = [
        (True, titles_by_id[i]) for i in confirmed_ids
    ]
    graph2.create_todo_task.side_effect = AssertionError(
        "create_todo_task must NOT fire on a 'Yes' confirmation"
    )

    result2 = handlers._try_handle_action_intent(
        free_text=fx["turn2"]["inbound"]["text"],
        recent_outbound=[],
        claude=claude2,
        graph=graph2,
        list_id=cfg["graph"]["mstodo_shared_list_id"],
        config=cfg,
        sender_handle=sender,
        source_imessage_id=f"im_{fx['id']}_t2",
    )

    assert result2 is not None
    assert result2["status"] == fx["turn2"]["expected_outcome"]["result_status"]
    assert result2["executed_count"] == fx["turn2"]["expected_outcome"][
        "executed_count"
    ]
    assert result2["failed_count"] == fx["turn2"]["expected_outcome"][
        "failed_count"
    ]
    assert graph2.mark_task_done.call_count == fx["turn2"]["expected_outcome"][
        "graph_mark_task_done_call_count"
    ]
    called_ids = [c.args[1] for c in graph2.mark_task_done.call_args_list]
    assert called_ids == fx["turn2"]["expected_outcome"][
        "graph_mark_task_done_called_with_ids"
    ]

    # Pending state cleared after execution.
    assert load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), sender,
    ) is None

    # Post-action reply was shipped (composer drew from verified tool results).
    assert sent
    final_reply = sent[-1]["text"]
    assert "Marked" in final_reply or "marked" in final_reply
