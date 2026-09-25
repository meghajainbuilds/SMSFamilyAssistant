"""Deterministic executors for the intent-first dispatch.

Covers each executor (close incl. answered-by-close, qa_keep/qa_drop for
both question kinds, create, delete, pause/resume, correction), the
multi-intent ALL-EXECUTE invariant (one failure never swallows the rest),
and the dry-run contract: ZERO Graph calls and ZERO state writes
(sentinel object that raises on any attribute access).

Run with:
    cd kavi-runtime && .venv/bin/python -m pytest tests/test_intent_executors.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from capabilities.kavi_persona.intent_executors import execute_intents
from kavi_runtime.state_per_concept import load_questions, save_questions


class _Sentinel:
    """Raises on ANY attribute access — proves dry-run touches nothing."""

    def __getattr__(self, name: str):
        raise AssertionError(f"dry-run touched forbidden object attribute {name!r}")


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "paths": {
            "imessage_state": str(tmp_path / "state" / "imessage-state.json"),
            "corrections_jsonl": str(tmp_path / "corrections.jsonl"),
            "household_md": str(tmp_path / "household.md"),
            "promoted_patterns_jsonl": str(tmp_path / "promoted.jsonl"),
            "rejected_patterns_jsonl": str(tmp_path / "rejected.jsonl"),
        },
    }


def _seed_questions(config: dict, questions: list[dict[str, Any]]) -> Path:
    state_path = Path(config["paths"]["imessage_state"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    save_questions(state_path, {"questions": questions})
    return state_path


def _run(intents, *, config, graph, pending_questions, dry_run=False,
         sender="megha", free_text="test"):
    return execute_intents(
        intents,
        config=config,
        graph=graph,
        claude=MagicMock(),
        list_id="LIST",
        sender=sender,
        sender_handle="+15555550101",
        pending_questions=pending_questions,
        free_text=free_text,
        dry_run=dry_run,
    )


# ---- close_task (incl. answered-by-close) ----------------------------------


def test_close_marks_every_target_done(tmp_path) -> None:
    cfg = _config(tmp_path)
    _seed_questions(cfg, [])
    graph = MagicMock()
    graph.mark_task_done.return_value = (True, "title")
    intents = [{
        "type": "close_task", "target_text": "all seven",
        "targets": [{"id": f"t{i}", "title": f"MJ Task {i}"} for i in range(7)],
        "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[])
    assert graph.mark_task_done.call_count == 7
    assert rows[0]["result"] == "success"
    assert rows[0]["target_ids"] == [f"t{i}" for i in range(7)]
    assert rows[0]["simulated"] is False


def test_close_resolves_pending_question_answered_by_close(tmp_path) -> None:
    """Canonical-direction side effect: closing a task with a pending [?]
    question pops the question (answered-by-close) — it is never re-asked
    and never coerced into keep/drop."""
    cfg = _config(tmp_path)
    question = {
        "id": "q-anita", "task_id": "t-anita", "kind": "q_and_a",
        "task_title_rendered": "MJ Decide on Anita invite",
    }
    state_path = _seed_questions(cfg, [question])
    graph = MagicMock()
    graph.mark_task_done.return_value = (True, "MJ Decide on Anita invite")
    intents = [{
        "type": "close_task", "target_text": "anita stuff done",
        "targets": [{"id": "t-anita", "title": "MJ Decide on Anita invite"}],
        "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[question])
    assert rows[0]["answered_by_close"] == ["q-anita"]
    assert load_questions(state_path)["questions"] == []


def test_close_partial_failure_reports_honestly(tmp_path) -> None:
    cfg = _config(tmp_path)
    _seed_questions(cfg, [])
    graph = MagicMock()
    graph.mark_task_done.side_effect = [(True, "a"), RuntimeError("graph 503")]
    intents = [{
        "type": "close_task", "target_text": "both",
        "targets": [{"id": "t1", "title": "MJ A"}, {"id": "t2", "title": "MJ B"}],
        "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[])
    assert rows[0]["result"] == "partial_failure"
    assert "MJ B" in rows[0]["detail"]


# ---- qa_keep / qa_drop ------------------------------------------------------


def test_qa_keep_strips_provisional_prefix_and_pops_state(tmp_path) -> None:
    cfg = _config(tmp_path)
    question = {
        "id": "q1", "task_id": "task-1", "kind": "q_and_a",
        "task_title_rendered": "MJ Decide on PEPS volunteering",
    }
    state_path = _seed_questions(cfg, [question])
    graph = MagicMock()
    intents = [{
        "type": "qa_keep", "target_text": "keep the PEPS one",
        "targets": [{"id": "q1", "title": "MJ Decide on PEPS volunteering"}],
        "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[question])
    graph.update_todo_task.assert_called_once_with(
        "LIST", "task-1", {"title": "MJ Decide on PEPS volunteering"},
    )
    assert rows[0]["result"] == "success"
    assert load_questions(state_path)["questions"] == []


def test_qa_drop_completes_provisional_task(tmp_path) -> None:
    cfg = _config(tmp_path)
    question = {
        "id": "q1", "task_id": "task-1", "kind": "q_and_a",
        "task_title_rendered": "MJ Decide on Parent Association meeting",
    }
    _seed_questions(cfg, [question])
    graph = MagicMock()
    intents = [{
        "type": "qa_drop", "target_text": "drop the parent association meeting",
        "targets": [{"id": "q1", "title": "MJ Decide on Parent Association meeting"}],
        "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[question])
    graph.update_todo_task.assert_called_once_with(
        "LIST", "task-1", {"status": "completed"},
    )
    assert rows[0]["result"] == "success"


def test_qa_keep_examples_proposal_writes_household_example(tmp_path) -> None:
    cfg = _config(tmp_path)
    household = tmp_path / "household.md"
    household.write_text(
        "### Examples table\n| a | b | c | d |\n### Adding an example\n"
    )
    question = {
        "id": "q-ex", "kind": "examples_proposal",
        "target_pattern": "Boonli noreply", "correction_type": "false_positive",
        "action": "skip", "owner": "Megha", "signals": "auto-archived",
        "task_title_rendered": "Examples: Boonli noreply",
    }
    _seed_questions(cfg, [question])
    graph = MagicMock()
    intents = [{
        "type": "qa_keep", "target_text": "yes add it",
        "targets": [{"id": "q-ex", "title": "Examples: Boonli noreply"}],
        "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[question])
    assert rows[0]["result"] == "success"
    assert "Boonli noreply" in household.read_text()
    promoted = (tmp_path / "promoted.jsonl").read_text()
    assert "Boonli noreply" in promoted
    graph.update_todo_task.assert_not_called()


def test_qa_drop_examples_proposal_records_rejection(tmp_path) -> None:
    cfg = _config(tmp_path)
    question = {
        "id": "q-ex", "kind": "examples_proposal",
        "target_pattern": "RR newsletter", "correction_type": "missed",
        "task_title_rendered": "Examples: RR newsletter",
    }
    _seed_questions(cfg, [question])
    intents = [{
        "type": "qa_drop", "target_text": "no skip it",
        "targets": [{"id": "q-ex", "title": "Examples: RR newsletter"}],
        "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=MagicMock(), pending_questions=[question])
    assert rows[0]["result"] == "success"
    assert "RR newsletter" in (tmp_path / "rejected.jsonl").read_text()


# ---- create / delete / steering / correction --------------------------------


def test_create_task_uses_sender_owner(tmp_path) -> None:
    cfg = _config(tmp_path)
    graph = MagicMock()
    graph.create_todo_task.return_value = "new-id"
    intents = [{
        "type": "create_task", "target_text": "buy ferry tickets for Friday",
        "targets": [], "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[],
                sender="max", free_text="add a task to buy ferry tickets for Friday")
    assert rows[0]["result"] == "success"
    payload = graph.create_todo_task.call_args.args[1]
    assert payload["owner"] == "max"


def test_delete_task_completes_target(tmp_path) -> None:
    cfg = _config(tmp_path)
    graph = MagicMock()
    intents = [{
        "type": "delete_task", "target_text": "delete the dentist task",
        "targets": [{"id": "t-d", "title": "MJ Schedule dentist"}],
        "confidence": "high",
    }]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[])
    graph.update_todo_task.assert_called_once_with("LIST", "t-d", {"status": "completed"})
    assert rows[0]["result"] == "success"


def test_pause_sets_quiet_flag(tmp_path) -> None:
    cfg = _config(tmp_path)
    state_path = _seed_questions(cfg, [])
    intents = [{"type": "pause", "target_text": "be quiet", "targets": [],
                "confidence": "high"}]
    rows = _run(intents, config=cfg, graph=MagicMock(), pending_questions=[])
    assert rows[0]["result"] == "success"
    from kavi_runtime.runtime.guardrails import is_paused
    assert is_paused(state_path) is True


def test_pause_time_boxes_the_quiet_window(tmp_path) -> None:
    # Regression for the 2026-06-27 stuck pause: a quiet window MUST carry a
    # paused_until so the scheduler can auto-resume it.
    cfg = _config(tmp_path)
    state_path = _seed_questions(cfg, [])
    intents = [{"type": "pause", "target_text": "go quiet for an hour",
                "targets": [], "confidence": "high"}]
    rows = _run(intents, config=cfg, graph=MagicMock(), pending_questions=[],
                free_text="go quiet for an hour")
    assert rows[0]["result"] == "success"
    from kavi_runtime.runtime.guardrails import get_pause_state
    ps = get_pause_state(state_path)
    assert ps["paused_reason"] == "user_requested_quiet"
    assert ps["paused_until"] is not None


def test_resume_no_op_when_not_paused(tmp_path) -> None:
    cfg = _config(tmp_path)
    _seed_questions(cfg, [])
    intents = [{"type": "resume", "target_text": "I'm back", "targets": [],
                "confidence": "high"}]
    rows = _run(intents, config=cfg, graph=MagicMock(), pending_questions=[])
    assert rows[0]["result"] == "no_op"
    assert rows[0].get("reply_owned") is None


def test_resume_when_paused_delegates_and_owns_reply(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    state_path = _seed_questions(cfg, [])
    from kavi_runtime.runtime.guardrails import set_paused
    set_paused(state_path, "user_requested_quiet")
    called = {}
    from kavi_runtime.runtime import pause as pause_mod
    monkeypatch.setattr(
        pause_mod, "_handle_resume",
        lambda sp, c, ir: called.setdefault("resume", True) or {"status": "resume"},
    )
    intents = [{"type": "resume", "target_text": "ok I'm back", "targets": [],
                "confidence": "high"}]
    rows = _run(intents, config=cfg, graph=MagicMock(), pending_questions=[])
    assert called.get("resume") is True
    assert rows[0]["result"] == "success"
    assert rows[0]["reply_owned"] is True


def test_correction_appends_learning_row(tmp_path) -> None:
    cfg = _config(tmp_path)
    intents = [{"type": "correction", "target_text": "skip Scholastic going forward",
                "targets": [], "confidence": "high"}]
    rows = _run(intents, config=cfg, graph=MagicMock(), pending_questions=[],
                free_text="skip the Scholastic one going forward")
    assert rows[0]["result"] == "success"
    row = json.loads((tmp_path / "corrections.jsonl").read_text().splitlines()[0])
    assert "Scholastic" in row["free_text"]


def test_undo_and_rename_report_not_supported(tmp_path) -> None:
    cfg = _config(tmp_path)
    intents = [
        {"type": "undo", "target_text": "undo that", "targets": [], "confidence": "high"},
        {"type": "rename_task", "target_text": "reword it",
         "targets": [{"id": "t1", "title": "MJ Look at this"}], "confidence": "high"},
    ]
    rows = _run(intents, config=cfg, graph=MagicMock(), pending_questions=[])
    assert [r["result"] for r in rows] == ["not_supported", "not_supported"]


# ---- multi-intent all-execute -----------------------------------------------


def test_multi_intent_all_execute_even_when_one_raises(tmp_path) -> None:
    """The all-execute invariant: every intent gets a result row; an
    executor exception is recorded, not propagated, and the remaining
    intents still execute (no first-match-wins, no silent swallow)."""
    cfg = _config(tmp_path)
    question = {
        "id": "q1", "task_id": "task-1", "kind": "q_and_a",
        "task_title_rendered": "MJ Decide on PEPS",
    }
    _seed_questions(cfg, [question])
    graph = MagicMock()
    graph.mark_task_done.side_effect = RuntimeError("boom")
    graph.create_todo_task.return_value = "new-id"
    intents = [
        {"type": "close_task", "target_text": "close harper",
         "targets": [{"id": "t-m", "title": "MJ Prep Harper"}], "confidence": "high"},
        {"type": "qa_drop", "target_text": "drop PEPS",
         "targets": [{"id": "q1", "title": "MJ Decide on PEPS"}], "confidence": "high"},
        {"type": "create_task", "target_text": "book sitter", "targets": [],
         "confidence": "high"},
    ]
    rows = _run(intents, config=cfg, graph=graph, pending_questions=[question])
    assert len(rows) == 3
    assert rows[0]["result"] == "failure"
    assert rows[1]["result"] == "success"
    assert rows[2]["result"] == "success"


def test_clarify_and_conversational_execute_nothing(tmp_path) -> None:
    cfg = _config(tmp_path)
    intents = [
        {"type": "clarify", "target_text": "the school thing",
         "targets": [{"id": "t1", "title": "MJ Maple forms"}], "confidence": "high"},
        {"type": "conversational", "target_text": "hey", "targets": [],
         "confidence": "high"},
    ]
    rows = _run(intents, config=cfg, graph=_Sentinel(), pending_questions=[])
    assert rows == []


# ---- dry-run: zero Graph calls, zero state writes ---------------------------


def test_dry_run_makes_zero_graph_calls_and_zero_state_writes(tmp_path) -> None:
    """The synthetic endpoints' contract: dry-run resolution is real,
    mutation is simulated. The graph object is a sentinel that raises on
    ANY attribute access; the questions state file must be unchanged."""
    cfg = _config(tmp_path)
    question = {
        "id": "q-anita", "task_id": "t-anita", "kind": "q_and_a",
        "task_title_rendered": "MJ Decide on Anita invite",
    }
    state_path = _seed_questions(cfg, [question])
    before = load_questions(state_path)
    intents = [
        {"type": "close_task", "target_text": "anita done",
         "targets": [{"id": "t-anita", "title": "MJ Decide on Anita invite"}],
         "confidence": "high"},
        {"type": "qa_drop", "target_text": "drop it",
         "targets": [{"id": "q-anita", "title": "MJ Decide on Anita invite"}],
         "confidence": "high"},
        {"type": "create_task", "target_text": "new thing", "targets": [],
         "confidence": "high"},
        {"type": "delete_task", "target_text": "kill it",
         "targets": [{"id": "t-anita", "title": "MJ Decide on Anita invite"}],
         "confidence": "high"},
        {"type": "pause", "target_text": "quiet", "targets": [], "confidence": "high"},
        {"type": "resume", "target_text": "back", "targets": [], "confidence": "high"},
        {"type": "correction", "target_text": "skip it", "targets": [],
         "confidence": "high"},
        {"type": "coordination_reply", "target_text": "ask max", "targets": [],
         "confidence": "high"},
    ]
    rows = execute_intents(
        intents,
        config=cfg,
        graph=_Sentinel(),
        claude=_Sentinel(),
        list_id="dry-run",
        sender="megha",
        sender_handle=None,
        pending_questions=[question],
        free_text="x",
        dry_run=True,
    )
    assert len(rows) == 8
    assert all(r["simulated"] is True for r in rows)
    assert all(r["result"] == "success" for r in rows)
    # answered-by-close resolution still computed in dry-run...
    assert rows[0]["answered_by_close"] == ["q-anita"]
    # ...but nothing was written.
    assert load_questions(state_path) == before
    assert not (tmp_path / "corrections.jsonl").exists()
