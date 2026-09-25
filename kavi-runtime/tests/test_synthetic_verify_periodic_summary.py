"""Tests for the selection-behavior verify mode on periodic_summary.

Added 2026-05-31. The 2026-05-30 9 PM rollup proved that the prior
output-shape Verifier missed selection-behavior bugs. These tests lock
the three new behavior gates:

  - done_task_surfaced: output names a title whose task_id is in
    `closed_task_ids` → FAIL.
  - past_event_surfaced: output names a title flagged as past_event → FAIL.
  - duplicate_phrase: same 4-word phrase appears twice in output → FAIL.

The tests stub the LLM composer so we control the output directly. This
lets us assert the GATE behavior in isolation from the LLM's ability to
honor the skill rule. The LLM behavior is verified separately by the live
production-observation check after deploy; these tests cover the verifier
machinery itself.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kavi_runtime import synthetic_compose


def _cfg() -> dict:
    return {
        "claude": {"model": "claude-sonnet-4-6", "api_key": "fake"},
        "model_routing": {},
    }


class _StubClaudeClient:
    """Drop-in replacement for ClaudeClient in tests. Bypasses the real
    Anthropic SDK + keychain resolve, so the verify-mode tests can run in
    CI without secrets. Each test sets `_output` to the fake LLM output it
    wants verify_periodic_summary_selection to gate against."""

    _output: str | None = None

    def __init__(self, config: dict) -> None:
        self._config = config

    def compose_periodic_summary(self, **_kwargs) -> str | None:
        return type(self)._output


def _stub_compose(output: str | None):
    """Context manager: swap ClaudeClient for the stub and pin its output.
    Patches at the capability module level where verify_periodic_summary_selection
    actually constructs ClaudeClient. Phase 4 (2026-06-02) move: function body
    moved to capabilities/kavi_persona/verify.py, so the patch target moved
    too. The synthetic_compose re-export is a thin pass-through."""
    _StubClaudeClient._output = output
    return patch(
        "capabilities.kavi_persona.verify.ClaudeClient",
        new=_StubClaudeClient,
    )


# ---- gate 1: done_task_surfaced -----------------------------------------


def test_verify_passes_when_no_done_tasks_flagged() -> None:
    """No `closed_task_ids` in body → done-task gate is a no-op."""
    with _stub_compose("Heads up: BCBA invoice due Friday."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [{"task_id": "t1", "title": "BCBA invoice from Hollis"}],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
            },
        )
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_verify_fails_when_output_names_done_task_from_pending() -> None:
    """The canonical 2026-05-30 9 PM rollup bug: Q&A pointing at a task
    Megha marked done in MS To Do leaks into the composed output."""
    with _stub_compose("Walk for Kids this Saturday — still need your call. 9am at HLCC."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [{
                    "task_id": "done-task-1",
                    "task_title_rendered": "MJ Decide on Walk for Kids this Saturday",
                }],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "closed_task_ids": ["done-task-1"],
            },
        )
    assert result["verdict"] == "FAIL"
    assert any(f["gate"] == "done_task_surfaced" for f in result["failures"])
    detail = next(f["detail"] for f in result["failures"] if f["gate"] == "done_task_surfaced")
    assert "done-task-1" in detail


def test_verify_fails_when_output_names_done_task_from_queued() -> None:
    """Same gate, summary_queue surface (parallel to pending_questions)."""
    with _stub_compose("Added Krispy Kreme pickup Saturday at 7am."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [{
                    "task_id": "done-q-1",
                    "title": "Krispy Kreme pickup Saturday",
                }],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "closed_task_ids": ["done-q-1"],
            },
        )
    assert result["verdict"] == "FAIL"
    assert any(f["gate"] == "done_task_surfaced" for f in result["failures"])


def test_verify_done_task_gate_ignores_non_matching_output() -> None:
    """closed_task_ids is set but output doesn't name the title → PASS.
    Ensures the gate doesn't over-fire on inputs the LLM correctly skipped."""
    with _stub_compose("Quiet morning — nothing new queued."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [{
                    "task_id": "done-task-1",
                    "task_title_rendered": "MJ Decide on Walk for Kids",
                }],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "closed_task_ids": ["done-task-1"],
            },
        )
    assert result["verdict"] == "PASS"


# ---- gate 2: past_event_surfaced ----------------------------------------


def test_verify_fails_when_output_names_past_event() -> None:
    """9 PM Saturday rollup re-anchoring on a 9am Saturday event. The
    skill stale-date rule should have skipped it; the verifier catches it."""
    with _stub_compose("Walk for Kids this Saturday — still need your call. 9am at HLCC."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [{
                    "task_id": "q1",
                    "task_title_rendered": "MJ Decide on Walk for Kids this Saturday",
                }],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "past_event_titles": ["Walk for Kids this Saturday"],
            },
        )
    assert result["verdict"] == "FAIL"
    assert any(f["gate"] == "past_event_surfaced" for f in result["failures"])


def test_verify_past_event_gate_no_op_when_list_empty() -> None:
    """No `past_event_titles` → gate skipped."""
    with _stub_compose("Heads up: BCBA invoice due Friday."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [{"task_id": "t1", "title": "BCBA invoice"}],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
            },
        )
    assert result["verdict"] == "PASS"
    assert not any(f["gate"] == "past_event_surfaced" for f in result["failures"])


# ---- gate 3: duplicate_phrase -------------------------------------------


def test_verify_fails_when_output_contains_duplicate_phrase() -> None:
    """If the same 4-word phrase appears twice, gate fires."""
    with _stub_compose(
        "Walk for Kids Saturday morning at HLCC. "
        "Reminder: Walk for Kids Saturday morning at HLCC."
    ):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
            },
        )
    assert result["verdict"] == "FAIL"
    assert any(f["gate"] == "duplicate_phrase" for f in result["failures"])


def test_verify_passes_on_clean_short_output() -> None:
    """Typical Kavi-shape output (one priority, no dup, no done task) → PASS."""
    with _stub_compose("Heads up: BCBA invoice from Hollis due Friday."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [{"task_id": "t1", "title": "BCBA invoice from Hollis"}],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
            },
        )
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


def test_verify_returns_input_payload_for_evidence() -> None:
    """Verifier sub-agent reads input_payload to record what the gates saw."""
    with _stub_compose("Heads up: X due Friday."):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [{"task_id": "t1", "title": "X"}],
                "pending_questions": [],
                "pending_facts": [],
                "is_rollup": False,
                "time_of_day": "morning",
                "closed_task_ids": ["done-1"],
                "past_event_titles": ["something else"],
            },
        )
    assert result["input_payload"]["closed_task_ids"] == ["done-1"]
    assert result["input_payload"]["past_event_titles"] == ["something else"]


def test_verify_handles_none_output_gracefully() -> None:
    """LLM returns None (composer failure) → no crash; verdict PASS with no
    failures because there's no output to gate. (The shape-side caller is
    expected to flag a None output separately.)"""
    with _stub_compose(None):
        result = synthetic_compose.verify_periodic_summary_selection(
            _cfg(),
            {
                "queued_tasks": [],
                "pending_questions": [{
                    "task_id": "done-1",
                    "task_title_rendered": "MJ Decide on X",
                }],
                "pending_facts": [],
                "is_rollup": True,
                "time_of_day": "9pm",
                "closed_task_ids": ["done-1"],
            },
        )
    assert result["output"] is None
    assert result["verdict"] == "PASS"
    assert result["failures"] == []


# ---- helper-function microtests ----------------------------------------


def test_title_keywords_strips_short_words_and_action_verbs() -> None:
    out = synthetic_compose._title_keywords(
        "MJ Decide on Walk for Kids this Saturday (May 30, 9am at HLCC)"
    )
    assert "Walk" in out
    assert "Kids" in out
    assert "Saturday" in out
    # min_len=4 filter: "for", "on", "MJ", "May" all 3 chars or less
    assert "for" not in [w.lower() for w in out]
    # action verb filter: "Decide" explicitly removed
    assert "Decide" not in out


def test_output_names_title_requires_two_keyword_overlap() -> None:
    title = "Walk for Kids this Saturday"
    # Two-keyword overlap (Walk + Kids) → match.
    assert synthetic_compose._output_names_title(
        "Walk for Kids Saturday at 9am", title,
    )
    # One-keyword overlap → no match (avoids over-firing on single common words).
    assert not synthetic_compose._output_names_title(
        "Saturday is going to be busy.", title,
    )


def test_detect_duplicate_phrases_empty_on_short_text() -> None:
    """Output too short to form two non-overlapping 4-word shingles → no
    duplicates detected (avoids false positives on tight Kavi-shape text)."""
    assert synthetic_compose._detect_duplicate_phrases(
        "Heads up: BCBA invoice due Friday.",
    ) == []


def test_detect_duplicate_phrases_catches_repeat() -> None:
    out = "alpha beta gamma delta foo bar baz alpha beta gamma delta"
    dups = synthetic_compose._detect_duplicate_phrases(out)
    assert any("alpha beta gamma delta" in d for d in dups)
