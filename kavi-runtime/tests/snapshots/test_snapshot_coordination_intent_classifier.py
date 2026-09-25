"""Live-LLM snapshot for coordination_intent_classifier.

Asserts the real LLM response:
  - returns a dict with is_coordination, addressee_name,
    addressee_handle, coordination_ask, confidence.
  - does not clip (output_tokens <= 400 - 50).
"""

from __future__ import annotations

import pytest

from ._runner import (
    assert_output_tokens_well_below_cap,
    install_usage_recorder,
)


HOUSEHOLD = [
    {
        "name": "Megha",
        "role": "requester",
        "handles": ["+15555550101"],
    },
    {
        "name": "Max",
        "role": "spouse",
        "handles": ["+15555550102"],
    },
]


@pytest.mark.parametrize(
    "free_text,expected_is_coordination",
    [
        ("Ask Max if he can pick up Rosa tomorrow morning", True),
        ("What's on my list today?", False),
    ],
)
def test_classify_coordination_intent_shape(
    monkeypatch: pytest.MonkeyPatch,
    claude_live,
    free_text: str,
    expected_is_coordination: bool,
) -> None:
    recorder = install_usage_recorder(monkeypatch)

    result = claude_live.classify_coordination_intent(
        free_text=free_text,
        requester_handle="+15555550101",
        household_members=HOUSEHOLD,
    )

    assert isinstance(result, dict)
    for field in (
        "is_coordination", "addressee_name", "addressee_handle",
        "coordination_ask", "confidence",
    ):
        assert field in result, f"missing field {field!r}: {result!r}"

    assert isinstance(result["is_coordination"], bool)
    assert result["confidence"] in {"high", "medium", "low"}

    if result["is_coordination"]:
        assert isinstance(result["addressee_name"], str)
        assert isinstance(result["coordination_ask"], str)
        assert result["coordination_ask"].strip()
    # No strict assertion on the negative case beyond shape — the
    # classifier is allowed to be uncertain about borderline inputs.

    assert_output_tokens_well_below_cap(
        recorder, call_type="classify_coordination_intent", cap=400,
    )
