"""Regression test: recency-only top=30 fetch dropped older open tasks
on busy days; matcher returned 0 from an incomplete slate. Fixed in
cef1817 (server-side $filter on status, $top=100 via list_open_todo_tasks).

User-visible failure (pre-fix): on a day with many recent completions,
"Mark all elders tea pending tasks done" returned 'I couldn't find any
Elders' Tea tasks' even though 4 open ones existed in the list.

Post-fix expectation: the action layer fetches via list_open_todo_tasks
which uses a server-side $filter. Even when 30 newer tasks have been
completed, the open Elders' Tea tasks come back and reach the matcher.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.state import save_imessage_state

from ._helpers import claude_with_action_responses, make_config, patch_send_paths


@pytest.mark.regression_fixture_id("2026-05-08_30_task_cap_misses_open_tasks")
def test_30_task_cap_misses_open_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    regression_fixture,
) -> None:
    fx = regression_fixture
    cfg = make_config(tmp_path)
    sent: list[dict] = []
    patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    open_via_filter = fx["graph_state"]["open_tasks_via_filter"]
    recent_top_30 = fx["graph_state"]["recent_top_30_status_agnostic"]
    expected_candidates = fx["mock_llm_responses"]["match_target_to_open_task"][
        "candidate_task_ids"
    ]

    claude = claude_with_action_responses(fx["mock_llm_responses"])

    graph = MagicMock()
    # The post-fix path calls list_open_todo_tasks (server-side filter):
    # only the 4 open Elders' Tea tasks come back.
    graph.list_open_todo_tasks.return_value = open_via_filter
    # Pre-fix path used list_recent_todo_tasks(top=30): all 30 completed
    # tasks come back, the 4 open Elders' Tea ones are absent. We wire
    # this so a regression to the old code path fails the assertion.
    graph.list_recent_todo_tasks.return_value = recent_top_30

    result = handlers._try_handle_action_intent(
        free_text=fx["inbound"]["text"],
        recent_outbound=[],
        claude=claude,
        graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"],
        config=cfg,
        sender_handle=fx["inbound"]["sender"],
        source_imessage_id=f"im_{fx['id']}",
    )

    # The matcher MUST have been invoked. If the action layer regressed
    # to list_recent_todo_tasks, it would still call the matcher with the
    # 30 completed tasks; we assert the slate the matcher saw contained
    # the open Elders' Tea ids (proof we're reading from the filter path).
    claude.match_target_to_open_task.assert_called_once()
    matcher_args = claude.match_target_to_open_task.call_args
    matcher_open_tasks = (
        matcher_args.kwargs.get("open_tasks") or matcher_args.args[1]
    )
    matcher_ids = {t.get("id") for t in matcher_open_tasks}
    assert set(expected_candidates).issubset(matcher_ids), (
        f"matcher slate missed expected open Elders' Tea ids; got {matcher_ids}"
    )

    # End-to-end: matcher candidate count is 4 (the four open Elders' Tea ids).
    assert result is not None
    assert result["status"] == fx["expected_outcome"]["result_status"]
