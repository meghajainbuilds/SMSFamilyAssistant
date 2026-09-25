"""Regression test: apostrophe tokenizer returned 0 matches against a
slate of 4 real Elders' Tea matches. Reverted in 53e1dc7.

User-visible failure (pre-fix): Megha texts "Mark all elders tea items
done"; Kavi replies with a hallucinated "the volunteering decision is
already showing completed" message naming UNRELATED tasks.

Post-fix expectation: matcher receives full slate, returns 4 candidates,
clarifying composer asks about Elders' Tea by name, no state-claim
hallucination.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.state import (
    load_pending_clarification,
    save_imessage_state,
)

from ._helpers import claude_with_action_responses, make_config, patch_send_paths


@pytest.mark.regression_fixture_id("2026-05-08_apostrophe_tokenizer_zero_matches")
def test_apostrophe_tokenizer_zero_matches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    regression_fixture,
) -> None:
    fx = regression_fixture
    cfg = make_config(tmp_path)
    sent: list[dict] = []
    patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    full_slate = fx["graph_state"]["open_tasks"]
    expected_candidates = fx["mock_llm_responses"]["match_target_to_open_task"][
        "candidate_task_ids"
    ]

    claude = claude_with_action_responses(fx["mock_llm_responses"])

    graph = MagicMock()
    graph.list_open_todo_tasks.return_value = full_slate
    graph.list_recent_todo_tasks.return_value = full_slate

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

    # Matcher saw the full slate (no deterministic pre-filter strip).
    claude.match_target_to_open_task.assert_called_once()
    matcher_args = claude.match_target_to_open_task.call_args
    matcher_open_tasks = (
        matcher_args.kwargs.get("open_tasks") or matcher_args.args[1]
    )
    assert len(matcher_open_tasks) == len(full_slate)
    elders_in_input = [
        t for t in matcher_open_tasks if "Oak Circle" in (t.get("title") or "")
    ]
    assert len(elders_in_input) == fx["expected_outcome"]["matcher_candidate_count"]

    # Result is action_clarifying.
    assert result is not None
    assert result["status"] == fx["expected_outcome"]["result_status"]

    # Pending clarification stores ONLY the matcher's 4 ids (Fix B), not
    # the full 30 nor a fallback slice.
    saved = load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), fx["inbound"]["sender"],
    )
    assert saved is not None
    assert {m["id"] for m in saved["proposed_matches"]} == set(expected_candidates)

    # Reply names the actual tasks; no forbidden state-claim phrases.
    assert sent
    reply = sent[-1]["text"]
    forbidden = (
        "showing completed",
        "already done",
        "already marked",
        "already completed",
    )
    for phrase in forbidden:
        assert phrase not in reply.lower(), f"forbidden phrase leaked: {phrase!r}"
