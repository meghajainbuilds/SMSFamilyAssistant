"""Tests for the count_without_axis deep verify gate.

Added 2026-06-02 Phase 1. The gate exists because tonight's 9 PM rollup
shipped "7 new tasks in MS To Do" — a bare count paired with a pointer
to another surface, no axis, no named example. The shape violated the
Aggregation rule in capabilities/kavi-persona.md but slipped past
structural_checks.py (regex too narrow) and the three existing deep
verify gates (all of which catch wrong-content, none catch missing-
required-content).

These tests pin the gate behavior so a future refactor cannot quietly
re-open the same failure mode.
"""

from __future__ import annotations

from capabilities.kavi_persona.verify import (
    _detect_count_without_axis,
    SELECTION_GATES,
)


class TestCountWithoutAxisGate:
    """The shape that shipped tonight is the canonical bad case."""

    def test_tonight_actual_bad_shape_fails(self):
        bad = "7 new tasks in MS To Do."
        failures = _detect_count_without_axis(bad, top_importance_tasks=None)
        assert len(failures) == 1
        assert "7 new tasks" in failures[0]
        assert "pointer" in failures[0] or "no axis" in failures[0]

    def test_last_night_actual_bad_shape_fails(self):
        bad = "7 new tasks + 1 open RSVP (Anita's housewarming). All in MS To Do — nothing urgent tonight."
        failures = _detect_count_without_axis(bad, top_importance_tasks=None)
        # "7 new tasks" appears with no axis nearby; "1 open RSVP" might also
        # trigger if "items" matches. Check at least the canonical bad
        # phrase is caught.
        assert any("7 new tasks" in f for f in failures)

    def test_skill_old_bad_example_fails(self):
        bad = "8 new tasks in MS To Do, 2 need your call. Tap MS To Do for the list."
        failures = _detect_count_without_axis(bad, top_importance_tasks=None)
        # "8 new tasks" has no axis word in its window. "2 need your call"
        # is a soft pass because "needs" is in the axis word list and
        # "your call" reads as named action — but the leading "8 new
        # tasks" pattern is the canonical bad shape and must fire.
        assert any("8 new tasks" in f for f in failures)

    def test_count_plus_pointer_no_axis_fails_loud(self):
        bad = "9 more tasks in MS To Do."
        failures = _detect_count_without_axis(bad, top_importance_tasks=None)
        assert len(failures) == 1
        assert "pointer" in failures[0]


class TestCountWithoutAxisGate_PassingShapes:
    """The shape we're trying to ship after Phase 1 lands."""

    def test_three_count_shape_passes(self):
        """The new 9 PM rollup target shape."""
        good = "7 added today, 3 done, 2 over a week. Kelly meeting tomorrow 8:30 still needs your call."
        failures = _detect_count_without_axis(good, top_importance_tasks=None)
        # All three counts have axis words ("today", "done", "over").
        assert failures == []

    def test_count_with_axis_passes(self):
        good = "5 more tasks over 3 days old."
        failures = _detect_count_without_axis(good, top_importance_tasks=None)
        assert failures == []

    def test_count_with_named_example_passes(self):
        """A count plus a named title from top_importance_tasks reads as
        actionable even without an explicit axis word."""
        good = "2 tasks: Kelly meeting and Boonli payment."
        # Provide top_importance_tasks so the named-keyword check fires.
        top = [{"title": "Kelly meeting", "reason": "tomorrow 8:30"}]
        failures = _detect_count_without_axis(good, top_importance_tasks=top)
        # "Kelly meeting" appears in window; named-keyword check passes.
        # Actually "tasks" with no axis word and no named keyword in
        # window would fail, but "Kelly" IS in window. Verify.
        assert failures == []

    def test_decision_axis_passes(self):
        good = "12 emails today, 3 still need a decision."
        failures = _detect_count_without_axis(good, top_importance_tasks=None)
        assert failures == []

    def test_all_clear_shape_passes(self):
        """The 9 PM all-clear shape: three zero-counts each with axis."""
        good = "Quiet day. 0 added, 0 done, 0 over a week. Tomorrow we go again."
        failures = _detect_count_without_axis(good, top_importance_tasks=None)
        assert failures == []

    def test_empty_output_passes(self):
        assert _detect_count_without_axis("", top_importance_tasks=None) == []
        assert _detect_count_without_axis("", top_importance_tasks=[]) == []


class TestCountWithoutAxisGate_NonCountText:
    """The gate should only trigger on count claims, not on prose."""

    def test_no_count_passes(self):
        good = "Heads up: BCBA invoice from Hollis due Friday."
        assert _detect_count_without_axis(good, top_importance_tasks=None) == []

    def test_single_named_priority_passes(self):
        good = "Kelly meeting tomorrow 8:30 still needs your call."
        assert _detect_count_without_axis(good, top_importance_tasks=None) == []


class TestGateRegistration:
    """The gate name must appear in SELECTION_GATES so the architectural
    deep-verify-parity test (test_deep_verify_parity.py) sees it."""

    def test_count_without_axis_is_in_selection_gates(self):
        assert "count_without_axis" in SELECTION_GATES
