"""Tests for the two selection-behavior verify gates added 2026-06-10.

  * owner_leak — a digest composed for recipient R must not name a task
    whose title-prefix owner is the other person ("MJ " = Megha,
    "MM " = Max, unprefixed = Megha).
  * due_soon_dropped — if the synthetic payload carries a due-soon item,
    the composed MORNING digest must reference it (at least one content
    keyword from the due-soon task title). No-op for the 9 PM rollup.

Same stub pattern as tests/test_synthetic_verify_periodic_summary.py: the
LLM composer is replaced so the tests assert the GATE machinery, not the
model's compliance. Both gates must fire on violating synthetic output
and pass on clean output.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_verify_owner_leak_due_soon_gates.py -v
"""

from __future__ import annotations

from unittest.mock import patch

from kavi_runtime import synthetic_compose
from capabilities.kavi_persona.verify import SELECTION_GATES


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


# ---- registration ------------------------------------------------------------


def test_both_gates_registered_in_selection_gates() -> None:
    assert "owner_leak" in SELECTION_GATES
    assert "due_soon_dropped" in SELECTION_GATES


# ---- owner_leak ----------------------------------------------------------------


def test_owner_leak_fires_when_meghas_digest_names_max_task() -> None:
    with _stub_compose("Cleaner cash for the house cleaner is due today."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [
                    {"task_id": "t-mj", "title": "MJ Book dentist"},
                    {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
                ],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
            },
        )
    assert result["verdict"] == "FAIL"
    leak = [f for f in result["failures"] if f["gate"] == "owner_leak"]
    assert leak, f"expected owner_leak failure, got {result['failures']}"
    assert "max" in leak[0]["detail"]


def test_owner_leak_fires_when_maxs_digest_names_unprefixed_megha_task() -> None:
    """Unprefixed legacy titles are Megha's; Max's digest naming one is a
    leak."""
    with _stub_compose("Order diapers for the kids is still open today."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [
                    {"task_id": "t-legacy", "title": "Order diapers for kids"},
                    {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
                ],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "max",
            },
        )
    assert result["verdict"] == "FAIL"
    assert any(f["gate"] == "owner_leak" for f in result["failures"])


def test_owner_leak_passes_on_clean_own_task_output() -> None:
    with _stub_compose("Heads up: dentist booking still needs a slot today."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [
                    {"task_id": "t-mj", "title": "MJ Book dentist slot"},
                    {"task_id": "t-mm", "title": "MM Leave cleaner cash"},
                ],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "recipient": "megha",
            },
        )
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_owner_leak_scans_due_soon_titles_too() -> None:
    with _stub_compose("Cleaner cash payment is due tomorrow."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "megha",
                "due_soon": [
                    {"title": "MM Cleaner cash payment", "due_date": "2026-06-11", "days_until": 1},
                ],
            },
        )
    assert any(f["gate"] == "owner_leak" for f in result["failures"])


def test_owner_leak_defaults_recipient_to_megha() -> None:
    """Payloads without the new field behave exactly as before the split:
    composed-for-Megha, her own tasks never flag."""
    with _stub_compose("Heads up: dentist booking still needs a slot today."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [{"task_id": "t-mj", "title": "MJ Book dentist slot"}],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
            },
        )
    assert result["input_payload"]["recipient"] == "megha"
    assert not any(f["gate"] == "owner_leak" for f in result["failures"])


def test_recipient_is_threaded_into_the_composer_call() -> None:
    """The verify endpoint must replay the composer AS the named
    recipient, not just gate the output."""
    with _stub_compose("Cleaner cash is due today."):
        synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "max",
                "due_soon": [
                    {"title": "MM Cleaner cash", "due_date": "2026-06-10", "days_until": 0},
                ],
            },
        )
    kwargs = _StubClaudeClient._seen_kwargs
    assert kwargs is not None
    assert kwargs["recipient_name"] == "Max"
    assert kwargs["due_soon"][0]["title"] == "MM Cleaner cash"


# ---- due_soon_dropped -----------------------------------------------------------


def test_due_soon_dropped_fires_when_morning_digest_omits_item() -> None:
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
                "due_soon": [
                    {"title": "MJ Pay Boonli invoice", "due_date": "2026-06-11", "days_until": 1},
                ],
            },
        )
    assert result["verdict"] == "FAIL"
    dropped = [f for f in result["failures"] if f["gate"] == "due_soon_dropped"]
    assert dropped, f"expected due_soon_dropped, got {result['failures']}"
    assert "Boonli" in dropped[0]["detail"]


def test_due_soon_dropped_passes_when_item_referenced() -> None:
    with _stub_compose("Boonli invoice is due tomorrow."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "megha",
                "due_soon": [
                    {"title": "MJ Pay Boonli invoice", "due_date": "2026-06-11", "days_until": 1},
                ],
            },
        )
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_due_soon_dropped_flags_each_missing_item() -> None:
    with _stub_compose("Boonli invoice is due tomorrow."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "megha",
                "due_soon": [
                    {"title": "MJ Pay Boonli invoice", "due_date": "2026-06-11", "days_until": 1},
                    {"title": "MJ Submit BCBA paperwork", "due_date": "2026-06-08", "days_until": -2},
                ],
            },
        )
    dropped = [f for f in result["failures"] if f["gate"] == "due_soon_dropped"]
    assert len(dropped) == 1
    assert "BCBA" in dropped[0]["detail"]


def test_due_soon_dropped_is_noop_for_rollup() -> None:
    """The 9 PM rollup keeps its counts + named-top shape; due_soon in a
    rollup payload does not arm the gate."""
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
                "due_soon": [
                    {"title": "MJ Pay Boonli invoice", "due_date": "2026-06-11", "days_until": 1},
                ],
            },
        )
    assert not any(f["gate"] == "due_soon_dropped" for f in result["failures"])


def test_due_soon_dropped_noop_when_composer_returns_none() -> None:
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
                "due_soon": [
                    {"title": "MJ Pay Boonli invoice", "due_date": "2026-06-11", "days_until": 1},
                ],
            },
        )
    assert result["output"] is None
    assert result["verdict"] == "PASS"


# ---- input_payload evidence ------------------------------------------------------


def test_new_fields_echoed_in_input_payload_for_verifier_evidence() -> None:
    due = [{"title": "MJ Pay Boonli invoice", "due_date": "2026-06-11", "days_until": 1}]
    with _stub_compose("Boonli invoice is due tomorrow."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "recipient": "max",
                "due_soon": due,
            },
        )
    assert result["input_payload"]["recipient"] == "max"
    assert result["input_payload"]["due_soon"] == due
