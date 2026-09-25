"""Live-LLM snapshot for periodic_summary_composer.

Asserts the real LLM response:
  - returns a string (or None on wrapper rejection).
  - on success, the string is non-empty and within the 180-char hard cap.
  - does not clip (output_tokens <= 300 - 50).
"""

from __future__ import annotations

import pytest

from ._runner import (
    assert_output_tokens_well_below_cap,
    install_usage_recorder,
)


def test_compose_periodic_summary_shape(
    monkeypatch: pytest.MonkeyPatch, claude_live,
) -> None:
    recorder = install_usage_recorder(monkeypatch)

    msg = claude_live.compose_periodic_summary(
        queued_tasks=[
            {"title": "MJ Pay UW Medicine overdue balance ($630.00)", "owner": "MJ", "is_priority": True},
            {"title": "MJ Schedule pediatrician 2yr checkup", "owner": "MJ", "is_priority": False},
        ],
        pending_questions=[],
        is_rollup=False,
        time_of_day="morning",
        pending_facts=[],
    )

    assert msg is not None, "periodic_summary composer returned None"
    assert isinstance(msg, str)
    assert msg.strip()
    assert len(msg) <= 180, f"periodic_summary too long: {len(msg)} chars"

    assert_output_tokens_well_below_cap(
        recorder, call_type="compose_periodic_summary", cap=300,
    )
