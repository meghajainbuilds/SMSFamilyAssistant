"""Live-LLM snapshot for action_intent_classifier.

Asserts the real LLM response:
  - is parseable as expected by the wrapper (returns a dict).
  - contains every documented field (has_action, action_type,
    target_text, confidence).
  - does not clip (output_tokens <= max_tokens - 50).
"""

from __future__ import annotations

import pytest

from ._runner import (
    assert_output_tokens_well_below_cap,
    install_usage_recorder,
)


@pytest.mark.parametrize(
    "free_text,expected_has_action,expected_action_type",
    [
        ("Mark the Oak Circle volunteering task as done", True, "mark_done"),
        ("Add a task to call the dentist tomorrow", True, "create"),
        ("Thanks!", False, None),
    ],
)
def test_action_intent_classifier_shape(
    monkeypatch: pytest.MonkeyPatch,
    claude_live,
    free_text: str,
    expected_has_action: bool,
    expected_action_type: str | None,
) -> None:
    recorder = install_usage_recorder(monkeypatch)
    result = claude_live.classify_action_intent(free_text=free_text)

    # Schema: every documented field present.
    assert isinstance(result, dict), f"not a dict: {result!r}"
    for field in ("has_action", "action_type", "target_text", "confidence"):
        assert field in result, f"missing field {field!r} in {result!r}"

    # Type constraints.
    assert isinstance(result["has_action"], bool)
    assert result["confidence"] in {"high", "medium", "low"}
    if expected_has_action:
        assert result["has_action"] is True
        if expected_action_type is not None:
            assert result["action_type"] == expected_action_type, (
                f"expected action_type={expected_action_type}, got {result['action_type']}"
            )

    # Output tokens clip-safety.
    assert_output_tokens_well_below_cap(
        recorder, call_type="classify_action_intent", cap=256,
    )
