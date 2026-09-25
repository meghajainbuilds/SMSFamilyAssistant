"""Live-LLM snapshot for action_clarifying_reply_composer.

Asserts the real LLM response:
  - returns a string (or None if the wrapper rejected the output for
    voice/length/forbidden-state-claim).
  - on a successful return, the string is non-empty and within the
    240-char cap.
  - does not clip (output_tokens <= 400 - 50).
"""

from __future__ import annotations

import pytest

from ._runner import (
    assert_output_tokens_well_below_cap,
    install_usage_recorder,
)


CANDIDATES = [
    {"id": "et_zoom",   "title": "MJ Forward Oak Circle Tea Zoom link to your guest", "match_reasoning": "literal Oak Circle Tea match"},
    {"id": "et_cater",  "title": "MJ Confirm Oak Circle Tea catering count by Friday", "match_reasoning": "literal Oak Circle Tea match"},
    {"id": "et_print",  "title": "MJ Print Oak Circle Tea name tags", "match_reasoning": "literal Oak Circle Tea match"},
    {"id": "et_maple", "title": "MJ Oak Circle Tea at Maple Street tomorrow 4pm RSVP", "match_reasoning": "literal Oak Circle Tea match"},
]


def test_compose_action_clarifying_reply_shape(
    monkeypatch: pytest.MonkeyPatch, claude_live,
) -> None:
    recorder = install_usage_recorder(monkeypatch)

    msg = claude_live.compose_action_clarifying_reply(
        free_text="Mark all oak circle tea items done",
        action_type="mark_done",
        target_text="oak circle tea items",
        candidates=CANDIDATES,
    )

    # The wrapper returns None on validation failure; on a clean prompt
    # like this one, we expect a real reply.
    assert msg is not None, (
        "composer returned None — likely the LLM tripped a forbidden-phrase "
        "or over-length check. Inspect skill prompt or fixture input."
    )
    assert isinstance(msg, str)
    assert msg.strip()
    assert len(msg) <= 240, f"composer returned {len(msg)}-char message; cap is 240"

    # Forbidden state-claim phrases must not appear (the 2026-05-07
    # 'showing completed' lie). The wrapper applies this check too —
    # this is belt-and-suspenders.
    forbidden = (
        "showing completed", "already done", "already marked",
        "already completed",
    )
    low = msg.lower()
    for phrase in forbidden:
        assert phrase not in low, f"forbidden phrase leaked: {phrase!r}"

    assert_output_tokens_well_below_cap(
        recorder, call_type="compose_action_clarifying_reply", cap=400,
    )
