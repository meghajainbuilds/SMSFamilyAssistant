"""Regression test: disambiguation reply incorrectly classified as
fresh_intent + create_task, runtime created 'MJ MJ oak circle tea at
maple street tomorrow' duplicate. Fixed in 53e1dc7 (Fix D:
post-clarifier create_task guard).

User-visible failure (pre-fix): Megha disambiguates a clarifying
question by naming one of the proposed tasks; instead of marking that
task done, Kavi creates a brand-new duplicate task.

Post-fix expectation: when the resolver returns fresh_intent within
the clarifier window AND the next classifier wants create_task, the
runtime asks the user to disambiguate against the prior candidates
instead of creating.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers
from kavi_runtime.state import (
    load_pending_clarification,
    save_imessage_state,
    save_pending_clarification,
)

from ._helpers import claude_with_action_responses, make_config, patch_send_paths


@pytest.mark.regression_fixture_id("2026-05-08_disambiguation_creates_duplicate_task")
def test_disambiguation_creates_duplicate_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    regression_fixture,
) -> None:
    fx = regression_fixture
    cfg = make_config(tmp_path)
    sent: list[dict] = []
    patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    pending = fx["pending_clarification_state"]
    save_pending_clarification(
        Path(cfg["paths"]["imessage_state"]),
        fx["inbound"]["sender"],
        action_type=pending["action_type"],
        original_inbound=pending["original_inbound"],
        proposed_matches=pending["proposed_matches"],
        reply_sent=pending["reply_sent"],
    )

    claude = claude_with_action_responses(fx["mock_llm_responses"])

    graph = MagicMock()
    # If the guard fails, runtime would call create_todo_task. Make any
    # accidental call fail the test loudly.
    graph.create_todo_task.side_effect = AssertionError(
        "create_todo_task must NOT be called when the disambiguation guard fires"
    )

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

    assert result is not None
    assert result["status"] == fx["expected_outcome"]["result_status"]
    assert result["reason"] == fx["expected_outcome"]["result_reason"]
    graph.create_todo_task.assert_not_called()

    # Prior proposal re-saved so user's next reply still binds.
    saved_after = load_pending_clarification(
        Path(cfg["paths"]["imessage_state"]), fx["inbound"]["sender"],
    )
    assert saved_after is not None
    assert {m["id"] for m in saved_after["proposed_matches"]} == {
        m["id"] for m in pending["proposed_matches"]
    }
