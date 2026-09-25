"""Live-LLM snapshot for correction_classifier.

Asserts the real LLM response:
  - returns one of two valid shapes (correction or not_correction).
  - on a correction, every required correction field is present.
  - does not clip (output_tokens <= 300 - 50).
"""

from __future__ import annotations

import pytest

from ._runner import (
    assert_output_tokens_well_below_cap,
    install_usage_recorder,
)


@pytest.mark.parametrize(
    "free_text,expected_status",
    [
        ("Hey, just saying hi", "not_correction"),
        ("That Boonli email shouldn't have become a task", "correction"),
    ],
)
def test_classify_correction_shape(
    monkeypatch: pytest.MonkeyPatch,
    claude_live,
    free_text: str,
    expected_status: str,
) -> None:
    recorder = install_usage_recorder(monkeypatch)

    result = claude_live.classify_correction(free_text=free_text)

    assert isinstance(result, dict)
    assert "status" in result
    assert result["status"] in {"correction", "not_correction"}

    if result["status"] == "correction":
        assert "correction" in result
        c = result["correction"]
        assert isinstance(c, dict)
        # Type is required and from the documented set.
        assert c.get("type") in {
            "missed", "false_positive", "wrong_owner",
            "wrong_title", "wrong_source_tag", "wrong_confidence",
        }
        # target + reason required (non-empty strings).
        assert isinstance(c.get("target"), str) and c["target"].strip()
        assert isinstance(c.get("reason"), str) and c["reason"].strip()
    else:
        assert "reason" in result

    # Output tokens clip-safety.
    assert_output_tokens_well_below_cap(
        recorder, call_type="classify_correction", cap=300,
    )
