"""Deterministic executors for the intent-first iMessage dispatch
(PM-approved rebuild, 2026-06-10).

The LLM parser (`reply_intent_parser.py`) owns judgment; this module owns
mutation. EVERY intent in the parsed list executes (no first-match-wins —
the swallowed-maple-intent class). Executors NEVER compose user-facing
text; the reply composer narrates the returned results.

Dry-run contract (the synthetic verify endpoint): with `dry_run=True`,
target resolution runs for real against the payload's open_tasks /
pending_questions, and every Graph/state mutation is SIMULATED — no Graph
calls, no state writes, no sends. Each simulated row carries
`simulated: True, result: "success"`.

Result row shape:
    {"intent_type": str, "target_ids": [str], "target_titles": [str],
     "simulated": bool, "result": "success"|"failure"|"already_completed"|
     "not_supported"|"no_op", "detail": str (optional),
     "answered_by_close": [question_id] (close_task only, optional),
     "reply_owned": bool (optional — the executor's delegate already sent
     the user-facing reply; the final composer skips it when every
     executed row is reply_owned)}
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from kavi_runtime.state import append_correction, utc_now_iso  # noqa: F401
from kavi_runtime.state_per_concept import load_questions, save_questions

logger = logging.getLogger(__name__)


def _state_path(config: dict) -> Path | None:
    raw = (config.get("paths") or {}).get("imessage_state")
    return Path(raw) if raw else None


def _pop_question_from_state(config: dict, question_id: str) -> None:
    """Remove one pending question by id (atomic per save)."""
    state_path = _state_path(config)
    if state_path is None:
        return
    qstate = load_questions(state_path)
    qstate["questions"] = [
        q for q in qstate.get("questions", []) if q.get("id") != question_id
    ]
    save_questions(state_path, qstate)


def _row(
    intent_type: str,
    targets: list[dict[str, str]],
    *,
    result: str,
    simulated: bool,
    detail: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "intent_type": intent_type,
        "target_ids": [t.get("id") for t in targets if t.get("id")],
        "target_titles": [t.get("title", "") for t in targets],
        "simulated": simulated,
        "result": result,
    }
    if detail:
        row["detail"] = detail
    row.update(extra)
    return row


def _questions_answered_by_close(
    task_id: str, task_title: str, pending_questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pending questions resolved as a side effect of closing their task
    (canonical-direction rule: close beats keep/drop; the question is
    answered-by-close, never re-asked)."""
    out = []
    for q in pending_questions:
        q_title = q.get("task_title_rendered") or q.get("task_title") or ""
        if (q.get("task_id") and q.get("task_id") == task_id) or (
            q_title and q_title == task_title
        ):
            out.append(q)
    return out


def _execute_close(
    intent: dict[str, Any], *, config: dict, graph: Any, list_id: str,
    pending_questions: list[dict[str, Any]], dry_run: bool,
) -> dict[str, Any]:
    answered: list[str] = []
    failures: list[str] = []
    for t in intent["targets"]:
        tid, title = t.get("id"), t.get("title", "")
        if not dry_run:
            try:
                ok, _post_title = graph.mark_task_done(list_id, tid)
                if not ok:
                    failures.append(title or tid)
                    continue
            except Exception as e:
                logger.exception("close_task executor: mark_task_done failed id=%s: %s", (tid or "")[:12], e)
                failures.append(title or tid)
                continue
        for q in _questions_answered_by_close(tid, title, pending_questions):
            qid = q.get("id")
            if qid:
                answered.append(qid)
                if not dry_run:
                    _pop_question_from_state(config, qid)
    result = "success" if not failures else (
        "failure" if len(failures) == len(intent["targets"]) else "partial_failure"
    )
    row = _row(
        "close_task", intent["targets"], result=result, simulated=dry_run,
        detail=(f"failed: {', '.join(failures)}" if failures else None),
    )
    if answered:
        row["answered_by_close"] = answered
    return row


def _execute_qa(
    intent: dict[str, Any], *, config: dict, graph: Any, list_id: str,
    pending_questions: list[dict[str, Any]], dry_run: bool,
) -> dict[str, Any]:
    """qa_keep / qa_drop. Dispatches by question kind:
    - q_and_a: keep -> strip the [?] (title PATCH to rendered);
               drop -> complete the provisional task.
    - examples_proposal: keep -> household example + promoted pattern;
                         drop -> rejected pattern.
    No template acks — the reply composer narrates the result.
    """
    keep = intent["type"] == "qa_keep"
    q_by_id = {q.get("id"): q for q in pending_questions}
    failures: list[str] = []
    for t in intent["targets"]:
        qid = t.get("id")
        question = q_by_id.get(qid)
        if question is None:
            failures.append(t.get("title") or qid or "?")
            continue
        if dry_run:
            continue
        kind = question.get("kind", "q_and_a")
        try:
            if kind == "examples_proposal":
                _resolve_examples_proposal(question, keep, config)
            else:
                task_id = question.get("task_id")
                rendered = (
                    question.get("task_title_rendered")
                    or question.get("task_title") or "(no title)"
                )
                if keep:
                    graph.update_todo_task(list_id, task_id, {"title": rendered})
                else:
                    graph.update_todo_task(list_id, task_id, {"status": "completed"})
            _pop_question_from_state(config, qid)
        except Exception as e:
            logger.exception("qa executor: resolution failed qid=%s: %s", (qid or "")[:12], e)
            failures.append(t.get("title") or qid or "?")
    result = "success" if not failures else (
        "failure" if len(failures) == len(intent["targets"]) else "partial_failure"
    )
    return _row(
        intent["type"], intent["targets"], result=result, simulated=dry_run,
        detail=(f"failed: {', '.join(failures)}" if failures else None),
    )


def _resolve_examples_proposal(question: dict[str, Any], keep: bool, config: dict) -> None:
    """Examples-promotion resolution (formerly _handle_examples_proposal_reply,
    minus its template ack send — the reply composer owns the narration)."""
    from kavi_runtime.state import append_promoted_pattern, append_rejected_pattern
    from capabilities.kavi_persona.qa_loop.handler import _append_household_example

    pattern = question.get("target_pattern", "")
    correction_type = question.get("correction_type", "")
    action = question.get("action", "create")
    owner = question.get("owner", "Megha")
    signals = question.get("signals", "")
    if keep:
        household_path = Path(config["paths"]["household_md"])
        promoted_path = Path(config["paths"]["promoted_patterns_jsonl"])
        _append_household_example(
            household_path, trigger=pattern, action=action, owner=owner,
            signals=signals,
        )
        append_promoted_pattern(promoted_path, {
            "type": correction_type,
            "target_pattern": pattern,
            "action": action,
            "owner": owner,
        })
    else:
        rejected_path = Path(config["paths"]["rejected_patterns_jsonl"])
        append_rejected_pattern(rejected_path, {
            "type": correction_type,
            "target_pattern": pattern,
        })


def _execute_create(
    intent: dict[str, Any], *, config: dict, graph: Any, list_id: str,
    sender: str, free_text: str, dry_run: bool,
) -> dict[str, Any]:
    # Prefer the parser's extracted clean title; fall back to the verbatim
    # span only when extraction is absent (intent-extraction bug class:
    # the raw instruction "create a task with the title X" must not become
    # the title). Same for owner (content-derived, not sender-default) and
    # the resolved due date.
    title = (intent.get("task_title") or intent.get("target_text") or "").strip()[:120]
    if not title:
        return _row("create_task", [], result="failure", simulated=dry_run,
                    detail="empty title")
    owner = intent.get("owner")
    if owner not in ("megha", "max"):
        owner = "megha" if sender != "max" else "max"
    due = intent.get("due") or None
    if dry_run:
        return _row("create_task",
                    [{"id": "simulated-new", "title": title}],
                    result="success", simulated=True,
                    detail=f"owner={owner} due={due or 'none'}")
    try:
        task_obj: dict[str, Any] = {
            "title": title, "owner": owner, "source_tag": "Manual",
            "owner_reason": "created via intent dispatch",
        }
        if due:
            task_obj["due"] = due
        new_id = graph.create_todo_task(
            list_id,
            task_obj,
            source_email_id=f"intent-{uuid.uuid4().hex[:8]}",
            source_subject=f"iMessage: {free_text[:80]}",
        )
        return _row("create_task", [{"id": new_id, "title": title}],
                    result="success", simulated=False)
    except Exception as e:
        logger.exception("create_task executor failed: %s", e)
        return _row("create_task", [{"id": "", "title": title}],
                    result="failure", simulated=False, detail=str(e)[:200])


def _execute_delete(
    intent: dict[str, Any], *, graph: Any, list_id: str, dry_run: bool,
) -> dict[str, Any]:
    failures: list[str] = []
    for t in intent["targets"]:
        if dry_run:
            continue
        try:
            # Same removal mechanic the legacy false_positive correction used:
            # complete the task (MS To Do has no hard-delete in our wrapper).
            graph.update_todo_task(list_id, t.get("id"), {"status": "completed"})
        except Exception as e:
            logger.exception("delete_task executor failed id=%s: %s", (t.get("id") or "")[:12], e)
            failures.append(t.get("title") or t.get("id") or "?")
    result = "success" if not failures else "failure"
    return _row("delete_task", intent["targets"], result=result, simulated=dry_run,
                detail=(f"failed: {', '.join(failures)}" if failures else None))


def _execute_pause(intent: dict[str, Any], *, config: dict, free_text: str,
                   dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return _row("pause", [], result="success", simulated=True)
    from kavi_runtime.runtime.guardrails import set_paused, compute_pause_until
    state_path = _state_path(config)
    if state_path is None:
        return _row("pause", [], result="failure", simulated=False,
                    detail="no state path configured")
    # Time-box the quiet window from the request text (default: next 7 AM PT,
    # hard-capped at 24h). Without this a "quiet until tomorrow" never expires —
    # the 2026-06-27 stuck-pause that silently gated email->task for 17 days.
    text = free_text or intent.get("target_text") or ""
    paused_until = compute_pause_until(text)
    set_paused(state_path, "user_requested_quiet", paused_until=paused_until)
    return _row("pause", [], result="success", simulated=False,
                detail=f"quiet until {paused_until}")


def _execute_resume(intent: dict[str, Any], *, config: dict, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return _row("resume", [], result="success", simulated=True)
    from kavi_runtime.runtime.guardrails import is_paused
    from kavi_runtime.runtime.pause import _handle_resume
    state_path = _state_path(config)
    if state_path is None:
        return _row("resume", [], result="failure", simulated=False,
                    detail="no state path configured")
    if not is_paused(state_path):
        return _row("resume", [], result="no_op", simulated=False,
                    detail="not paused")
    # _handle_resume owns its own (audited) ack + queued-email drain.
    _handle_resume(state_path, config, {"reason": "intent parser resume"})
    return _row("resume", [], result="success", simulated=False,
                reply_owned=True)


def _execute_correction(
    intent: dict[str, Any], *, config: dict, free_text: str, dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return _row("correction", intent["targets"], result="success", simulated=True)
    try:
        corrections_path = Path(config["paths"]["corrections_jsonl"])
        append_correction(corrections_path, {
            "type": "freeform",
            "target": (intent.get("target_text") or "")[:200],
            "free_text": free_text[:500],
            "action_taken": False,
            "run_id_at_correction": utc_now_iso(),
        })
        return _row("correction", intent["targets"], result="success",
                    simulated=False, detail="logged for the learning loop")
    except Exception as e:
        logger.exception("correction executor failed: %s", e)
        return _row("correction", intent["targets"], result="failure",
                    simulated=False, detail=str(e)[:200])


def _execute_coordination(
    intent: dict[str, Any], *, config: dict, claude: Any, graph: Any,
    sender_handle: str | None, free_text: str, dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return _row("coordination_reply", intent["targets"], result="success",
                    simulated=True)
    # Capability isolation: route via the runtime dispatch entry, never a
    # direct capabilities.coordination import from kavi_persona code.
    from kavi_runtime.runtime.imessage_dispatch import _coordination_dispatch_entry
    try:
        coord_result = _coordination_dispatch_entry(
            free_text=free_text,
            sender_handle=sender_handle,
            claude=claude,
            graph=graph,
            config=config,
        )
    except Exception as e:
        logger.exception("coordination executor failed: %s", e)
        return _row("coordination_reply", intent["targets"], result="failure",
                    simulated=False, detail=str(e)[:200])
    if coord_result is None:
        return _row("coordination_reply", intent["targets"], result="no_op",
                    simulated=False, detail="coordination dispatch declined")
    # The coordination flow sent its own requester ack; the final composer
    # must not double-message when this was the only intent.
    return _row("coordination_reply", intent["targets"], result="success",
                simulated=False, reply_owned=True,
                detail=str(coord_result.get("status", ""))[:120])


def execute_intents(
    intents: list[dict[str, Any]],
    *,
    config: dict,
    graph: Any,
    claude: Any,
    list_id: str,
    sender: str,
    sender_handle: str | None,
    pending_questions: list[dict[str, Any]],
    free_text: str,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Execute EVERY intent in order; return one result row per executed
    intent. `clarify` and `conversational` intents execute nothing (the
    reply composer handles them from the intents list directly)."""
    results: list[dict[str, Any]] = []
    for intent in intents:
        itype = intent.get("type")
        try:
            if itype == "close_task":
                results.append(_execute_close(
                    intent, config=config, graph=graph, list_id=list_id,
                    pending_questions=pending_questions, dry_run=dry_run,
                ))
            elif itype in {"qa_keep", "qa_drop"}:
                results.append(_execute_qa(
                    intent, config=config, graph=graph, list_id=list_id,
                    pending_questions=pending_questions, dry_run=dry_run,
                ))
            elif itype == "create_task":
                results.append(_execute_create(
                    intent, config=config, graph=graph, list_id=list_id,
                    sender=sender, free_text=free_text, dry_run=dry_run,
                ))
            elif itype == "delete_task":
                results.append(_execute_delete(
                    intent, graph=graph, list_id=list_id, dry_run=dry_run,
                ))
            elif itype == "rename_task":
                # v1 limitation: the contract intent shape carries no new
                # title field; the composer asks for it instead of guessing.
                results.append(_row(
                    "rename_task", intent["targets"], result="not_supported",
                    simulated=dry_run, detail="rename needs the new title",
                ))
            elif itype == "undo":
                results.append(_row(
                    "undo", intent["targets"], result="not_supported",
                    simulated=dry_run, detail="undo not wired yet",
                ))
            elif itype == "pause":
                results.append(_execute_pause(
                    intent, config=config, free_text=free_text, dry_run=dry_run,
                ))
            elif itype == "resume":
                results.append(_execute_resume(intent, config=config, dry_run=dry_run))
            elif itype == "correction":
                results.append(_execute_correction(
                    intent, config=config, free_text=free_text, dry_run=dry_run,
                ))
            elif itype == "coordination_reply":
                results.append(_execute_coordination(
                    intent, config=config, claude=claude, graph=graph,
                    sender_handle=sender_handle, free_text=free_text,
                    dry_run=dry_run,
                ))
            # clarify / conversational: no execution by design.
        except Exception as e:
            # One intent's failure must never swallow the rest (the
            # all-execute invariant). Record and continue.
            logger.exception("execute_intents: %s executor raised: %s", itype, e)
            results.append(_row(
                str(itype), intent.get("targets") or [], result="failure",
                simulated=dry_run, detail=str(e)[:200],
            ))
    return results


__all__ = ["execute_intents"]
