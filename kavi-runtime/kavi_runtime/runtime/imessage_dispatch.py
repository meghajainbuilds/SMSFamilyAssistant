"""iMessage dispatch: imessage_received, _imessage_received_impl.

INTENT-FIRST REBUILD (PM-approved 2026-06-10, contract:
evals/kavi-reply/matrix/ENDPOINT_CONTRACT.md). After the NON-SEMANTIC gates
(household-handle allowlist, tapback filter, the coordination
addressee-session lookup — sender identity, not semantics) EVERY inbound
goes to ONE LLM intent parser. The parser's intent list drives the
deterministic executors (ALL intents execute — no first-match-wins), then
ONE final LLM compose reports what happened, G-A1-gated.

DELETED legacy branches (2026-06-10; behavior classes covered by the frozen
matrix at evals/kavi-reply/matrix/matrix-kavi-reply.jsonl):
- classify_qa_reply interception (cases regression-bare-keep-after-qa,
  regression-bare-drop-after-qa, mixed-keep-drop-close-one-message)
- BARE_KEEP_WORDS / BARE_DROP_WORDS conversational-adjacency bindings
  (cases regression-bare-keep-after-qa, bare-yes-binds-to-offer-not-pending-qa)
- pending-fact follow-up binding (case regression-bare-yea-pending-fact-offer)
- MIN_CORRECTION_TEXT_LEN short-text "Got it." branch
  (case conversational-nothing-pending-no-got-it)
- QA_REPLY_PATTERN "1 yes" legacy regex backstop + "Kept:"/"Dropped:"
  template acks (cases incident-2026-06-10-yes-plus-close-all,
  reworded-close-verbs-extract)
- pause-intent classifier branch (cases pause-intent-recognized,
  resume-intent-recognized — the parser owns pause/resume now)
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

# Cross-cutting runtime plumbing.
from kavi_runtime.runtime.clients import _get_clients  # noqa: F401
from kavi_runtime.runtime.send_imessage import (  # noqa: F401
    _send_imessage_with_fallback,
    _send_imessage_with_fallback_and_context,
    send_imessage_raw,
)
from kavi_runtime.state import list_pending_questions, utc_now_iso  # noqa: F401

# Per-capability entry points.
from capabilities.coordination.dispatch import _try_handle_coordination_intent  # noqa: F401
from capabilities.kavi_persona.actions.handler import (  # noqa: F401
    _enforce_g_a1_or_alert_fallback,
)
from capabilities.kavi_persona.intent_executors import execute_intents
from capabilities.kavi_persona.utils import (  # noqa: F401
    _load_recent_inbound,
    _load_recent_outbound,
)

# Late-binding handle so tests that patch handlers._foo flow through:
import kavi_runtime.handlers as _h_mod  # noqa: E402

logger = logging.getLogger(__name__)


# Tapbacks ("Liked ...") carry no language signal; deterministic skip.
REACTION_PATTERN = re.compile(r"^(Liked|Disliked|Loved|Laughed at|Emphasized|Questioned)\s", re.IGNORECASE)
OWNER_ABBREV = {"megha": "MJ", "max": "MM"}
# Retained ONLY as the coordination course-correction pre-gate's minimum
# (a 1-4 char reply to a mid-flight session isn't a course-correction).
# The short-text "Got it." ack branch that this constant used to gate was
# deleted 2026-06-10 (intent-first rebuild).
MIN_CORRECTION_TEXT_LEN = 5

# Cold-fallback safe sentence (CLAUDE.md cold-fallback policy): ships when
# the parser or the reply composer failed after their one retry. No action
# claims, no enumeration. AUDIT 2026-06-10: honest, present-tense, prose,
# under the target length cap.
_REPLY_SAFE_FALLBACK = "Hit a snag reading that — give me a minute and try again?"
_REPLY_SAFE_FALLBACK_PROVENANCE = {"fallback_audit": "2026-06-10"}



# Reply path task fetch. Covers the whole shared list today (about 200 open);
# raise if the list ever grows past it.
OPEN_TASKS_FETCH_LIMIT = 400


def _ensure_question_tasks(graph, list_id, open_tasks, pending_questions):
    """Append the task behind each pending question when the fetch missed
    it. Only still-open tasks are added; lookup failures are skipped."""
    have = {t.get("id") for t in open_tasks}
    extra = []
    for q in pending_questions or []:
        tid = q.get("task_id")
        if not tid or tid in have:
            continue
        try:
            task = graph.get_todo_task(list_id, tid)
        except Exception as e:
            logger.warning("dispatch: pending-question task %s lookup failed: %s", tid[-12:], e)
            continue
        if task.get("status") in ("notStarted", "inProgress"):
            extra.append(task)
            have.add(tid)
    return open_tasks + extra

def _sender_logical_name(sender_handle: str | None, config: dict) -> str:
    """Map an iMessage handle to the logical household sender ('megha' /
    'max'). Mirrors capabilities/kavi_persona/utils._owner_prefix_from_sender
    (MJ default when unknown — Megha is the default requester)."""
    from capabilities.kavi_persona.utils import _owner_prefix_from_sender
    return "max" if _owner_prefix_from_sender(sender_handle, config) == "MM" else "megha"


def imessage_received(payload: dict[str, Any], config: dict) -> dict[str, Any]:
    """BlueBubbles webhook entry. Non-semantic gates, then the intent-first
    parse -> execute -> compose pipeline."""
    event_type = payload.get("type", "")
    data = payload.get("data") or {}
    if event_type != "new-message":
        return {"status": "ignored", "reason": f"event_type={event_type}"}
    if data.get("isFromMe"):
        return {"status": "ignored", "reason": "from_me"}

    text = (data.get("text") or "").strip()
    if not text:
        return {"status": "ignored", "reason": "empty_text"}

    sender_handle = (
        data.get("handle", {}).get("address")
        or data.get("chats", [{}])[0].get("guid")
        if data.get("chats") else None
    )

    # Phase B unified-trace (2026-05-12): one JSONL row per inbound exchange.
    try:
        from kavi_runtime.trace_log import Trace as _Trace
        _trace_ctx = _Trace(
            channel="imessage", participant=sender_handle, config=config,
        )
    except Exception as _e:
        logger.debug("trace_log: Trace construction failed (continuing): %s", _e)
        _trace_ctx = None
    if _trace_ctx is None:
        return _imessage_received_impl(payload, config, data, text, sender_handle)
    with _trace_ctx:
        return _imessage_received_impl(payload, config, data, text, sender_handle)


def _imessage_received_impl(
    payload: dict[str, Any], config: dict,
    data: dict[str, Any], text: str, sender_handle: str | None,
) -> dict[str, Any]:
    """Inner implementation, separated so the outer function can wrap the
    body in a Trace context manager."""

    # ---- NON-SEMANTIC GATE 1: inbound sender allowlist (hard security gate).
    from kavi_runtime.runtime import outbound_scanner
    allowed_sender, _sender_block_reason = outbound_scanner.gate_inbound_sender(
        config=config, sender=sender_handle, text=text,
    )
    if not allowed_sender:
        return {
            "status": "discarded_unknown_sender",
            "sender": sender_handle,
        }

    # Channel heartbeat write-through (2026-06-04). Failure-safe.
    try:
        from kavi_runtime import channel_heartbeat as _hb
        _hb.record_inbound(config, sender_handle or "")
    except Exception:
        logger.debug("channel_heartbeat.record_inbound emit failed (continuing)")

    # ---- NON-SEMANTIC GATE 2: coordination addressee-session lookup
    # (sender identity, not semantics). When an active session awaits a
    # reply from THIS handle, the inbound belongs to that session.
    from capabilities.coordination import handler as coordination_handler
    coordination_handler._ensure_sessions_loaded(config)
    awaiting_session = coordination_handler.lookup_session_by_addressee_handle(
        sender_handle or "",
    )
    if awaiting_session is not None:
        graph_c, claude_c, bb_c = _h_mod._get_clients(config)
        return coordination_handler.handle_addressee_reply(
            session_id=awaiting_session["session_id"],
            reply_text=text,
            config=config,
            claude=claude_c,
            graph=graph_c,
            bb=bb_c,
        )

    # Coordination requester course-correction (2026-05-07, Bug 2). Stateful
    # session gate, kept ahead of the parser: it fires only on a
    # high-confidence course-correction and otherwise falls through.
    requester_session = coordination_handler.lookup_session_by_requester_handle(
        sender_handle or "",
    )
    if requester_session is not None and not REACTION_PATTERN.match(text) and len(text) >= MIN_CORRECTION_TEXT_LEN:
        graph_r, claude_r, bb_r = _h_mod._get_clients(config)
        cc_result = coordination_handler.handle_requester_course_correction(
            session_id=requester_session["session_id"],
            follow_up_text=text,
            config=config,
            claude=claude_r,
            bb=bb_r,
        )
        if cc_result is not None:
            return cc_result
        # else: not a course-correction; fall through.

    # ---- Context capture + inbound log. recent_inbound loads BEFORE
    # log_inbound so the message under dispatch is excluded from its own
    # context.
    recent_inbound = _load_recent_inbound(config, sender_handle, n=5)
    from kavi_runtime.inbound_log import log_inbound, current_inbound_id
    log_inbound(config, text=text, sender=sender_handle, source="imessage")
    try:
        from kavi_runtime.trace_log import current as _trace_current
        _t = _trace_current()
        if _t is not None:
            _t.record_inbound(
                inbound_id=current_inbound_id.get(),
                ts=utc_now_iso(),
                text=text,
                sender=sender_handle,
                source="imessage",
                char_count=len(text),
            )
    except Exception as _trace_err:
        logger.debug("dispatch: trace record_inbound failed (continuing): %s", _trace_err)

    # ---- NON-SEMANTIC GATE 3: tapbacks carry no language signal.
    if REACTION_PATTERN.match(text):
        logger.info("imessage_received: ignoring reaction text=%r", text[:60])
        return {"status": "ignored", "reason": "reaction"}

    # Weekly self-check reply hook: stateful pre-gate (fires only when a
    # pending self-check exists and the classifier confirms; otherwise None).
    from kavi_runtime.runtime.weekly_self_check import try_handle_self_check_reply
    self_check_result = try_handle_self_check_reply(text, config)
    if self_check_result is not None:
        return self_check_result

    # ---- INTENT-FIRST PIPELINE: parse -> execute (all) -> compose (one).
    state_path = Path(config["paths"]["imessage_state"])
    graph, claude, _bb = _h_mod._get_clients(config)
    list_id = config["graph"]["mstodo_shared_list_id"]
    sender = _sender_logical_name(sender_handle, config)

    pending_questions = _h_mod.drop_expired_questions(
        _h_mod.list_pending_questions(state_path)
    )
    recent_outbound = _load_recent_outbound(config, n=5)
    try:
        from capabilities.kavi_persona.selection import (
            _read_pending_facts_for_summary,
        )
        pending_facts = _read_pending_facts_for_summary(config, top_n=3)
    except Exception:
        logger.exception("dispatch: pending-facts read failed (continuing empty)")
        pending_facts = []
    try:
        open_tasks = graph.list_open_todo_tasks(list_id, top=OPEN_TASKS_FETCH_LIMIT)
    except Exception as e:
        logger.exception("dispatch: list_open_todo_tasks failed (continuing empty): %s", e)
        open_tasks = []
    # Every task a pending question is about must be targetable, however
    # old (2026-09-25: stale-nudge tasks fell off the fetch and "close them"
    # closed three other tasks).
    open_tasks = _ensure_question_tasks(graph, list_id, open_tasks, pending_questions)
    try:
        open_sessions = coordination_handler.list_open_sessions(config)
    except Exception:
        logger.exception("dispatch: coordination session list failed (continuing empty)")
        open_sessions = []

    intents = claude.parse_reply_intents(
        inbound_text=text,
        sender=sender,
        recent_outbound=recent_outbound,
        recent_inbound=recent_inbound,
        pending_questions=pending_questions,
        pending_facts=pending_facts,
        open_tasks=open_tasks,
        open_coordination_sessions=open_sessions,
    )
    if intents is None:
        # Parser failed after its one retry. Cold-fallback policy: ONE safe
        # sentence, no action claims, never silence.
        logger.warning("dispatch: intent parser failed; sending safe sentence")
        _h_mod._send_imessage_with_fallback(
            config, _REPLY_SAFE_FALLBACK, kind="conversational",
            provenance=_REPLY_SAFE_FALLBACK_PROVENANCE,
        )
        return {"status": "intent_parse_failed", "reply": _REPLY_SAFE_FALLBACK}

    executed = execute_intents(
        intents,
        config=config,
        graph=graph,
        claude=claude,
        list_id=list_id,
        sender=sender,
        sender_handle=sender_handle,
        pending_questions=pending_questions,
        free_text=text,
        dry_run=False,
    )

    # Delegated flows (coordination initiation, resume drain) own their own
    # user-facing reply. When EVERY executed row is reply-owned and no
    # clarify/conversational intent needs a reply, skip the final compose to
    # avoid double-messaging.
    reply_owned_rows = [r for r in executed if r.get("reply_owned")]
    # Coordination owns the requester-facing turn (it sent its own ack and
    # will report the outcome). When a coordination_reply is in the turn, a
    # SEPARATE non-reply-owned mutation (e.g. "...and add a task for him",
    # which the coordination outcome already covers) must NOT trigger a
    # second narration on top of the ack — that double-message is the
    # 2026-06-11 SBP/Hollis "Pinged Max... Task added for him too." bug.
    # Only a genuine clarify/conversational intent still earns its own reply.
    coordination_owned = any(
        r.get("reply_owned") and r.get("intent_type") == "coordination_reply"
        for r in executed
    )
    has_interactive_intent = any(
        i.get("type") in {"clarify", "conversational"} for i in intents
    )
    needs_compose = bool(
        has_interactive_intent
        or (not coordination_owned
            and [r for r in executed if not r.get("reply_owned")])
    )
    if executed and reply_owned_rows and not needs_compose:
        return {
            "status": "intent_dispatch",
            "intents": intents,
            "executed": executed,
            "reply": None,
            "reply_owned_by_executor": True,
        }

    reply = claude.compose_kavi_reply(
        inbound_text=text,
        sender=sender,
        intents=intents,
        executed=executed,
        recent_outbound=recent_outbound,
        recent_inbound=recent_inbound,
        pending_facts=pending_facts,
        open_coordination_sessions=open_sessions,
    )
    reply_provenance: dict[str, str] = {"llm_call": "compose_kavi_reply"}
    if reply is None:
        # Composer failed (its module already enforced shape + one parse).
        # Cold-fallback policy: one safe sentence, no action claims.
        logger.warning("dispatch: reply composer failed; sending safe sentence")
        reply = _REPLY_SAFE_FALLBACK
        reply_provenance = dict(_REPLY_SAFE_FALLBACK_PROVENANCE)

    # G-A1 grounding: real execution results back any action verbs.
    real_actions = [
        {
            "task_id": (r.get("target_ids") or [""])[0] if r.get("target_ids") else "",
            "action_type": r.get("intent_type"),
            "result": r.get("result"),
            "target_title": (r.get("target_titles") or [""])[0] if r.get("target_titles") else "",
        }
        for r in executed
    ]
    g_a1_kind = "post_action_reply" if real_actions else "conversational"
    g_a1_context = {"actions_executed": real_actions} if real_actions else None
    safe_reply, safe_kind, safe_ctx = _enforce_g_a1_or_alert_fallback(
        config, reply, kind=g_a1_kind, context=g_a1_context,
    )
    if safe_kind == "alert_fallback":
        # AUDIT 2026-06-10: G-A1 substituted the deterministic alert sentence.
        reply_provenance = {"fallback_audit": "2026-06-10"}
    if safe_ctx is not None:
        _h_mod._send_imessage_with_fallback_and_context(
            config, safe_reply, kind=safe_kind, context=safe_ctx,
            provenance=reply_provenance,
        )
    else:
        _h_mod._send_imessage_with_fallback(
            config, safe_reply, kind=safe_kind, provenance=reply_provenance,
        )

    return {
        "status": "intent_dispatch",
        "intents": intents,
        "executed": executed,
        "reply": safe_reply,
    }


def _coordination_dispatch_entry(*args, **kwargs):
    """Cross-cap dispatch entry point. kavi_persona executors route
    coordination intents through the runtime, not via a direct
    capabilities.coordination import (capability isolation)."""
    return _try_handle_coordination_intent(*args, **kwargs)


__all__ = [
    "imessage_received",
    "_imessage_received_impl",
]
