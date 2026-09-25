"""Production-shape regression tests for the 2026-05-08 open-task fetch fix.

What this file covers:

  Bug: the action-layer matcher fetched the 30 most-recently-modified
  tasks from MS To Do regardless of status. On busy days, mass-completions
  pushed older notStarted tasks off the slate, so the matcher honestly
  reported "no candidates" against an incomplete slate. Megha's
  11:38 AM 2026-05-08 trace: "Mark all elders tea pending tasks done"
  → matcher_candidates=0, even though 3 open Elders' Tea tasks existed
  at positions >30 by recency.

  Fix: a new GraphClient.list_open_todo_tasks method that uses a
  server-side $filter on status (notStarted + inProgress only),
  raises $top to 100, and is wired into the action-layer matcher
  path AND the semantic-dedup path on task creation.

Each test mocks the Graph HTTP layer at the lowest sensible boundary
(httpx.get inside graph_client) and asserts on observable behavior:
the request URL contains the right $filter, the response is parsed,
and downstream slate composition works against a paginated-shape input.

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest \\
      tests/test_open_tasks_fetch_2026_05_08.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kavi_runtime import graph_client as gc_mod
from kavi_runtime import handlers
from kavi_runtime.graph_client import GraphClient
from kavi_runtime.state import save_imessage_state


# ---- helpers --------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        pass


def _make_graph_client(monkeypatch: pytest.MonkeyPatch) -> GraphClient:
    """Build a GraphClient that won't try to load real MSAL token caches."""
    client = GraphClient.__new__(GraphClient)
    client._accounts = ["megha@example.com"]
    client._default_account = "megha@example.com"
    client._list_id_cache = {}
    monkeypatch.setattr(
        client, "_headers", lambda account=None: {"Authorization": "Bearer test"},
    )
    return client


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "imessage": {
            "megha_phone": "+15555550101",
            "own_email_addresses": ["megha@example.com"],
        },
        "graph": {"mstodo_shared_list_id": "AQMkADAwTEST=="},
        "action_layer": {"dry_run": False},
        "paths": {
            "imessage_state": str(tmp_path / "imessage-state.json"),
            "eval_persona_action_intent_jsonl": str(tmp_path / "action_intent.jsonl"),
        },
    }


def _patch_send_paths(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]],
) -> None:
    def _send_plain(config: dict, text: str, *, kind: str, **kwargs: Any) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": None})
        return {"verified": True, "fallback_used": False}

    def _send_with_context(
        config: dict, text: str, *, kind: str, context: dict[str, Any], **kwargs: Any,
    ) -> dict[str, Any]:
        sent.append({"text": text, "kind": kind, "context": context})
        return {"verified": True, "fallback_used": False}

    monkeypatch.setattr(handlers, "_send_imessage_with_fallback", _send_plain)
    monkeypatch.setattr(
        handlers, "_send_imessage_with_fallback_and_context", _send_with_context,
    )
    monkeypatch.setattr(handlers, "_log_action_intent_decision", lambda *a, **kw: None)


# ---- 1. Integration test: $filter on the wire -----------------------------


def test_list_open_todo_tasks_uses_status_filter_and_returns_only_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new list_open_todo_tasks fetch must use a server-side $filter
    on status. The Graph mock returns only the open tasks (because that's
    what the real Graph would do given the filter). The runtime must call
    with the right URL params and consume the response correctly.
    """
    captured: dict[str, Any] = {}

    open_tasks = [
        {"id": f"open_{i}", "title": f"Open task {i}", "status": "notStarted"}
        for i in range(50)
    ]
    # Server-side filter: completed wouldn't come back. We assert the
    # client doesn't include any completed in its response handling
    # because the Graph response simulates the filter being applied.
    payload = {"value": open_tasks}

    def _fake_get(url, headers=None, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params or {}
        return _FakeResp(payload)

    monkeypatch.setattr(gc_mod.httpx, "get", _fake_get)

    client = _make_graph_client(monkeypatch)
    result = client.list_open_todo_tasks("LIST_ID_TEST", top=100)

    # URL hits the right resource.
    assert "/me/todo/lists/LIST_ID_TEST/tasks" in captured["url"]
    # Status filter is on the wire.
    filter_param = captured["params"].get("$filter", "")
    assert "status eq 'notStarted'" in filter_param
    assert "status eq 'inProgress'" in filter_param
    # Top raised from the old 30 cap.
    assert captured["params"].get("$top") == 100
    # Order preserved so matcher still sees newest-first among open.
    assert captured["params"].get("$orderby") == "lastModifiedDateTime desc"
    # Response parsed; all 50 open tasks come through.
    assert len(result) == 50


# ---- 2. Production-shape regression: Elders' Tea at position 50 -----------


def test_elders_tea_tasks_at_low_recency_reach_matcher_after_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The 2026-05-08 11:38 AM PT Elders' Tea trace, simulated.

    Pre-fix shape: 30 most-recently-modified tasks fetched. The 3 open
    Elders' Tea tasks were at positions ~50 by recency among 100 open
    tasks (because today's mass-completion bumped 47 completed tasks
    above them in lastModifiedDateTime order).

    Post-fix shape: server-side $filter on status excludes completed
    entirely. The 100-task slate the matcher sees is open-only, so the
    3 Elders' Tea tasks are present regardless of where they sit by
    overall recency.

    Pre-fix this run would have produced matcher_candidates=0. Post-fix
    the matcher receives all 3 Elders' Tea tasks and returns them.
    """
    cfg = _config(tmp_path)
    sent: list[dict[str, Any]] = []
    _patch_send_paths(monkeypatch, sent)
    save_imessage_state(Path(cfg["paths"]["imessage_state"]), {})

    elders_tea_ids = ["et_maple", "et_zoom", "et_theo"]
    # 100 open tasks; 3 Elders' Tea at positions 47, 48, 49 (low recency
    # within the open universe). Pre-fix top=30 cap would have missed
    # all three. Post-fix top=100 + status filter sees them all.
    open_slate: list[dict[str, Any]] = []
    for i in range(47):
        open_slate.append({
            "id": f"unrelated_{i:03d}",
            "title": f"MJ Unrelated open task #{i}",
            "status": "notStarted",
        })
    open_slate.append({
        "id": "et_maple",
        "title": (
            "MJ Elders' Tea at Maple tomorrow Fri May 8 at 1:15pm — "
            "confirm guests received invite"
        ),
        "status": "notStarted",
    })
    open_slate.append({
        "id": "et_zoom",
        "title": "MJ Forward Elders' Tea Zoom link to your guest",
        "status": "notStarted",
    })
    open_slate.append({
        "id": "et_theo",
        "title": "MJ Theo to bring fancy clothes for Elder's tea",
        "status": "notStarted",
    })
    for i in range(50):
        open_slate.append({
            "id": f"older_{i:03d}",
            "title": f"MJ Older open task #{i}",
            "status": "notStarted",
        })

    claude = MagicMock()
    claude.classify_correction.return_value = {
        "is_correction": False, "correction_type": None,
    }
    claude.classify_action_intent.return_value = {
        "has_action": True,
        "action_type": "mark_done",
        "target_text": "elders tea pending tasks",
        "confidence": "low",
    }
    claude.match_target_to_open_task.return_value = {
        "match_id": None,
        "candidate_task_ids": elders_tea_ids,
        "confidence": "medium",
        "reasoning": (
            "Three open Elders' Tea tasks; 'all pending' is a batch "
            "reference."
        ),
    }
    claude.compose_action_clarifying_reply.return_value = (
        "Found 3 open Elders' Tea tasks: confirm guests, Zoom link, "
        "Theo's fancy clothes. Mark all three?"
    )

    graph = MagicMock()
    graph.list_open_todo_tasks.return_value = open_slate

    result = handlers._try_handle_action_intent(
        free_text="Mark all elders tea pending tasks done",
        recent_outbound=[],
        claude=claude, graph=graph,
        list_id=cfg["graph"]["mstodo_shared_list_id"], config=cfg,
        sender_handle="+15555550101",
        source_imessage_id="im_elders_tea_2026_05_08",
    )

    assert result is not None
    assert result["status"] == "action_clarifying"

    # The runtime called list_open_todo_tasks (the new fetch), NOT
    # list_recent_todo_tasks (the old 30-cap recency fetch).
    graph.list_open_todo_tasks.assert_called_once()
    list_kwargs = graph.list_open_todo_tasks.call_args.kwargs
    list_args = graph.list_open_todo_tasks.call_args.args
    top_value = list_kwargs.get("top") if "top" in list_kwargs else (
        list_args[1] if len(list_args) > 1 else None
    )
    assert top_value == 100
    # Old method must not be called from the action-layer path.
    graph.list_recent_todo_tasks.assert_not_called()

    # Matcher saw the full 100-task open slate, with all 3 Elders' Tea
    # tasks present.
    claude.match_target_to_open_task.assert_called_once()
    matcher_args = claude.match_target_to_open_task.call_args.args
    matcher_open_tasks = matcher_args[1]
    assert len(matcher_open_tasks) == 100
    elders_in_input = [
        t for t in matcher_open_tasks
        if "Elders" in (t.get("title") or "") or "Elder's" in (t.get("title") or "")
    ]
    assert len(elders_in_input) == 3
    assert {t["id"] for t in elders_in_input} == set(elders_tea_ids)


# ---- 3. Edge: empty open-task list ----------------------------------------


def test_list_open_todo_tasks_handles_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Graph returns an empty `value` array when there are zero open
    tasks. The fetch should return an empty list cleanly, not raise."""
    def _fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResp({"value": []})

    monkeypatch.setattr(gc_mod.httpx, "get", _fake_get)

    client = _make_graph_client(monkeypatch)
    result = client.list_open_todo_tasks("LIST_ID_TEST", top=100)
    assert result == []


# ---- 4. Edge: inProgress tasks included -----------------------------------


def test_list_open_todo_tasks_includes_inprogress_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inProgress task is still actionable — Megha can mark it done.
    The $filter must include both notStarted AND inProgress so the
    matcher's slate doesn't miss tasks Megha started but hasn't finished.

    Even though inProgress is rarely used in this household today, the
    runtime must handle it correctly.
    """
    captured: dict[str, Any] = {}

    in_progress_task = {
        "id": "ip_1",
        "title": "MJ In-progress task",
        "status": "inProgress",
    }
    not_started_task = {
        "id": "ns_1",
        "title": "MJ Not-started task",
        "status": "notStarted",
    }

    def _fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params or {}
        # Real Graph honors the filter; both statuses come back.
        return _FakeResp({"value": [in_progress_task, not_started_task]})

    monkeypatch.setattr(gc_mod.httpx, "get", _fake_get)

    client = _make_graph_client(monkeypatch)
    result = client.list_open_todo_tasks("LIST_ID_TEST", top=100)

    filter_param = captured["params"].get("$filter", "")
    assert "status eq 'inProgress'" in filter_param
    assert "status eq 'notStarted'" in filter_param
    statuses = {t["status"] for t in result}
    assert statuses == {"notStarted", "inProgress"}
