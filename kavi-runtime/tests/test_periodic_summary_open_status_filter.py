"""Tests for the MS To Do open-status filter on periodic_summary inputs.

Why these tests exist: 2026-05-31. The 2026-05-30 9 PM rollup re-surfaced
"Walk for Kids" — a pending Q&A whose underlying MS To Do task Megha had
already marked done. Root cause: the 2026-05-29 open-status filter was
wired only into the `summary_queue` drain path; `pending_questions` (which
carries the same `task_id` for the same MS To Do row) was a parallel
surfacing path with no done-status filter, so it re-fired every periodic
summary cycle until Megha replied.

These tests lock the behavior on both surfaces and on the fail-open
contract so future refactors can't silently regress to the 2026-05-30
shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kavi_runtime import handlers


def _wire_graph(monkeypatch: pytest.MonkeyPatch, *, open_tasks):
    """Stub `_get_clients` so the open-status filter sees `open_tasks` as
    the list_open_todo_tasks response without hitting Graph. `open_tasks`
    may be a list (normal path), a non-list value (defensive path), or an
    Exception class to raise (failure path)."""
    graph = MagicMock()
    if isinstance(open_tasks, type) and issubclass(open_tasks, Exception):
        graph.list_open_todo_tasks.side_effect = open_tasks("simulated")
    else:
        graph.list_open_todo_tasks.return_value = open_tasks
    claude = MagicMock()
    bb = MagicMock()
    _stub_get_clients = lambda cfg: (graph, claude, bb)
    monkeypatch.setattr(handlers, "_get_clients", _stub_get_clients)
    from kavi_runtime.runtime import clients as _clients_mod, send_imessage as _send_mod
    monkeypatch.setattr(_clients_mod, "_get_clients", _stub_get_clients)
    monkeypatch.setattr(_send_mod, "_get_clients", _stub_get_clients)
    return graph


def _cfg(list_id: str | None = "list-shared") -> dict:
    """Minimal config with a graph.mstodo_shared_list_id (or None to test the
    misconfig path)."""
    return {"graph": {"mstodo_shared_list_id": list_id}}


# ---- _fetch_open_todo_task_ids -------------------------------------------


def test_fetch_open_task_ids_returns_set_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns the set of open task IDs from the Graph response."""
    _wire_graph(monkeypatch, open_tasks=[
        {"id": "open-1", "title": "T1"},
        {"id": "open-2", "title": "T2"},
    ])
    out = handlers._fetch_open_todo_task_ids(_cfg())
    assert out == {"open-1", "open-2"}


def test_fetch_open_task_ids_none_when_list_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No list_id configured → fail-open (return None)."""
    out = handlers._fetch_open_todo_task_ids(_cfg(list_id=None))
    assert out is None


def test_fetch_open_task_ids_none_when_graph_returns_non_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: if Graph returns a MagicMock-without-spec or any non-list
    value, treat as fail-open (None) instead of silently zeroing out."""
    _wire_graph(monkeypatch, open_tasks=MagicMock())  # not a list
    out = handlers._fetch_open_todo_task_ids(_cfg())
    assert out is None


def test_fetch_open_task_ids_none_when_graph_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph error → fail-open (None). Kavi keeps composing rather than going
    silent on the rollup."""
    _wire_graph(monkeypatch, open_tasks=RuntimeError)
    out = handlers._fetch_open_todo_task_ids(_cfg())
    assert out is None


# ---- _filter_summary_queue_by_open_status --------------------------------


def test_summary_queue_filter_drops_done_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue entries whose task_id is NOT in the open set get dropped."""
    _wire_graph(monkeypatch, open_tasks=[{"id": "open-1"}])
    queued = [
        {"task_id": "open-1", "title": "still open"},
        {"task_id": "closed-1", "title": "marked done"},
    ]
    out = handlers._filter_summary_queue_by_open_status(_cfg(), queued)
    assert [q["task_id"] for q in out] == ["open-1"]


def test_summary_queue_filter_accepts_shared_open_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller can pass a pre-fetched `open_ids` snapshot; no Graph call is
    made when one is provided. Enables sharing one Graph snapshot across
    summary_queue + pending_questions filters in `_periodic_summary_impl`."""
    graph = _wire_graph(monkeypatch, open_tasks=[{"id": "should-not-be-called"}])
    queued = [{"task_id": "open-1"}, {"task_id": "done-1"}]
    out = handlers._filter_summary_queue_by_open_status(
        _cfg(), queued, open_ids={"open-1"},
    )
    assert [q["task_id"] for q in out] == ["open-1"]
    graph.list_open_todo_tasks.assert_not_called()


def test_summary_queue_filter_fails_open_on_graph_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph error → surface original queue (Megha would rather see a done
    task than have Kavi go silent on the rollup)."""
    _wire_graph(monkeypatch, open_tasks=RuntimeError)
    queued = [{"task_id": "anything"}, {"task_id": "else"}]
    out = handlers._filter_summary_queue_by_open_status(_cfg(), queued)
    assert out == queued


def test_summary_queue_filter_empty_input_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty queue → no Graph call, return as-is."""
    graph = _wire_graph(monkeypatch, open_tasks=[])
    out = handlers._filter_summary_queue_by_open_status(_cfg(), [])
    assert out == []
    graph.list_open_todo_tasks.assert_not_called()


# ---- _filter_pending_questions_by_open_status (new 2026-05-31) -----------


def test_pending_questions_filter_drops_done_task_qas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical 2026-05-31 bug repro: a pending Q&A pointing at a task
    Megha has marked done in MS To Do must be dropped before the composer
    sees it. Closes the symptom where the 9 PM rollup re-surfaced "Walk for
    Kids" every cycle until Megha texted a reply."""
    _wire_graph(monkeypatch, open_tasks=[{"id": "open-task"}])
    pending = [
        {"task_id": "open-task", "task_title_rendered": "MJ Decide on X"},
        {"task_id": "closed-task", "task_title_rendered": "MJ Decide on Walk for Kids"},
    ]
    out = handlers._filter_pending_questions_by_open_status(_cfg(), pending)
    assert [q["task_title_rendered"] for q in out] == ["MJ Decide on X"]


def test_pending_questions_filter_keeps_qas_with_no_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: a Q&A without a task_id is not anchored to MS To Do, so
    the open-status check doesn't apply and the entry stays. (Real Q&As
    today all have task_ids but the runtime shouldn't silently drop legacy
    entries that don't.)"""
    _wire_graph(monkeypatch, open_tasks=[{"id": "open-task"}])
    pending = [
        {"task_id": None, "task_title_rendered": "MJ Untethered Q&A"},
        {"task_id": "", "task_title_rendered": "MJ Empty task_id Q&A"},
        {"task_id": "open-task", "task_title_rendered": "MJ Decide on X"},
        {"task_id": "closed-task", "task_title_rendered": "MJ Done"},
    ]
    out = handlers._filter_pending_questions_by_open_status(_cfg(), pending)
    titles = [q["task_title_rendered"] for q in out]
    assert "MJ Untethered Q&A" in titles
    assert "MJ Empty task_id Q&A" in titles
    assert "MJ Decide on X" in titles
    assert "MJ Done" not in titles


def test_pending_questions_filter_accepts_shared_open_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller passes a pre-fetched snapshot → no second Graph call. This is
    how `_periodic_summary_impl` shares one Graph call across both filters."""
    graph = _wire_graph(monkeypatch, open_tasks=[{"id": "should-not-be-called"}])
    pending = [{"task_id": "open-1"}, {"task_id": "closed-1"}]
    out = handlers._filter_pending_questions_by_open_status(
        _cfg(), pending, open_ids={"open-1"},
    )
    assert [q["task_id"] for q in out] == ["open-1"]
    graph.list_open_todo_tasks.assert_not_called()


def test_pending_questions_filter_fails_open_on_graph_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph error → surface original pending list (consistent fail-open
    contract with summary_queue filter)."""
    _wire_graph(monkeypatch, open_tasks=RuntimeError)
    pending = [{"task_id": "a"}, {"task_id": "b"}]
    out = handlers._filter_pending_questions_by_open_status(_cfg(), pending)
    assert out == pending


def test_pending_questions_filter_empty_input_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty pending list → no Graph call."""
    graph = _wire_graph(monkeypatch, open_tasks=[])
    out = handlers._filter_pending_questions_by_open_status(_cfg(), [])
    assert out == []
    graph.list_open_todo_tasks.assert_not_called()
