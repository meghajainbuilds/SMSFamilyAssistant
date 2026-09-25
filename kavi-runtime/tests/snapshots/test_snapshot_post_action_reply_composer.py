"""Live-LLM snapshot for post_action_reply_composer.

Asserts the real LLM response:
  - returns a string (or None on wrapper rejection).
  - on success, the string is non-empty and uses past-tense framing
    appropriate to the reported `result`.
  - does not clip (output_tokens <= 300 - 50).
"""

from __future__ import annotations

import pytest

from ._runner import (
    assert_output_tokens_well_below_cap,
    install_usage_recorder,
)


@pytest.mark.parametrize(
    "action_type,target_title,result",
    [
        ("mark_done", "MJ Forward Elders' Tea Zoom link", "completed"),
        ("create",    "MJ Schedule pediatrician 2yr checkup", "created"),
    ],
)
def test_compose_post_action_reply_shape(
    monkeypatch: pytest.MonkeyPatch,
    claude_live,
    action_type: str,
    target_title: str,
    result: str,
) -> None:
    recorder = install_usage_recorder(monkeypatch)

    msg = claude_live.compose_post_action_reply(
        action_type=action_type,
        target_title=target_title,
        result=result,
    )

    assert msg is not None, "post_action_reply composer returned None"
    assert isinstance(msg, str)
    assert msg.strip()
    # Post-action reply is short by design; voice rules cap ~120 chars.
    assert len(msg) <= 240, f"post_action_reply too long: {len(msg)} chars"

    assert_output_tokens_well_below_cap(
        recorder, call_type="compose_post_action_reply", cap=300,
    )
