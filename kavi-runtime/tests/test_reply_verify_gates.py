"""Gate set for the kavi-reply deep verify (ENDPOINT_CONTRACT.md §4).

One fires/passes pair per gate, exercised directly against
`capabilities.kavi_persona.verify_reply.run_reply_gates` (the LLM is out
of the loop; these tests cover the verifier machinery itself, the same
split as test_synthetic_verify_periodic_summary.py).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_reply_verify_gates.py -v
"""

from __future__ import annotations

from typing import Any

import pytest

from capabilities.kavi_persona.verify_reply import (
    run_reply_gates,
    validate_reply_payload,
)
from kavi_runtime.structural_checks import LENGTH_CAP_TARGET


def _gates(failures: list[dict[str, str]]) -> set[str]:
    return {f["gate"] for f in failures}


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "inbound_text": "x",
        "sender": "megha",
        "recent_outbound": [],
        "recent_inbound": [],
        "pending_questions": [],
        "pending_facts": [],
        "open_tasks": [],
        "expected_intents": [],
        "forbidden_intents": [],
    }
    base.update(overrides)
    return base


def _intent(itype: str, target_text: str = "", titles: list[str] | None = None):
    return {
        "type": itype,
        "target_text": target_text,
        "targets": [{"id": f"t{i}", "title": t} for i, t in enumerate(titles or [])],
        "confidence": "high",
    }


_SUCCESS_ROW = {"intent_type": "close_task", "target_ids": ["t0"],
                "target_titles": ["MJ X"], "simulated": True, "result": "success"}


# ---- intent_dropped ---------------------------------------------------------


def test_intent_dropped_fires_when_expected_missing() -> None:
    failures = run_reply_gates(
        output="Done.",
        parsed_intents=[_intent("close_task", titles=["MJ Anita RSVP"])],
        executed=[_SUCCESS_ROW],
        payload=_payload(expected_intents=[
            {"type": "close_task", "target_keyword": "Maple"},
        ]),
    )
    assert "intent_dropped" in _gates(failures)


def test_intent_dropped_passes_on_title_keyword_match() -> None:
    failures = run_reply_gates(
        output="Closed the Maple forms.",
        parsed_intents=[_intent("close_task", titles=["MJ Complete Maple Street camp forms"])],
        executed=[_SUCCESS_ROW],
        payload=_payload(expected_intents=[
            {"type": "close_task", "target_keyword": "maple street"},
        ]),
    )
    assert "intent_dropped" not in _gates(failures)


def test_intent_dropped_passes_on_target_text_keyword_match() -> None:
    failures = run_reply_gates(
        output="Closed it.",
        parsed_intents=[_intent("close_task", target_text="the maple street stuff",
                                titles=["MJ something unrelated-looking"])],
        executed=[_SUCCESS_ROW],
        payload=_payload(expected_intents=[
            {"type": "close_task", "target_keyword": "Maple Street"},
        ]),
    )
    assert "intent_dropped" not in _gates(failures)


def test_null_keyword_matches_any_intent_of_type() -> None:
    failures = run_reply_gates(
        output="Going quiet.",
        parsed_intents=[_intent("pause")],
        executed=[{"intent_type": "pause", "target_ids": [], "target_titles": [],
                   "simulated": True, "result": "success"}],
        payload=_payload(expected_intents=[{"type": "pause", "target_keyword": None}]),
    )
    assert "intent_dropped" not in _gates(failures)


# ---- wrong_direction_resolution ----------------------------------------------


def test_wrong_direction_fires_on_forbidden_match() -> None:
    """The tonight class: close coerced into keep."""
    failures = run_reply_gates(
        output="Noted.",
        parsed_intents=[_intent("qa_keep", titles=["MJ Decide on Anita invite"])],
        executed=[],
        payload=_payload(forbidden_intents=[
            {"type": "qa_keep", "target_keyword": None},
        ]),
    )
    assert "wrong_direction_resolution" in _gates(failures)


def test_wrong_direction_keyword_scoped() -> None:
    """Forbidden close on 'kick-off' must not fire on a Wren close."""
    failures = run_reply_gates(
        output="Closed it.",
        parsed_intents=[_intent("close_task", titles=["MJ Reply to Wren (Cedar House)"])],
        executed=[_SUCCESS_ROW],
        payload=_payload(forbidden_intents=[
            {"type": "close_task", "target_keyword": "kick-off"},
        ]),
    )
    assert "wrong_direction_resolution" not in _gates(failures)


# ---- banned_template_reply ----------------------------------------------------


@pytest.mark.parametrize("text", ["Got it.", "Got it!", "Kept: MJ Anita invite",
                                  "Dropped: MJ Boonli payment"])
def test_banned_template_fires(text: str) -> None:
    failures = run_reply_gates(
        output=text, parsed_intents=[], executed=[], payload=_payload(),
    )
    assert "banned_template_reply" in _gates(failures)


def test_banned_template_passes_on_real_prose() -> None:
    failures = run_reply_gates(
        output="Got it — the Boonli skip rule is saved.",
        parsed_intents=[], executed=[], payload=_payload(),
    )
    assert "banned_template_reply" not in _gates(failures)


# ---- unresolved_context_claim --------------------------------------------------


def test_unresolved_context_claim_fires_with_her_context_present() -> None:
    """The 'I don't have context on others from this thread' class."""
    failures = run_reply_gates(
        output="I don't have context on others from this thread.",
        parsed_intents=[], executed=[],
        payload=_payload(recent_inbound=["Close the following tasks ..."]),
    )
    assert "unresolved_context_claim" in _gates(failures)


def test_unresolved_context_claim_noop_without_recent_inbound() -> None:
    failures = run_reply_gates(
        output="I don't have context on that yet.",
        parsed_intents=[], executed=[],
        payload=_payload(recent_inbound=[]),
    )
    assert "unresolved_context_claim" not in _gates(failures)


def test_unresolved_context_claim_handles_curly_apostrophe() -> None:
    failures = run_reply_gates(
        output="I don’t have context on the others.",
        parsed_intents=[], executed=[],
        payload=_payload(recent_inbound=["earlier ask"]),
    )
    assert "unresolved_context_claim" in _gates(failures)


# ---- ungrounded_action_claim (negation-aware) -----------------------------------


def test_ungrounded_action_claim_fires_on_bare_past_tense() -> None:
    failures = run_reply_gates(
        output="Sent them over to Max earlier today.",
        parsed_intents=[], executed=[], payload=_payload(),
    )
    assert "ungrounded_action_claim" in _gates(failures)


def test_negated_claim_passes() -> None:
    """Contract example: 'I haven't sent them' passes."""
    failures = run_reply_gates(
        output="I haven't sent them to Max yet. Want me to?",
        parsed_intents=[], executed=[], payload=_payload(),
    )
    assert "ungrounded_action_claim" not in _gates(failures)


def test_question_form_is_not_a_claim() -> None:
    failures = run_reply_gates(
        output="Want me to mark it done?",
        parsed_intents=[], executed=[], payload=_payload(),
    )
    assert "ungrounded_action_claim" not in _gates(failures)


def test_claim_backed_by_simulated_success_passes() -> None:
    """In dry-run the composer is ALLOWED to narrate the simulated
    executions; that is the production path under test."""
    failures = run_reply_gates(
        output="Marked all seven done.",
        parsed_intents=[], executed=[_SUCCESS_ROW], payload=_payload(),
    )
    assert "ungrounded_action_claim" not in _gates(failures)


# ---- length_cap / prose_required -------------------------------------------------


def test_length_cap_fires_over_target() -> None:
    failures = run_reply_gates(
        output="x" * (LENGTH_CAP_TARGET + 1),
        parsed_intents=[], executed=[], payload=_payload(),
    )
    assert "length_cap" in _gates(failures)


def test_length_cap_passes_at_target() -> None:
    failures = run_reply_gates(
        output="y" * LENGTH_CAP_TARGET,
        parsed_intents=[], executed=[], payload=_payload(),
    )
    assert "length_cap" not in _gates(failures)


def test_prose_required_fires_on_list_markers() -> None:
    failures = run_reply_gates(
        output="Done:\n- Anita\n- Maple",
        parsed_intents=[], executed=[_SUCCESS_ROW], payload=_payload(),
    )
    assert "prose_required" in _gates(failures)


def test_all_gates_pass_on_clean_grounded_reply() -> None:
    failures = run_reply_gates(
        output="Closed the Anita RSVP and both Maple camp tasks.",
        parsed_intents=[
            _intent("close_task", titles=["MJ Decide on Anita invite"]),
            _intent("close_task", titles=["MJ Maple Street camp forms",
                                          "MJ Maple Street medical form"]),
        ],
        executed=[_SUCCESS_ROW],
        payload=_payload(
            recent_inbound=["close all the anita and maple tasks"],
            expected_intents=[
                {"type": "close_task", "target_keyword": "Anita"},
                {"type": "close_task", "target_keyword": "Maple Street"},
            ],
            forbidden_intents=[{"type": "qa_keep", "target_keyword": None}],
        ),
    )
    assert failures == []


# ---- request-body schema (contract §1) ----------------------------------------


def test_unknown_field_rejected() -> None:
    p = _payload()
    p["expected_intent"] = []  # typo'd expectation field
    problem = validate_reply_payload(p, expectations_required=True)
    assert problem is not None and "expected_intent" in problem


def test_missing_required_field_rejected() -> None:
    p = _payload()
    del p["recent_inbound"]
    problem = validate_reply_payload(p, expectations_required=True)
    assert problem is not None and "recent_inbound" in problem


def test_bad_sender_rejected() -> None:
    problem = validate_reply_payload(
        _payload(sender="theo"), expectations_required=True,
    )
    assert problem is not None and "sender" in problem


def test_expectations_required_for_verify_mode() -> None:
    p = _payload()
    del p["expected_intents"]
    assert validate_reply_payload(p, expectations_required=True) is not None
    # compose mode tolerates their absence
    del p["forbidden_intents"]
    assert validate_reply_payload(p, expectations_required=False) is None


def test_valid_verify_payload_accepted() -> None:
    assert validate_reply_payload(_payload(), expectations_required=True) is None
