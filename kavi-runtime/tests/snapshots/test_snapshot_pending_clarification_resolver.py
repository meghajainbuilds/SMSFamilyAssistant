"""Live-LLM snapshot for pending_clarification_resolver.

Asserts the real LLM response:
  - returns a dict with resolution, confirmed_match_ids, reasoning.
  - resolution is one of {execute, ignore, fresh_intent}.
  - confirmed_match_ids only contains ids present in
    pending.proposed_matches[*].id (no fabrications).
  - does not clip (output_tokens <= 600 - 50).
"""

from __future__ import annotations

import pytest

from ._runner import (
    assert_output_tokens_well_below_cap,
    install_usage_recorder,
)


def _pending() -> dict:
    return {
        "action_type": "mark_done",
        "original_inbound": "Mark all oak circle tea items done",
        "proposed_matches": [
            {"id": "et_zoom",   "title": "MJ Forward Oak Circle Tea Zoom link to your guest"},
            {"id": "et_cater",  "title": "MJ Confirm Oak Circle Tea catering count by Friday"},
            {"id": "et_print",  "title": "MJ Print Oak Circle Tea name tags"},
            {"id": "et_maple", "title": "MJ Oak Circle Tea at Maple Street tomorrow 4pm RSVP"},
        ],
        "reply_sent": "Found 4 Oak Circle Tea tasks. Mark all four?",
    }


@pytest.mark.parametrize(
    "new_inbound_text",
    [
        "Yes",
        "Ignore that, never mind",
        "Just the maple street one",
    ],
)
def test_resolve_pending_action_clarification_shape(
    monkeypatch: pytest.MonkeyPatch,
    claude_live,
    new_inbound_text: str,
) -> None:
    recorder = install_usage_recorder(monkeypatch)
    pending = _pending()
    valid_ids = {m["id"] for m in pending["proposed_matches"]}

    result = claude_live.resolve_pending_action_clarification(
        new_inbound_text=new_inbound_text,
        pending=pending,
    )

    assert isinstance(result, dict)
    for field in ("resolution", "confirmed_match_ids", "reasoning"):
        assert field in result, f"missing field {field!r}: {result!r}"

    assert result["resolution"] in {"execute", "ignore", "fresh_intent"}
    assert isinstance(result["confirmed_match_ids"], list)
    for cid in result["confirmed_match_ids"]:
        assert cid in valid_ids, f"resolver returned invented id: {cid!r}"

    assert_output_tokens_well_below_cap(
        recorder, call_type="resolve_pending_action_clarification", cap=600,
    )
