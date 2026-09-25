"""Tests for the evening suggest-to-close selection layer (2026-06-10).

Contract locked here (capabilities/kavi_persona/close_suggestions.py):

  * At most CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN LLM judgments per run,
    spent on the NEWEST-created tasks first.
  * At most CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED candidates examined per
    run (Graph-read bound).
  * A (task_id, reply_id) pair is judged ONCE ever — verdicts cache in
    the per-concept state file close_suggestions.json.
  * A surfaced suggestion is not re-suggested until a NEWER sent reply
    appears.
  * Conservative bias: LLM failure → no suggestion (and no cache, so
    tomorrow retries); non-high confidence → no suggestion (cached).
  * Every judgment appends an eval row; Graph errors skip the task; any
    top-level error returns [].

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_close_suggestions_selection.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from capabilities.kavi_persona import close_suggestions as cs
from kavi_runtime.runtime import clients as _clients_mod


def _cfg(tmp_path: Path) -> dict[str, Any]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    return {
        "graph": {"mstodo_shared_list_id": "AQTEST=="},
        "paths": {
            "imessage_state": str(state_dir / "imessage-state.json"),
            "eval_persona_close_suggestions_jsonl": str(
                tmp_path / "evals" / "eval-persona-close-suggestions.jsonl"
            ),
        },
    }


_BASE = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _task(i: int, *, hours_old: int) -> dict[str, Any]:
    return {
        "id": f"task-{i}",
        "title": f"MJ Pay vendor {i} invoice",
        "createdDateTime": _iso(_BASE - timedelta(hours=hours_old)),
    }


class _StubGraph:
    """Programmable Graph stub: per-task linked resources, message
    conversation ids, and per-conversation sent replies."""

    def __init__(self) -> None:
        self.linked: dict[str, list[dict[str, Any]]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.replies: dict[str, list[dict[str, Any]]] = {}
        self.thread: dict[str, list[dict[str, Any]]] = {}
        self.linked_calls: list[str] = []
        self.raise_for_tasks: set[str] = set()

    def wire_task(self, task: dict[str, Any], reply_sent: datetime,
                  *, reply_id: str | None = None) -> None:
        """Give `task` a source email + one sent reply at `reply_sent`."""
        tid = task["id"]
        email_id = f"email-{tid}"
        conv_id = f"conv-{tid}"
        self.linked[tid] = [{
            "externalId": email_id,
            "applicationName": "HomeOS kavi-runtime",
        }]
        self.messages[email_id] = {"conversationId": conv_id}
        self.replies.setdefault(conv_id, []).append({
            "id": reply_id or f"reply-{tid}-1",
            "sentDateTime": _iso(reply_sent),
            "bodyPreview": "Paid it this morning.",
        })

    def list_todo_task_linked_resources(self, list_id: str, task_id: str,
                                        **kw: Any) -> list[dict[str, Any]]:
        self.linked_calls.append(task_id)
        if task_id in self.raise_for_tasks:
            raise RuntimeError("graph boom")
        return self.linked.get(task_id, [])

    def fetch_message(self, message_id: str, **kw: Any) -> dict[str, Any] | None:
        return self.messages.get(message_id)

    def fetch_sentitems_replies(self, conversation_id: str, **kw: Any) -> list[dict[str, Any]]:
        return self.replies.get(conversation_id, [])

    def fetch_thread(self, conversation_id: str, **kw: Any) -> list[dict[str, Any]]:
        return self.thread.get(conversation_id, [])

    def wire_inbound(self, task: dict[str, Any], received: datetime,
                     *, from_addr: str, preview: str,
                     msg_id: str | None = None) -> None:
        """Give `task`'s conversation an INBOUND message (from a third party)
        at `received` — the 'response to an email' done-signal."""
        tid = task["id"]
        conv_id = f"conv-{tid}"
        email_id = f"email-{tid}"
        # Ensure the task has a source email pointing at this conversation.
        self.linked.setdefault(tid, [{
            "externalId": email_id, "applicationName": "HomeOS kavi-runtime",
        }])
        self.messages.setdefault(email_id, {"conversationId": conv_id})
        self.thread.setdefault(conv_id, []).append({
            "id": msg_id or f"inbound-{tid}-1",
            "from": {"emailAddress": {"address": from_addr}},
            "receivedDateTime": _iso(received),
            "bodyPreview": preview,
        })


class _StubClaude:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.verdict: dict[str, Any] = {
            "suggest_close": True,
            "reason": "your reply says it was paid",
            "confidence": "high",
            "_usage": {"input_tokens": 900, "output_tokens": 40},
        }

    def judge_close_suggestion(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return dict(self.verdict)


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> tuple[_StubGraph, _StubClaude]:
    graph = _StubGraph()
    claude = _StubClaude()
    monkeypatch.setattr(
        _clients_mod, "_get_clients", lambda c: (graph, claude, None),
    )
    return graph, claude


# ---- caps: 8 judgments, newest first; 16-candidate scan bound -----------------


def test_judgments_capped_at_max_per_run_newest_first(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    tasks = [_task(i, hours_old=i) for i in range(12)]  # task-0 newest
    for t in tasks:
        graph.wire_task(t, _BASE + timedelta(minutes=30))

    suggestions = cs.select_close_suggestions(_cfg(tmp_path), tasks)

    assert len(claude.calls) == cs.CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN
    judged_titles = [c["task_title"] for c in claude.calls]
    expected_newest = [f"MJ Pay vendor {i} invoice"
                       for i in range(cs.CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN)]
    assert judged_titles == expected_newest
    # Suggestions come back newest task first too.
    assert [s["task_id"] for s in suggestions] == [
        f"task-{i}" for i in range(cs.CLOSE_SUGGEST_MAX_JUDGMENTS_PER_RUN)
    ]


def test_candidate_scan_bounded(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    """Graph reads are bounded: at most CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED
    linked-resource lookups even with many more open tasks."""
    graph, claude = stubs
    tasks = [_task(i, hours_old=i) for i in range(40)]
    # No replies wired → no judgments, but every scanned candidate costs a
    # linked-resource read.
    for t in tasks:
        graph.linked[t["id"]] = []

    cs.select_close_suggestions(_cfg(tmp_path), tasks)

    assert len(graph.linked_calls) == cs.CLOSE_SUGGEST_MAX_CANDIDATES_SCANNED
    assert claude.calls == []


def test_unsorted_input_still_judged_newest_first(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    tasks = [_task(i, hours_old=i) for i in (5, 1, 9, 3)]
    for t in tasks:
        graph.wire_task(t, _BASE)

    cs.select_close_suggestions(_cfg(tmp_path), tasks)

    judged = [c["task_title"] for c in claude.calls]
    assert judged == [f"MJ Pay vendor {i} invoice" for i in (1, 3, 5, 9)]


# ---- judged-pair cache --------------------------------------------------------


def test_pair_judged_once_then_served_from_cache(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    cfg = _cfg(tmp_path)
    task = _task(0, hours_old=2)
    graph.wire_task(task, _BASE - timedelta(hours=1))

    first = cs.select_close_suggestions(cfg, [task])
    second = cs.select_close_suggestions(cfg, [task])

    assert len(claude.calls) == 1, "same (task, reply) pair re-judged"
    assert len(first) == 1 and len(second) == 1
    assert second[0]["task_id"] == "task-0"
    # Cache persisted in the per-concept state file.
    state = json.loads(cs._close_suggestions_state_path(cfg).read_text())
    assert "task-0::reply-task-0-1" in state["judged"]


def test_cached_no_verdict_not_rejudged(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    cfg = _cfg(tmp_path)
    task = _task(0, hours_old=2)
    graph.wire_task(task, _BASE - timedelta(hours=1))
    claude.verdict = {
        "suggest_close": False, "reason": None, "confidence": "high",
        "_usage": {},
    }

    assert cs.select_close_suggestions(cfg, [task]) == []
    assert cs.select_close_suggestions(cfg, [task]) == []
    assert len(claude.calls) == 1


def test_cached_pair_does_not_consume_judgment_budget(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    """Run 1 judges 8 of 9; run 2 spends its budget on the 9th while the
    first 8 serve from cache."""
    graph, claude = stubs
    cfg = _cfg(tmp_path)
    tasks = [_task(i, hours_old=i) for i in range(9)]
    for t in tasks:
        graph.wire_task(t, _BASE + timedelta(minutes=30))

    first = cs.select_close_suggestions(cfg, tasks)
    assert len(first) == 8
    second = cs.select_close_suggestions(cfg, tasks)

    assert len(claude.calls) == 9  # 8 + the 9th on run 2
    assert len(second) == 9


# ---- surfaced suggestions are one-shot per reply -------------------------------


def test_not_resuggested_after_surfacing_until_newer_reply(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    cfg = _cfg(tmp_path)
    task = _task(0, hours_old=10)
    graph.wire_task(task, _BASE - timedelta(hours=5))

    first = cs.select_close_suggestions(cfg, [task])
    assert len(first) == 1
    cs.record_close_suggestions_surfaced(cfg, first)

    # Same reply → silence.
    assert cs.select_close_suggestions(cfg, [task]) == []

    # A NEWER sent reply appears → new pair, judged and suggested again.
    graph.wire_task(task, _BASE - timedelta(hours=1), reply_id="reply-task-0-2")
    third = cs.select_close_suggestions(cfg, [task])
    assert len(third) == 1
    assert third[0]["reply_id"] == "reply-task-0-2"
    assert len(claude.calls) == 2


# ---- conservative bias ----------------------------------------------------------


def test_llm_failure_means_no_suggestion_and_no_cache(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    """API/parse failure → conservative no-suggest; the pair is NOT cached
    so the next run re-judges (failure is transient, a verdict is not)."""
    graph, claude = stubs
    cfg = _cfg(tmp_path)
    task = _task(0, hours_old=2)
    graph.wire_task(task, _BASE - timedelta(hours=1))
    claude.verdict = {
        "suggest_close": False, "reason": None, "confidence": "low",
        "_usage": None, "_error": "api_error: boom",
    }

    assert cs.select_close_suggestions(cfg, [task]) == []
    assert cs.select_close_suggestions(cfg, [task]) == []
    assert len(claude.calls) == 2, "failed judgment should be retried next run"
    assert not cs._close_suggestions_state_path(cfg).exists()


def test_non_high_confidence_yes_not_suggested(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    cfg = _cfg(tmp_path)
    task = _task(0, hours_old=2)
    graph.wire_task(task, _BASE - timedelta(hours=1))
    claude.verdict = {
        "suggest_close": True, "reason": "maybe", "confidence": "medium",
        "_usage": {},
    }

    assert cs.select_close_suggestions(cfg, [task]) == []
    # Definitive verdict → cached, not re-judged.
    assert cs.select_close_suggestions(cfg, [task]) == []
    assert len(claude.calls) == 1


# ---- candidate filtering ---------------------------------------------------------


def test_reply_older_than_task_creation_not_judged(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    task = _task(0, hours_old=2)
    graph.wire_task(task, _BASE - timedelta(hours=6))  # reply predates task

    assert cs.select_close_suggestions(_cfg(tmp_path), [task]) == []
    assert claude.calls == []


def test_task_without_linked_resource_skipped(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    task = _task(0, hours_old=2)
    graph.linked[task["id"]] = []  # hand-created task, no source email

    assert cs.select_close_suggestions(_cfg(tmp_path), [task]) == []
    assert claude.calls == []


def test_graph_error_on_one_task_skips_it_others_continue(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    t0, t1 = _task(0, hours_old=1), _task(1, hours_old=2)
    graph.wire_task(t0, _BASE)
    graph.wire_task(t1, _BASE)
    graph.raise_for_tasks.add("task-0")

    suggestions = cs.select_close_suggestions(_cfg(tmp_path), [t0, t1])

    assert [s["task_id"] for s in suggestions] == ["task-1"]


def test_top_level_failure_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(config: dict) -> Any:
        raise RuntimeError("clients unavailable")

    monkeypatch.setattr(_clients_mod, "_get_clients", _boom)
    task = _task(0, hours_old=1)
    assert cs.select_close_suggestions(_cfg(tmp_path), [task]) == []


def test_empty_and_none_inputs(tmp_path: Path) -> None:
    assert cs.select_close_suggestions(_cfg(tmp_path), []) == []
    assert cs.select_close_suggestions(_cfg(tmp_path), None) == []


# ---- eval rows + state hygiene ----------------------------------------------------


def test_every_judgment_appends_an_eval_row(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    cfg = _cfg(tmp_path)
    t0, t1 = _task(0, hours_old=1), _task(1, hours_old=2)
    graph.wire_task(t0, _BASE)
    graph.wire_task(t1, _BASE)

    cs.select_close_suggestions(cfg, [t0, t1])

    rows = [
        json.loads(line)
        for line in Path(cfg["paths"]["eval_persona_close_suggestions_jsonl"])
        .read_text().splitlines() if line.strip()
    ]
    assert len(rows) == 2
    for row in rows:
        assert row["capability"] == "kavi-persona"
        assert row["kind"] == "close_suggestion_judgment"
        assert row["suggest_close"] is True
        assert row["confidence"] == "high"
        assert row["task_id"] and row["reply_id"] and row["ts"]


def test_state_file_lands_in_state_dir_with_no_stray_tmp(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    graph, claude = stubs
    cfg = _cfg(tmp_path)
    task = _task(0, hours_old=2)
    graph.wire_task(task, _BASE - timedelta(hours=1))

    cs.select_close_suggestions(cfg, [task])

    state_path = cs._close_suggestions_state_path(cfg)
    assert state_path == Path(cfg["paths"]["imessage_state"]).parent / "close_suggestions.json"
    assert state_path.exists()
    leftovers = list(state_path.parent.glob("*.tmp"))
    assert leftovers == [], f"atomic write left tmp files: {leftovers}"


# ---- inbound "response to an email" done-signal (2026-06-22) -----------------


def test_inbound_reply_surfaces_close_suggestion_with_direction(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    """A third-party reply newer than the task ('we received your payment')
    is judged with reply_direction='inbound' and can surface a suggestion,
    even with NO sent reply from the recipient."""
    graph, claude = stubs
    task = _task(0, hours_old=4)
    # No sent reply wired — only an inbound confirmation from the vendor.
    graph.wire_inbound(
        task, _BASE - timedelta(hours=1),
        from_addr="billing@summithvac.com",
        preview="We received your payment of $340 — paid in full.",
    )

    suggestions = cs.select_close_suggestions(
        _cfg(tmp_path), [task],
        account="megha@example.com",
        own_addresses={"megha@example.com"},
    )

    assert len(claude.calls) == 1
    assert claude.calls[0]["reply_direction"] == "inbound"
    assert [s["task_id"] for s in suggestions] == ["task-0"]


def test_own_address_thread_message_is_not_treated_as_inbound(
    tmp_path: Path, stubs: tuple[_StubGraph, _StubClaude],
) -> None:
    """A thread message FROM the recipient's own address is a sent reply,
    not an inbound signal — it must not be double-counted as inbound."""
    graph, claude = stubs
    task = _task(0, hours_old=4)
    graph.wire_inbound(
        task, _BASE - timedelta(hours=1),
        from_addr="megha@example.com",  # her own address
        preview="Paid it, all set.",
    )

    cs.select_close_suggestions(
        _cfg(tmp_path), [task],
        account="megha@example.com",
        own_addresses={"megha@example.com"},
    )

    # No sent-reply wired and the only thread message is her own → excluded
    # from the inbound path, so there is no candidate to judge.
    assert claude.calls == []
