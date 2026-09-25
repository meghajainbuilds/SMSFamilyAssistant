"""Live-LLM snapshot for action_target_matcher.

Asserts the real LLM response:
  - returns a dict with match_id, candidate_task_ids, confidence,
    reasoning.
  - candidate_task_ids only contain ids from the input slate (no
    invented ids — the wrapper strips invented ones, this test
    confirms the wrapper saw legitimate ids to begin with).
  - does not clip (output_tokens <= 1500 - 50).
"""

from __future__ import annotations

import pytest

from ._runner import (
    assert_output_tokens_well_below_cap,
    install_usage_recorder,
)


def _slate() -> list[dict]:
    return [
        {"id": "et_zoom",  "title": "MJ Forward Oak Circle Tea Zoom link to your guest", "status": "notStarted"},
        {"id": "et_cater", "title": "MJ Confirm Oak Circle Tea catering count by Friday", "status": "notStarted"},
        {"id": "et_print", "title": "MJ Print Oak Circle Tea name tags", "status": "notStarted"},
        {"id": "et_maple","title": "MJ Oak Circle Tea at Maple Street tomorrow 4pm RSVP", "status": "notStarted"},
        {"id": "t_uw",     "title": "MJ Pay Northgate Medical overdue balance ($630.00)", "status": "notStarted"},
        {"id": "t_swim",   "title": "MJ Schedule Max swim lesson", "status": "notStarted"},
    ]


def test_match_target_to_open_task_shape(
    monkeypatch: pytest.MonkeyPatch, claude_live,
) -> None:
    recorder = install_usage_recorder(monkeypatch)
    slate = _slate()
    valid_ids = {t["id"] for t in slate}

    result = claude_live.match_target_to_open_task(
        target_text="oak circle tea items",
        open_tasks=slate,
    )

    # Schema.
    assert isinstance(result, dict)
    for field in ("match_id", "candidate_task_ids", "confidence", "reasoning"):
        assert field in result, f"missing field {field!r}: {result!r}"

    # Constraints.
    assert result["match_id"] is None or isinstance(result["match_id"], str)
    assert result["confidence"] in {"high", "medium", "low"}
    assert isinstance(result["candidate_task_ids"], list)

    # Every candidate id must be in the input slate (no fabrications).
    for cid in result["candidate_task_ids"]:
        assert cid in valid_ids, f"matcher returned invented id: {cid!r}"

    # Output tokens clip-safety. The 2026-05-08 bug: max_tokens=400 was
    # too small for fenced output + 4 long ids. Cap is now 1500.
    assert_output_tokens_well_below_cap(
        recorder, call_type="compose_action_target_match", cap=1500,
    )
