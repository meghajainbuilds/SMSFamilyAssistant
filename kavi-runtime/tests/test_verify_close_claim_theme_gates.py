"""Tests for the two selection-behavior verify gates added 2026-06-10
(second pair of the day):

  * close_claim — when the synthetic payload carries close suggestions,
    the composed output must use suggestion language only; any
    completion-claim phrasing ("closed it", "marked done", "completed
    it") fails. Not armed without close suggestions in the payload.
  * theme_unsupported — payload theme present → the composed morning
    digest must reference the theme label's keywords; payload theme
    absent → the output must not use a theme-introducing shape. No-op
    for the 9 PM rollup.

Same stub pattern as tests/test_verify_owner_leak_due_soon_gates.py: the
LLM composer is replaced so these assert the GATE machinery, not the
model's compliance. Both gates must fire on violating synthetic output
and pass on clean output.

Also locks: the new payload fields (`close_suggestions`, `theme`) are
threaded into the composer call and echoed in input_payload (both the
verify route and the raw compose/replay route), and every worked example
in the periodic_summary skill passes the close_claim detector.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_verify_close_claim_theme_gates.py -v
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

from kavi_runtime import synthetic_compose
from capabilities.kavi_persona.verify import (
    SELECTION_GATES,
    _detect_close_completion_claims,
    _detect_theme_unsupported,
)
from capabilities.kavi_persona import verify as _verify_module


def _cfg() -> dict:
    return {
        "claude": {"model": "claude-sonnet-4-6", "api_key": "fake"},
        "model_routing": {},
    }


class _StubClaudeClient:
    _output: str | None = None
    _seen_kwargs: dict | None = None

    def __init__(self, config: dict) -> None:
        self._config = config

    def compose_periodic_summary(self, **kwargs) -> str | None:
        type(self)._seen_kwargs = kwargs
        return type(self)._output


def _stub_compose(output: str | None):
    _StubClaudeClient._output = output
    _StubClaudeClient._seen_kwargs = None
    return patch(
        "capabilities.kavi_persona.verify.ClaudeClient",
        new=_StubClaudeClient,
    )


_CLOSE_PAYLOAD = [
    {"title": "MJ Pay Boonli invoice",
     "reason": "your reply says it was paid"},
]

_THEME_PAYLOAD = {
    "label": "Summer camp planning",
    "supporting_task_titles": [
        "MJ Register for Cascade summer camp",
        "MJ Pay camp deposit",
        "MJ Order swim gear for camp",
    ],
}


# ---- registration ------------------------------------------------------------


def test_both_gates_registered_in_selection_gates() -> None:
    assert "close_claim" in SELECTION_GATES
    assert "theme_unsupported" in SELECTION_GATES


# ---- close_claim ----------------------------------------------------------------


def test_close_claim_fires_on_first_person_completion_claim() -> None:
    with _stub_compose("Boonli looked finished so I closed it for you."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
                "close_suggestions": _CLOSE_PAYLOAD,
            },
        )
    assert result["verdict"] == "FAIL"
    claims = [f for f in result["failures"] if f["gate"] == "close_claim"]
    assert claims, f"expected close_claim failure, got {result['failures']}"


def test_close_claim_fires_on_marked_done_claim() -> None:
    with _stub_compose("Boonli invoice marked done already."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
                "close_suggestions": _CLOSE_PAYLOAD,
            },
        )
    assert any(f["gate"] == "close_claim" for f in result["failures"])


def test_close_claim_passes_on_suggestion_language() -> None:
    with _stub_compose("Looks like you already paid Boonli, want to close it?"):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
                "close_suggestions": _CLOSE_PAYLOAD,
            },
        )
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_close_claim_not_armed_without_close_suggestions() -> None:
    """Without suggestions in the payload, completion wording is governed
    by the outbound G-A1 gate, not this replay gate."""
    with _stub_compose("Marked the dentist booking done after your reply."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
            },
        )
    assert not any(f["gate"] == "close_claim" for f in result["failures"])


# ---- theme_unsupported -----------------------------------------------------------


def test_theme_unsupported_fires_when_given_theme_never_referenced() -> None:
    with _stub_compose("Quiet morning, nothing new queued."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "megha",
                "theme": _THEME_PAYLOAD,
            },
        )
    assert result["verdict"] == "FAIL"
    fails = [f for f in result["failures"] if f["gate"] == "theme_unsupported"]
    assert fails, f"expected theme_unsupported, got {result['failures']}"
    assert "Summer camp planning" in fails[0]["detail"]


def test_theme_unsupported_passes_when_theme_opens_the_digest() -> None:
    with _stub_compose(
        "Summer camp planning is your big open thread today, 3 tasks in flight."
    ):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "megha",
                "theme": _THEME_PAYLOAD,
            },
        )
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_theme_unsupported_fires_on_invented_theme() -> None:
    with _stub_compose("Errand wrangling is the big open thread today."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "megha",
            },
        )
    assert result["verdict"] == "FAIL"
    assert any(f["gate"] == "theme_unsupported" for f in result["failures"])


def test_theme_unsupported_is_noop_for_rollup() -> None:
    with _stub_compose("3 added today, 1 done, 0 over a week."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
                "theme": _THEME_PAYLOAD,
            },
        )
    assert not any(f["gate"] == "theme_unsupported" for f in result["failures"])


def test_gates_noop_when_composer_returns_none() -> None:
    with _stub_compose(None):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "megha",
                "close_suggestions": _CLOSE_PAYLOAD,
                "theme": _THEME_PAYLOAD,
            },
        )
    assert result["output"] is None
    assert not any(
        f["gate"] in {"close_claim", "theme_unsupported"}
        for f in result["failures"]
    )


# ---- payload threading + evidence echo ---------------------------------------------


def test_new_fields_threaded_into_composer_and_echoed() -> None:
    with _stub_compose(
        "Summer camp planning is your big open thread today, 3 tasks in flight."
    ):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "megha",
                "close_suggestions": _CLOSE_PAYLOAD,
                "theme": _THEME_PAYLOAD,
            },
        )
    kwargs = _StubClaudeClient._seen_kwargs
    assert kwargs is not None
    assert kwargs["close_suggestions"] == _CLOSE_PAYLOAD
    # The clusterer's raw shape normalizes to the composer's
    # {label, task_count} shape (task_count = validated support size).
    assert kwargs["theme"] == {"label": "Summer camp planning", "task_count": 3}
    assert result["input_payload"]["close_suggestions"] == _CLOSE_PAYLOAD
    assert result["input_payload"]["theme"] == {
        "label": "Summer camp planning", "task_count": 3,
    }


def test_replay_route_threads_and_echoes_new_fields_too() -> None:
    with _stub_compose("Looks like you already paid Boonli, want to close it?"):
        result = synthetic_compose.replay_periodic_summary(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
                "close_suggestions": _CLOSE_PAYLOAD,
                "theme": {"label": "Summer camp planning", "task_count": 3},
            },
        )
    kwargs = _StubClaudeClient._seen_kwargs
    assert kwargs is not None
    assert kwargs["close_suggestions"] == _CLOSE_PAYLOAD
    assert kwargs["theme"] == {"label": "Summer camp planning", "task_count": 3}
    assert result["input_payload"]["close_suggestions"] == _CLOSE_PAYLOAD
    assert result["input_payload"]["theme"] == {
        "label": "Summer camp planning", "task_count": 3,
    }


def test_owner_leak_scans_close_suggestion_titles() -> None:
    """A close suggestion for the OTHER person's task in recipient R's
    payload is an owner leak when the output names it."""
    with _stub_compose("Looks like the cleaner cash payment is handled, close it?"):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
                "close_suggestions": [
                    {"title": "MM Cleaner cash payment", "reason": "reply says left it"},
                ],
            },
        )
    assert any(f["gate"] == "owner_leak" for f in result["failures"])


# ---- skill examples must pass the close_claim detector -----------------------------


def _extract_message_examples(skill_text: str) -> list[str]:
    """Local copy of the extractor in
    tests/test_skill_examples_pass_production_gates.py (kept small on
    purpose; that test owns the canonical structural-gate sweep)."""
    out: list[str] = []
    for m in re.finditer(r"^>\s*(\{.*\})\s*$", skill_text, flags=re.MULTILINE):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        msg = parsed.get("message")
        if isinstance(msg, str):
            out.append(msg)
    return out


def test_periodic_summary_skill_examples_pass_close_claim_detector() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1] / "skills"
        / "periodic_summary_composer.md"
    )
    examples = _extract_message_examples(skill_path.read_text())
    assert examples, "periodic_summary skill should carry worked examples"
    for i, msg in enumerate(examples):
        failures = _detect_close_completion_claims(msg)
        assert failures == [], (
            f"periodic_summary_composer.md example {i + 1} ({msg!r}) would "
            f"FAIL the close_claim gate — a skill must not teach the "
            f"completion-claim shape. Failures: {failures}"
        )


# ---- detector unit coverage ---------------------------------------------------------


def test_close_claim_detector_catches_known_phrasings() -> None:
    bad = [
        "I closed it after your reply.",
        "I've completed the Boonli task.",
        "Closed it for you tonight.",
        "Boonli is marked as done.",
        "I already marked it done.",
        "Completed that one for you.",
    ]
    for text in bad:
        assert _detect_close_completion_claims(text), f"missed: {text!r}"


def test_close_claim_detector_allows_suggestion_language() -> None:
    good = [
        "Looks like you already paid Boonli, want to close it?",
        "Boonli and the trip slip look handled. Close them?",
        "Want me to mark it done?",
        "2 added today, 1 done, 0 over a week.",
        "1 more might be closable.",
    ]
    for text in good:
        assert _detect_close_completion_claims(text) == [], f"false fire: {text!r}"


def test_theme_detector_noop_on_short_label_keywords() -> None:
    """A label with no extractable keywords (all tokens under the keyword
    minimum) no-ops instead of false-firing — documented heuristic limit."""
    assert _detect_theme_unsupported("Quiet morning.", {"label": "Ari K-1"}) == []
