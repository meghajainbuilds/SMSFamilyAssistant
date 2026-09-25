"""Capability handler functions: _enforce_g_a1_or_alert_fallback, _send_action_layer_reply, _compose_action_clarifying_reply, _save_pending_clarification_for_action, _try_resolve_pending_clarification, _try_anchor_then_sweep, _topic_tokens, _share_topic_keyword, _resolve_candidate_ids, _try_handle_action_intent, _handle_correction.

Phase 4 (2026-06-02) physical move from kavi-runtime/kavi_runtime/handlers.py.
This module owns the function bodies; handlers.py star-imports them for
backward compatibility with in-process callers.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kavi_runtime.bluebubbles_client import BlueBubblesClient
from kavi_runtime.claude_client import ClaudeClient
from kavi_runtime.graph_client import GraphClient
from kavi_runtime import lifecycle as lifecycle_module
from kavi_runtime import package_extractor
from collections import defaultdict

# REMOVED CROSS-CAP IMPORT (unused): from kavi_runtime.runtime.guardrails import (
# REMOVED CROSS-CAP IMPORT (unused):     clear_paused,
# REMOVED CROSS-CAP IMPORT (unused):     compute_monthly_spend_usd,
# REMOVED CROSS-CAP IMPORT (unused):     drain_pending_alerts,
# REMOVED CROSS-CAP IMPORT (unused):     enqueue_paused_email,
# REMOVED CROSS-CAP IMPORT (unused):     enqueue_pending_alert,
# REMOVED CROSS-CAP IMPORT (unused):     get_pause_state,
# REMOVED CROSS-CAP IMPORT (unused):     is_paused,
# REMOVED CROSS-CAP IMPORT (unused):     is_spend_cap_bypassed,
# REMOVED CROSS-CAP IMPORT (unused):     log_trip,
# REMOVED CROSS-CAP IMPORT (unused):     set_paused,
# REMOVED CROSS-CAP IMPORT (unused): )
from kavi_runtime.state import (
    add_pending_question,
    append_correction,
    append_promoted_pattern,
    append_rejected_pattern,
    append_run,
    clear_pending_clarification,
    clear_summary_anchor,
    drain_summary_queue,
    enqueue_summary_item,
    is_quiet_hours,
    list_pending_questions,
    load_pending_clarification,
    load_summary_anchor,
    pop_pending_question,
    read_jsonl,
    read_rejected_patterns,
    read_unapplied_corrections,
    save_pending_clarification,
    save_summary_anchor,
    utc_now_iso,
)
from kavi_runtime.state_per_concept import (
    load_pause_state,
    load_questions,
    save_questions,
)

# Cross-cutting runtime plumbing — physically moved to kavi_runtime/runtime/
# in Phase 4 (2026-06-02). Imported back here so the in-file callers in
# handlers.py (still mid-refactor) keep working transparently.
from kavi_runtime.runtime.clients import _get_clients
from kavi_runtime.runtime.dedup import (
    _dedup_check,
    _dedup_record,
    _per_message_lock,
)
from kavi_runtime.runtime.ids import _decision_id
from kavi_runtime.runtime.paths import _runtime_events_path
from kavi_runtime.runtime.send_imessage import (
    _send_imessage_with_fallback,
    _send_imessage_with_fallback_and_context,
    send_imessage_raw,
)
from kavi_runtime.runtime.spend_cap import _check_spend_cap_after_call
from kavi_runtime.runtime.alerts import _send_or_queue_alert
from kavi_runtime.runtime.time_utils import _latency_sec

# Per-capability entry points (physically moved to capability dirs in Phase 4).
# Imported back so handlers.py's in-file callers keep working.
# Cross-cap dispatch goes through the runtime imessage dispatch layer to
# preserve capability isolation; the coordination dispatcher itself still
# lives in capabilities/coordination/dispatch.py (its canonical home).
# Lazy import to avoid the circular kavi_runtime.runtime.imessage_dispatch -> here.
def _try_handle_coordination_intent(*args, **kwargs):
    from kavi_runtime.runtime.imessage_dispatch import _coordination_dispatch_entry
    return _coordination_dispatch_entry(*args, **kwargs)

# inbox-to-task helpers physically moved to capabilities/inbox_to_task/ in
# Phase 4 (2026-06-02). Imported back so handlers.py's in-file callers
# (`_email_arrived_impl`, etc.) keep working transparently.
# REMOVED CROSS-CAP IMPORT (unused): from capabilities.inbox_to_task.selection import (  # noqa: F401
# REMOVED CROSS-CAP IMPORT (unused):     _normalize_email,
# REMOVED CROSS-CAP IMPORT (unused):     _build_thread_state,
# REMOVED CROSS-CAP IMPORT (unused):     _inbox_owner_abbrev,
# REMOVED CROSS-CAP IMPORT (unused):     _account_to_owner_name,
# REMOVED CROSS-CAP IMPORT (unused): )
# REMOVED CROSS-CAP IMPORT (unused): from capabilities.inbox_to_task.task_writer import (  # noqa: F401
# REMOVED CROSS-CAP IMPORT (unused):     _apply_lifecycle_update,
# REMOVED CROSS-CAP IMPORT (unused):     _resolve_shared_list_id,
# REMOVED CROSS-CAP IMPORT (unused): )
# REMOVED CROSS-CAP IMPORT (unused): from capabilities.inbox_to_task.eval_log import (  # noqa: F401
# REMOVED CROSS-CAP IMPORT (unused):     _DECISION_MAP,
# REMOVED CROSS-CAP IMPORT (unused):     _eval_inbox_judgments_path,
# REMOVED CROSS-CAP IMPORT (unused):     _log_email_event,
# REMOVED CROSS-CAP IMPORT (unused):     _log_webhook_redup_hit,
# REMOVED CROSS-CAP IMPORT (unused): )

# kavi_persona helpers physically moved to capabilities/kavi_persona/ in
# Phase 4 (2026-06-02). Imported back so handlers.py's in-file callers
# (`_imessage_received_impl`, `_periodic_summary_impl`, etc.) keep
# working transparently.
from capabilities.kavi_persona.selection import (  # noqa: F401
    _fetch_open_todo_task_ids,
    _filter_summary_queue_by_open_status,
    _filter_pending_questions_by_open_status,
    _count_pending_facts,
    _read_pending_facts_for_summary,
    _pick_summary_anchor,
)
from capabilities.kavi_persona.composers.periodic_summary import (  # noqa: F401
    periodic_summary,
    _periodic_summary_impl,
    _PERIODIC_SUMMARY_SAFE_FALLBACK,
    PERIODIC_SUMMARY_DEBOUNCE_HOURS,
    _periodic_summary_state_path,
    _periodic_summary_input_hash,
    _load_periodic_summary_last_hash,
    _save_periodic_summary_last_hash,
    _should_suppress_periodic_summary,
)
from capabilities.kavi_persona.actions.title_edits import (  # noqa: F401


    _strip_kavi_prefix,
    _swap_owner_in_title,
    _replace_body_in_title,
    _replace_tag_in_title,
    _adjust_confidence_marker,
)

# Late-binding handle so tests that patch handlers._foo flow through:
import kavi_runtime.handlers as _h_mod  # noqa: E402

# Sibling utils used by multiple action/qa handler functions:
from capabilities.kavi_persona.utils import (  # noqa: E402, F401
    _owner_prefix_from_sender,
    _log_action_intent_decision,
    _load_recent_outbound,
    _parse_deadline_iso,
)
from capabilities.kavi_persona.actions.create_task import (  # noqa: E402, F401
    _handle_create_task_verb,
)


logger = logging.getLogger(__name__)


# Module-level constants used by the action-layer handlers. Sourced from the
# pre-Phase-4 handlers.py module.
_G_A1_ALERT_FALLBACK_TEXT = (
    "I caught myself about to claim that as done. Re-ask as a "
    "direct request and I'll route it."
)
G_A1_FAIL_CLOSED_KINDS = frozenset({
    "conversational",
    "post_action_reply",
    "qa_ack",
})
G_A1_STATE_CLAIM_FAIL_CLOSED_KINDS = frozenset({
    "action_clarifying",
})
_TOPIC_STOPWORDS = frozenset({
    "the", "and", "with", "from", "this", "that", "for",
    "your", "have", "into", "about", "yes", "marked",
    "done", "task", "tasks",
})
OWNER_ABBREV = {"megha": "MJ", "max": "MM"}
MIN_CORRECTION_TEXT_LEN = 5





def _enforce_g_a1_or_alert_fallback(
    config: dict,
    text: str,
    *,
    kind: str,
    context: dict[str, Any] | None,
) -> tuple[str, str, dict[str, Any] | None]:
    """G-A1 fail-closed gate (Fix 1, 2026-05-08): if `text` contains an action
    verb without a verified tool attempt in `context`, replace the outgoing
    payload with the deterministic alert_fallback message so Megha never
    reads a fabricated "marked it done" claim.

    Two-tier fail-closed (refined 2026-05-08 second pass):

    1. `G_A1_FAIL_CLOSED_KINDS` (broad action-verb gate, `passes_g_a1`):
       - `conversational`, `post_action_reply`, `qa_ack`
       - Any action verb without a verified tool result fails closed.

    2. `G_A1_STATE_CLAIM_FAIL_CLOSED_KINDS` (narrower past-tense state-claim
       gate, `text_contains_forbidden_clarify_state_claim`):
       - `action_clarifying`
       - Clarifying replies legitimately use action verbs in question form
         ("Mark done?"); the broad gate would over-fire. The narrower gate
         catches the Elders' Tea production failure pattern: a clarifying
         composer fabricating "X is already marked done" — a past-tense
         state claim with no tool result behind it.

    For other kinds (e.g. `coordination_outcome`, `correction_ack`), G-A1
    is log-only — the violation lands on the structural_checks field of
    the outbound row but the message ships.

    Returns `(text, kind, context)` after possible substitution. The
    original hallucinated text is logged to the eval surface (kind=
    `<original_kind>_g_a1_blocked`) but never sent.
    """
    from kavi_runtime.structural_checks import (
        passes_g_a1,
        text_contains_forbidden_clarify_state_claim,
    )
    if kind in G_A1_FAIL_CLOSED_KINDS:
        if passes_g_a1(text, context):
            return text, kind, context
        # Falls through to substitution below.
        block_reason = "g_a1_action_grounded"
    elif kind in G_A1_STATE_CLAIM_FAIL_CLOSED_KINDS:
        if not text_contains_forbidden_clarify_state_claim(text):
            return text, kind, context
        # Past-tense state claim in a clarifying reply — block.
        block_reason = "g_a1_state_claim_in_clarify"
    else:
        # Log-only path. G-A1 still gets evaluated downstream by the
        # outbound logger (which sets g_a1_action_grounded on the row),
        # but we don't substitute the message here.
        return text, kind, context

    logger.warning(
        "G-A1 fail on %s reply (%s): replacing with alert_fallback. Original=%r",
        kind, block_reason, text[:240],
    )
    try:
        from kavi_runtime.outbound_log import log_outbound
        log_outbound(
            config, kind=f"{kind}_g_a1_blocked",
            text=text,
            send_result={"sent": False, "verified": False, "fallback_used": True,
                         "blocked": True, "blocked_reason": block_reason},
            context={"g_a1_blocked": True, "tool_grounded": False,
                     "original_kind": kind},
        )
    except Exception as _log_err:
        logger.warning("g_a1 block log failed (non-fatal): %s", _log_err)
    return _G_A1_ALERT_FALLBACK_TEXT, "alert_fallback", None



def _send_action_layer_reply(
    config: dict,
    reply_text: str,
    *,
    context: dict[str, Any] | None = None,
    provenance: dict[str, str] | None = None,
) -> None:
    """Single send-path for every action-layer reply. The action layer always
    speaks past-tense or asks for clarification; it never narrates pre-action.
    Centralizing the send call keeps the bypass-conversational-composer
    invariant easy to audit: every action-implying inbound exits this function,
    not compose_conversational_reply.

    Kind tagging (refined 2026-05-08): the kind is derived from `context`:
      - context with `actions_executed` rows → `post_action_reply` (real
        post-action ack; G-A1 fail-closed applies — Fix 1).
      - context=None or missing actions_executed → `action_clarifying`
        (pre-action question; G-A1 log-only because clarifying questions
        legitimately use action verbs in question form like "mark done?").

    The Elders' Tea production trace tagged a clarifying-composer
    hallucination as `post_action_reply` because both shared this wrapper.
    That conflation is what let "All Elders' Tea tasks are already marked
    done" ship as a `post_action_reply` and bypass G-A1 fail-closed (which
    only applied to `conversational` then). Now: clarifying paths get their
    own kind, and the LLM-prompt rules in Fix 2 + the deterministic
    post-composer check are the defense for clarifying-composer
    hallucinations. Fix 1's fail-closed lives on `post_action_reply` (real
    post-action context) and `qa_ack` (real Q&A path) only.
    """
    has_attempted_action = bool(
        context and isinstance(context.get("actions_executed"), list)
        and context.get("actions_executed")
    )
    if has_attempted_action:
        kind = "post_action_reply"
    else:
        kind = "action_clarifying"

    safe_text, safe_kind, safe_ctx = _enforce_g_a1_or_alert_fallback(
        config, reply_text, kind=kind, context=context,
    )
    if safe_kind == "alert_fallback":
        # G-A1 substituted the deterministic alert-fallback sentence; its
        # provenance is the fallback audit, not the original composer call.
        # AUDIT 2026-06-10: honest "caught myself" sentence, no action claim.
        provenance = {"fallback_audit": "2026-06-10"}
    if safe_ctx is not None:
        _h_mod._send_imessage_with_fallback_and_context(
            config, safe_text, kind=safe_kind, context=safe_ctx,
            provenance=provenance,
        )
    else:
        _h_mod._send_imessage_with_fallback(
            config, safe_text, kind=safe_kind, provenance=provenance,
        )



def _compose_action_clarifying_reply(
    *,
    claude: ClaudeClient,
    free_text: str,
    action_type: str,
    target_text: str | None,
    open_tasks: list[dict[str, Any]] | None,
    candidates: list[dict[str, Any]] | None,
    recent_outbound: list[dict[str, Any]] | None,
) -> tuple[str, dict[str, str]]:
    """Single composition entry point for the action layer's clarifying
    replies. Always tries the LLM first (per Principle 7). Falls back to the
    cold-fallback template ONLY when the LLM returns None.

    Returns `(reply_text, provenance)` — provenance names the LLM call when
    the composer produced the text, or the fallback audit date when the
    safe sentence shipped (2026-06-10 outbound provenance invariant).
    """
    msg = claude.compose_action_clarifying_reply(
        free_text=free_text,
        action_type=action_type,
        target_text=target_text,
        open_tasks=open_tasks,
        candidates=candidates,
        recent_outbound=recent_outbound,
    )
    if msg:
        return msg, {"llm_call": "compose_action_clarifying_reply"}
    # 2026-05-29: deleted _compose_clarifying_reply_cold_fallback. Its
    # template enumerated candidates as "(a) X; (b) Y" (g_v2_prose
    # violation), used "degraded reply / Auto-fallback" status-board
    # language (g_p1 violation), and capped at 240 chars. One safe
    # sentence is more honest than a ghost-spec template.
    logger.warning(
        "_compose_action_clarifying_reply: LLM composer returned None; "
        "sending safe message instead of stale template"
    )
    # AUDIT 2026-06-10: honest safe sentence, no action claim, prose.
    target = (target_text or "").strip()
    if target:
        return (
            f"Couldn't compose a clarifying reply on '{target[:60]}'. Try again?",
            {"fallback_audit": "2026-06-10"},
        )
    return (
        "Couldn't compose a clarifying reply. Try again?",
        {"fallback_audit": "2026-06-10"},
    )



def _save_pending_clarification_for_action(
    config: dict,
    *,
    sender_handle: str | None,
    action_type: str,
    free_text: str,
    proposed_matches: list[dict[str, Any]],
    reply_sent: str,
) -> None:
    """Persist the pending clarification so a follow-up "Yes" / "no" / "just
    UW" reply from the same sender can resolve it on the next inbound
    instead of being routed to the conversational path with no action
    context (the 2026-05-07 gap). Only saves when sender_handle is known
    and the action_type is one we can actually execute on confirmation
    (mark_done today; create/update/cancel have separate flows or aren't
    wired and don't benefit from pending state)."""
    if not sender_handle:
        return
    if action_type != "mark_done":
        # Save pending only for the verbs we can execute on confirmation.
        # Add to the allowlist when create_task / update / cancel can be
        # triggered by a follow-up "Yes."
        return
    state_path_str = (config.get("paths") or {}).get("imessage_state")
    if not state_path_str:
        return
    state_path = Path(state_path_str)
    # Trim proposed_matches to {id, title, confidence} to keep state small.
    # Hard cap at 6 (Fix 3, 2026-05-08): the resolver can't reasonably bind
    # "Yes" / "Yea" / "the UW one" against a slate of 30. The clarify-path
    # caller already pre-filters to topic-relevant tasks; this is the
    # belt-and-suspenders cap for any direct caller that bypasses the
    # pre-filter.
    trimmed: list[dict[str, Any]] = []
    for m in (proposed_matches or [])[:6]:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        trimmed.append({
            "id": m.get("id"),
            "title": (m.get("title") or "")[:200],
            "confidence": m.get("confidence") or "",
        })
    if not trimmed:
        # Nothing to save — the resolver would have nothing to act on.
        return
    try:
        save_pending_clarification(
            state_path,
            sender_handle,
            action_type=action_type,
            original_inbound=(free_text or "")[:1000],
            proposed_matches=trimmed,
            reply_sent=(reply_sent or "")[:500],
        )
        logger.info(
            "pending_clarification: saved sender=%s action_type=%s n_matches=%d",
            sender_handle, action_type, len(trimmed),
        )
    except Exception:
        logger.exception("pending_clarification: save failed (continuing)")



def _try_resolve_pending_clarification(
    *,
    config: dict,
    free_text: str,
    sender_handle: str | None,
    claude: ClaudeClient,
    graph: GraphClient,
    list_id: str,
) -> dict[str, Any] | None:
    """If a pending clarification exists for this sender, resolve it via the
    LLM and either execute the confirmed match IDs, send an ignore-ack, or
    return None / a fresh_intent sentinel to let the regular action flow
    handle the inbound.

    Returns:
      - dict (action_executed / action_ignored) when the resolver took the
        turn. Caller treats this exactly like a return from the regular
        action layer.
      - dict with `status="pending_clarification_fresh_intent"` when the
        resolver decided this is a fresh request. Carries the saved
        proposal so the caller can apply a stricter `create_task` gate
        (the 2026-05-08 fix: don't let fresh_intent + classify_action_intent
        returning create_task silently spawn a duplicate task that's
        actually a disambiguation of the prior clarifier).
      - None when no pending state exists.
    """
    if not sender_handle:
        return None
    state_path_str = (config.get("paths") or {}).get("imessage_state")
    if not state_path_str:
        # Config doesn't expose the state path (test contexts often don't).
        # Pending state lives in that file; without the path, no pending
        # state can exist. Skip silently and let the regular action flow run.
        return None
    state_path = Path(state_path_str)
    try:
        pending = load_pending_clarification(state_path, sender_handle)
    except Exception:
        logger.exception("pending_clarification: load failed (continuing without)")
        return None
    if not pending:
        return None

    logger.info(
        "pending_clarification: found for sender=%s set_at=%s n_matches=%d",
        sender_handle,
        pending.get("set_at"),
        len(pending.get("proposed_matches") or []),
    )

    resolved = claude.resolve_pending_action_clarification(free_text, pending)
    resolution = resolved.get("resolution") or "ignore"
    confirmed_ids = resolved.get("confirmed_match_ids") or []
    reasoning = resolved.get("reasoning") or ""
    logger.info(
        "pending_clarification: resolved=%s confirmed=%d reasoning=%r",
        resolution, len(confirmed_ids), reasoning[:120],
    )

    if resolution == "fresh_intent":
        # Clear pending — the resolver decided this isn't disambiguation.
        # But return a sentinel carrying the proposal so a follow-up
        # `create_task` can be guarded: the resolver's prompt was tightened
        # 2026-05-08 to prefer execute-with-empty-ids over fresh_intent for
        # ambiguous descriptive replies, but as defense in depth, the action
        # layer asks the user for clarification rather than blind-creating
        # a task whose body shape overlaps with a recent candidate title.
        proposed_matches = list(pending.get("proposed_matches") or [])
        clear_pending_clarification(state_path, sender_handle)
        return {
            "status": "pending_clarification_fresh_intent",
            "reasoning": reasoning[:200],
            "prior_proposed_matches": proposed_matches,
            "prior_inbound": (pending.get("original_inbound") or "")[:500],
        }

    if resolution == "ignore":
        clear_pending_clarification(state_path, sender_handle)
        # No PATCH ran and no user-facing reply needed; the resolver judged
        # her message as "ok thanks"-shaped. The action layer stays silent
        # on these (no past-tense claim, no fabricated ack).
        return {
            "status": "pending_clarification_ignored",
            "reason": reasoning[:120],
            "reply": "",
        }

    # resolution == "execute"
    # Map confirmed IDs back to titles (from the saved proposal) so we can
    # log with human-readable names. Order preserved for the composer.
    title_by_id = {
        m.get("id"): (m.get("title") or "")
        for m in (pending.get("proposed_matches") or [])
        if isinstance(m, dict) and m.get("id")
    }
    pending_action_type = pending.get("action_type") or "mark_done"

    # Phase 1: execute every confirmed PATCH. Build actions_executed[] with
    # per-id verified result. (F2, 2026-05-07.)
    actions_executed: list[dict[str, Any]] = []
    if pending_action_type == "mark_done":
        for tid in confirmed_ids:
            title = title_by_id.get(tid, "")
            try:
                ok, post_title = graph.mark_task_done(list_id, tid)
                if ok:
                    actions_executed.append({
                        "task_id": tid,
                        "action_type": "mark_done",
                        "result": "success",
                        "target_title": post_title or title,
                    })
                else:
                    actions_executed.append({
                        "task_id": tid,
                        "action_type": "mark_done",
                        "result": "failure",
                        "target_title": title,
                    })
            except Exception as e:
                logger.exception(
                    "pending_clarification: mark_task_done failed task_id=%s err=%s",
                    (tid or "")[:12], e,
                )
                actions_executed.append({
                    "task_id": tid,
                    "action_type": "mark_done",
                    "result": "failure",
                    "target_title": title,
                    "failure_reason": str(e)[:200],
                })

    # Always clear pending after an execute resolution. The proposal has
    # been consumed; further confirmations need a new clarifying turn.
    clear_pending_clarification(state_path, sender_handle)

    # Phase 2: compose the user-facing reply against the verified results.
    # Two cases:
    #   - confirmed_ids non-empty → ack the verified PATCH outcomes
    #     (all-success / partial / all-failure shapes handled by the
    #     composer skill).
    #   - confirmed_ids empty → resolver judged her reply ambiguous; ask
    #     ONE specific clarifying question downstream. No PATCH ran.
    needs_clarification = (resolution == "execute" and not confirmed_ids)
    proposed_for_clarify = None
    if needs_clarification:
        # Pass the full saved proposal back to the composer so it can name
        # the subsets in its question (e.g., "Both UW tasks, or all three?").
        proposed_for_clarify = [
            {"id": m.get("id"), "title": m.get("title") or ""}
            for m in (pending.get("proposed_matches") or [])
            if isinstance(m, dict) and m.get("id")
        ]

    reply_text = claude.compose_batch_action_reply(
        actions_executed=actions_executed,
        needs_clarification=needs_clarification,
        ambiguous_reply=free_text if needs_clarification else None,
        proposed_matches=proposed_for_clarify,
    )
    reply_provenance = {"llm_call": "compose_batch_action_reply"}

    if reply_text is None:
        # Cold fallback per Principle 7. Past-tense framing only when we
        # have a verified `result=success` row; otherwise stay honest.
        successes = [a for a in actions_executed if a.get("result") == "success"]
        failures = [a for a in actions_executed if a.get("result") == "failure"]
        if needs_clarification:
            reply_text = "I couldn't pin which subset you meant — can you name them?"
        elif successes and not failures:
            n = len(successes)
            reply_text = f"Marked {n} task{'s' if n != 1 else ''} done."
        elif successes and failures:
            reply_text = (
                f"Closed {len(successes)} but couldn't mark {len(failures)} — hit an error. Retry?"
            )
        elif failures and not successes:
            reply_text = (
                f"Couldn't mark any of the {len(failures)} done — MS To Do hit an error. Retry?"
            )
        else:
            reply_text = "Got it."
        # AUDIT 2026-05-29 (Phase 1 cold-fallback audit): per-branch past-tense
        # framing, action-grounded (counts come from real PATCH attempts).
        # Kept in place. Re-audit when persona spec or structural_checks
        # gates change.
        reply_provenance = {"fallback_audit": "2026-05-29"}
        logger.warning(
            "compose_batch_action_reply returned None; using cold fallback "
            "(successes=%d failures=%d needs_clarification=%s)",
            len(successes), len(failures), needs_clarification,
        )

    # Build the outbound row context. `tool_grounded` is True iff at least
    # one PATCH succeeded (so an action verb in the reply has a verified
    # tool result behind it). For needs_clarification, no PATCH ran; the
    # reply must NOT contain action verbs, and we let G-A1 enforce that.
    has_success = any(a.get("result") == "success" for a in actions_executed)
    context: dict[str, Any] = {
        "actions_executed": actions_executed,
        "tool_grounded": bool(has_success and actions_executed),
    }
    _send_action_layer_reply(
        config, reply_text, context=context, provenance=reply_provenance,
    )

    return {
        "status": (
            "pending_clarification_needs_clarify"
            if needs_clarification
            else "pending_clarification_executed"
        ),
        "action_type": pending_action_type,
        "executed_count": sum(1 for a in actions_executed if a.get("result") == "success"),
        "failed_count": sum(1 for a in actions_executed if a.get("result") == "failure"),
        "actions_executed": actions_executed,
        "reply": reply_text,
        "reasoning": reasoning[:200],
    }



def _try_anchor_then_sweep(
    *,
    config: dict,
    sender_handle: str | None,
    free_text: str,
    target_text: str | None,
    open_tasks: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    claude: ClaudeClient,
    graph: GraphClient,
    list_id: str,
    intent: dict[str, Any],
) -> dict[str, Any] | None:
    """Fix 2 (2026-05-07): anchor-then-sweep flow.

    When the periodic_summary mentioned ONE specific task and Megha replies
    "Yes on [topic]" with multiple same-topic tasks open, execute the
    anchor immediately and offer the siblings as a sweep. Returns the
    action_layer result dict on success, or None if the anchor isn't usable
    (no anchor in state, anchor not in open tasks, anchor not semantically
    related to the matched candidates).
    """
    if not sender_handle:
        return None
    state_path_str = (config.get("paths") or {}).get("imessage_state")
    if not state_path_str:
        return None
    state_path = Path(state_path_str)

    try:
        anchor = load_summary_anchor(state_path, sender_handle)
    except Exception:
        logger.exception("anchor-then-sweep: load_summary_anchor failed")
        return None
    if not anchor:
        return None

    anchor_id = anchor.get("anchor_task_id")
    anchor_title = anchor.get("anchor_task_title") or ""
    if not anchor_id:
        return None

    # Find the anchor in the current open-task slate. If the periodic_summary
    # is older than the open-task list and the anchor was already completed
    # or removed, fall back to the flat clarifier.
    anchor_task: dict[str, Any] | None = None
    for t in open_tasks or []:
        if t.get("id") == anchor_id:
            anchor_task = t
            break
    if anchor_task is None:
        logger.info(
            "anchor-then-sweep: anchor task_id=%s no longer in open list; "
            "falling back to flat clarifier",
            (anchor_id or "")[:12],
        )
        clear_summary_anchor(state_path, sender_handle)
        return None
    if (anchor_task.get("status") or "").lower() == "completed":
        clear_summary_anchor(state_path, sender_handle)
        return None

    # The anchor must be semantically related to the topic Megha confirmed.
    # Cheap deterministic check: the anchor's title must share at least one
    # meaningful word with `target_text` (the topic the user just named).
    # Without this, an anchor that happens to share the matcher's
    # most-recently-modified candidates would fire mark_done on an unrelated
    # task. Note: we don't gate on "anchor in candidates" because the
    # matcher's slate is built deterministically (matched_task + 2 recent)
    # without regard to the anchor.
    if not _share_topic_keyword(anchor_title, target_text or ""):
        logger.info(
            "anchor-then-sweep: anchor=%r unrelated to target=%r; falling back",
            anchor_title[:60], (target_text or "")[:60],
        )
        return None

    # Build the sweep slate: every OPEN task that shares a topic keyword
    # with the anchor's title (cheap deterministic overlap, computed against
    # the full open-task list). The matcher's 3-candidate slate undersamples
    # — when 4 Elders' Tea tasks are open, the matcher's `candidates` only
    # surfaces 3 because of the slate cap. The runtime owns the sibling-
    # count for the composer (closes the 2026-05-07 "n_matches=3 vs 'four
    # open Elders' Tea tasks'" divergence: composer counts what runtime
    # verified, never what the LLM independently inferred).
    sweep_siblings: list[dict[str, Any]] = []
    seen_ids: set[str] = {anchor_id}
    # First pass: candidates the matcher already surfaced (preserves order).
    for c in candidates or []:
        cid = c.get("id")
        if not cid or cid in seen_ids:
            continue
        if (c.get("status") or "").lower() == "completed":
            continue
        if not _share_topic_keyword(anchor_title, c.get("title") or ""):
            continue
        sweep_siblings.append({"id": cid, "title": c.get("title") or ""})
        seen_ids.add(cid)
    # Second pass: any other open task with topic overlap that the matcher
    # didn't surface (the 4th Elders' Tea task that fell outside the top-3
    # slate). Cap at 6 to keep the sweep ask short.
    for t in open_tasks or []:
        if len(sweep_siblings) >= 6:
            break
        tid = t.get("id")
        if not tid or tid in seen_ids:
            continue
        if (t.get("status") or "").lower() == "completed":
            continue
        if not _share_topic_keyword(anchor_title, t.get("title") or ""):
            continue
        sweep_siblings.append({"id": tid, "title": t.get("title") or ""})
        seen_ids.add(tid)

    # Execute the anchor immediately.
    actions_executed: list[dict[str, Any]] = []
    try:
        ok, post_title = graph.mark_task_done(list_id, anchor_id)
        actions_executed.append({
            "task_id": anchor_id,
            "action_type": "mark_done",
            "result": "success" if ok else "failure",
            "target_title": post_title or anchor_title,
        })
    except Exception as e:
        logger.exception("anchor-then-sweep: mark_task_done failed for anchor: %s", e)
        actions_executed.append({
            "task_id": anchor_id,
            "action_type": "mark_done",
            "result": "failure",
            "target_title": anchor_title,
            "failure_reason": str(e)[:200],
        })

    # Anchor consumed — clear so the next reply doesn't re-anchor it.
    clear_summary_anchor(state_path, sender_handle)

    # Compose the user-facing reply via the existing batch composer. Pass
    # `proposed_matches` (the siblings) so the composer can name them and
    # ask "those too?" in voice. needs_clarification=True signals "the
    # anchor was executed; the sweep is the open question."
    reply_text = claude.compose_batch_action_reply(
        actions_executed=actions_executed,
        needs_clarification=bool(sweep_siblings),
        ambiguous_reply=free_text if sweep_siblings else None,
        proposed_matches=sweep_siblings if sweep_siblings else None,
    )
    reply_provenance = {"llm_call": "compose_batch_action_reply"}
    if reply_text is None:
        # Cold fallback. Honest past-tense for the anchor; pose the sweep ask
        # without action verbs.
        anchor_succeeded = any(
            a.get("result") == "success" for a in actions_executed
        )
        if anchor_succeeded and sweep_siblings:
            n = len(sweep_siblings)
            reply_text = (
                f"Marked '{anchor_title[:60]}' done. "
                f"{n} more like it — those too?"
            )
        elif anchor_succeeded:
            reply_text = f"Marked '{anchor_title[:80]}' done."
        else:
            reply_text = (
                f"Couldn't mark '{anchor_title[:60]}' done — hit an error. Retry?"
            )
        # AUDIT 2026-05-29 (Phase 1 cold-fallback audit): past-tense, action-
        # grounded (only fires after anchor PATCH attempt). Kept in place.
        # Re-audit when persona spec or structural_checks gates change.
        reply_provenance = {"fallback_audit": "2026-05-29"}
        logger.warning(
            "anchor-then-sweep: compose_batch_action_reply returned None; cold fallback"
        )

    # Persist sweep proposal so a follow-up "Yes" / "Yea" resolves through
    # Fix 1's pending-clarification path. Only save when there are siblings
    # AND the anchor succeeded — if the anchor failed, asking about siblings
    # is muddier and the flat clarifier is the cleaner UX.
    anchor_succeeded = any(a.get("result") == "success" for a in actions_executed)
    if sweep_siblings and anchor_succeeded:
        _save_pending_clarification_for_action(
            config,
            sender_handle=sender_handle,
            action_type="mark_done",
            free_text=free_text,
            proposed_matches=sweep_siblings,
            reply_sent=reply_text,
        )

    has_success = any(a.get("result") == "success" for a in actions_executed)
    context: dict[str, Any] = {
        "actions_executed": actions_executed,
        "tool_grounded": bool(has_success and actions_executed),
    }
    _send_action_layer_reply(
        config, reply_text, context=context, provenance=reply_provenance,
    )

    _h_mod._log_action_intent_decision(
        config, free_text=free_text, intent=intent, task_match=anchor_task,
        decision="anchor_then_sweep",
        executed=anchor_succeeded,
    )

    return {
        "status": "anchor_then_sweep",
        "action_type": "mark_done",
        "anchor_task_id": anchor_id,
        "anchor_executed": anchor_succeeded,
        "sweep_count": len(sweep_siblings),
        "reply": reply_text,
    }



def _topic_tokens(s: str) -> set[str]:
    """Tokenize a string for topic-keyword overlap. Lowercase, strip MS
    To Do owner prefixes, split on non-letters, keep tokens ≥4 chars,
    drop common stopwords.

    Used ONLY by `_share_topic_keyword` (anchor-then-sweep gate). The Fix 3
    deterministic pre-filter that previously also called this helper was
    reverted 2026-05-08 — see `architecture.md` action-layer principle
    ("any deterministic string-matcher between LLM calls is a bug factory").
    """
    s = (s or "").lower()
    for prefix in ("mj ", "mm ", "[?] mj ", "[?] mm "):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    out: set[str] = set()
    cur: list[str] = []
    for ch in s:
        if ch.isalpha():
            cur.append(ch)
        else:
            if cur:
                w = "".join(cur)
                if len(w) >= 4 and w not in _TOPIC_STOPWORDS:
                    out.add(w)
                cur = []
    if cur:
        w = "".join(cur)
        if len(w) >= 4 and w not in _TOPIC_STOPWORDS:
            out.add(w)
    return out



def _share_topic_keyword(title_a: str, title_b: str) -> bool:
    """Cheap topic-keyword overlap: do `title_a` and `title_b` share at least
    one meaningful word (≥4 chars, not in a small stopword list, ignoring
    owner prefixes)? Used to gate anchor-then-sweep so a Zoom-link anchor
    doesn't get triggered by a reply about a totally different task.
    """
    if not title_a or not title_b:
        return False
    return bool(_topic_tokens(title_a) & _topic_tokens(title_b))



def _resolve_candidate_ids(
    candidate_ids: list[str],
    open_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map matcher-returned `candidate_task_ids` back to the task dicts in
    the input slate. Preserves matcher order. Drops any id that doesn't
    correspond to a task in the slate (defense in depth — matcher already
    validates ids, but a runtime-side check guards against drift).

    Returns a list of full task dicts. Empty when no ids match.
    """
    if not candidate_ids or not open_tasks:
        return []
    by_id = {t.get("id"): t for t in open_tasks if isinstance(t, dict) and t.get("id")}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cid in candidate_ids:
        if not isinstance(cid, str) or cid in seen:
            continue
        t = by_id.get(cid)
        if t is None:
            continue
        out.append(t)
        seen.add(cid)
    return out



def _try_handle_action_intent(
    free_text: str,
    recent_outbound: list[dict[str, Any]],
    claude: ClaudeClient,
    graph: GraphClient,
    list_id: str,
    config: dict,
    sender_handle: str | None = None,
    source_imessage_id: str | None = None,
) -> dict[str, Any] | None:
    """Action layer entry point. Runs the action-intent classifier and, when
    the inbound implies an action, takes full ownership of the reply.

    Serialization invariant (added 2026-05-05 evening): when the classifier
    returns has_action=true with any action_type in {mark_done, create,
    update, cancel}, this function MUST return a non-None dict and ship a
    reply via the action layer. The conversational composer is BYPASSED for
    every action-implying inbound. The prior partial bypass — only create_task
    happy path returned a reply, mark_done fall-throughs returned None and let
    the conversational composer fabricate a "marked it done" claim — was the
    root cause of the trust gap fix on 2026-05-05.

    Returns None ONLY when has_action=false (the message was not an action
    request); the caller then runs the conversational reply path.

    Live verbs:
      - mark_done: LLM-matched against the open tasks (paraphrase tolerant);
        executes PATCH on high-confidence match, asks a clarifying question on
        medium/low or no match, replies "already done" on a status=completed
        match. No dry-run gate (flipped to live 2026-05-05 evening).
      - create: parsed title + sender-derived owner prefix, POST to MS To Do
        with natural-key dedup keyed on source_imessage_id.

    Detect-only verbs (no live execution yet):
      - update / cancel: log and reply with an honest "I see you want to
        <verb> X — that verb isn't wired yet." No fall-through to the
        conversational composer.

    Pending clarification check (added 2026-05-07 evening): if the action
    layer just shipped a clarifying iMessage to this sender (e.g., "mark
    both UW tasks?"), her current inbound may be a confirmation ("yes") or
    partial selection ("just UW $630"). Resolve against the saved proposal
    BEFORE running the action classifier — a bare "yes" looks like
    has_action=false to the classifier, which would route it to the
    conversational composer with no execution. The resolver returns
    `fresh_intent` to fall through when her reply is unrelated.
    """
    pending_result = _try_resolve_pending_clarification(
        config=config,
        free_text=free_text,
        sender_handle=sender_handle,
        claude=claude,
        graph=graph,
        list_id=list_id,
    )
    fresh_intent_after_clarifier: dict[str, Any] | None = None
    if pending_result is not None:
        if pending_result.get("status") == "pending_clarification_fresh_intent":
            # Resolver judged this NOT a disambiguation — fall through to the
            # regular classifier, but carry the prior proposal so the
            # create_task path can ask a follow-up question rather than blind-
            # create a duplicate of one of the candidates.
            fresh_intent_after_clarifier = pending_result
        else:
            return pending_result

    # Pass recent Kavi outbound as classifier context so "mark that one done"
    # can be resolved against what Kavi just said. Same shape the conversation
    # composer already uses.
    recent_kavi = [
        {"sent_at": r.get("ts"), "body": (r.get("text") or "")[:200]}
        for r in (recent_outbound or [])[-5:]
    ]
    intent = claude.classify_action_intent(free_text, recent_kavi_messages=recent_kavi)

    has_action = intent.get("has_action", False)
    action_type = intent.get("action_type")
    target_text = intent.get("target_text")
    confidence = intent.get("confidence", "low")

    if not has_action:
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=None,
            decision="no_action_detected", executed=False,
        )
        # Kavi coordinates routing (added 2026-05-05). Before returning None
        # and letting the conversational composer handle this inbound, run
        # the coordination-intent classifier. When it returns is_coordination=
        # high, start a coordination session and bypass the conversational
        # composer the same way the action layer does. The bypass invariant
        # holds: coordination-implying inbounds reply via the coordination
        # handler, never via compose_conversational_reply.
        coord_result = _try_handle_coordination_intent(
            free_text=free_text,
            sender_handle=sender_handle,
            claude=claude,
            graph=graph,
            config=config,
        )
        if coord_result is not None:
            return coord_result
        return None

    # ---- create verb -----------------------------------------------------
    if action_type == "create":
        # Defense against the 2026-05-08 duplicate-task bug: when the
        # resolver JUST returned fresh_intent on a clarifier window AND the
        # classifier now wants to create_task, ask the user whether they
        # meant to disambiguate one of the prior candidates instead. The
        # 17:41:03Z trace created "MJ MJ elder's tea at maple street
        # tomorrow" because the inbound "MJ elder's tea at maple street
        # tomorrow" was a paraphrase of an existing candidate, but the
        # resolver coded it as fresh_intent and the classifier filled in
        # create_task. Asking back is much cheaper than creating a duplicate.
        if fresh_intent_after_clarifier is not None:
            prior = fresh_intent_after_clarifier.get("prior_proposed_matches") or []
            logger.info(
                "action_intent: create_task suppressed by post-clarifier guard; "
                "asking user to disambiguate against %d prior candidates",
                len(prior),
            )
            clarify, clarify_prov = _compose_action_clarifying_reply(
                claude=claude,
                free_text=free_text,
                action_type="mark_done",
                target_text=target_text,
                open_tasks=None,
                candidates=[
                    {"id": m.get("id"), "title": m.get("title") or ""}
                    for m in prior if isinstance(m, dict) and m.get("id")
                ] or None,
                recent_outbound=recent_outbound,
            )
            _send_action_layer_reply(config, clarify, provenance=clarify_prov)
            # Re-save the prior proposal so the user's next reply still
            # binds to it. TTL refreshes on save.
            _save_pending_clarification_for_action(
                config,
                sender_handle=sender_handle,
                action_type="mark_done",
                free_text=free_text,
                proposed_matches=prior,
                reply_sent=clarify,
            )
            _h_mod._log_action_intent_decision(
                config, free_text=free_text, intent=intent, task_match=None,
                decision="post_clarifier_create_guard", executed=False,
            )
            return {
                "status": "action_clarifying",
                "action_type": "mark_done",
                "reason": "post_clarifier_create_guard",
                "reply": clarify,
            }
        result = _handle_create_task_verb(
            free_text=free_text,
            intent=intent,
            target_text=target_text,
            confidence=confidence,
            sender_handle=sender_handle,
            source_imessage_id=source_imessage_id,
            claude=claude,
            graph=graph,
            list_id=list_id,
            config=config,
        )
        if result is not None:
            return result
        # _handle_create_task_verb returns None on low/medium confidence or
        # empty title. Per the bypass invariant, the action layer still owns
        # the reply — send an honest clarifying message instead of falling
        # through to the conversational composer. LLM-composed per Principle 7.
        clarify, clarify_prov = _compose_action_clarifying_reply(
            claude=claude,
            free_text=free_text,
            action_type="create",
            target_text=target_text,
            open_tasks=None,
            candidates=None,
            recent_outbound=recent_outbound,
        )
        _send_action_layer_reply(config, clarify, provenance=clarify_prov)
        return {
            "status": "action_clarifying",
            "action_type": "create",
            "reason": "create_low_confidence_or_empty_target",
            "reply": clarify,
        }

    # ---- update / cancel: detect-only, but bypass the conversational composer
    if action_type in {"update", "cancel"}:
        logger.info(
            "action_intent: detected action_type=%s (verb not wired) target=%r conf=%s",
            action_type, (target_text or "")[:60], confidence,
        )
        # Per Principle 7, the LLM composes the "verb not wired" reply too —
        # leaking action_type strings ("I see you want to mark_done...") and
        # placeholder "that" tokens both come from this template path.
        reply, reply_prov = _compose_action_clarifying_reply(
            claude=claude,
            free_text=free_text,
            action_type=action_type,
            target_text=target_text,
            open_tasks=None,
            candidates=None,
            recent_outbound=recent_outbound,
        )
        _send_action_layer_reply(config, reply, provenance=reply_prov)
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=None,
            decision=f"detect_only_{action_type}", executed=False,
        )
        return {
            "status": "action_detect_only",
            "action_type": action_type,
            "reason": "verb_not_wired",
            "reply": reply,
        }

    # ---- mark_done -------------------------------------------------------
    if action_type != "mark_done":
        # Defensive: any unexpected action_type. Bypass invariant still applies.
        logger.warning(
            "action_intent: unexpected action_type=%r — sending honest fallback reply",
            action_type,
        )
        # AUDIT 2026-06-10: deterministic safe sentence, honest, no action claim.
        reply = "I caught an action request I don't know how to handle yet. What did you want me to do?"
        _send_action_layer_reply(
            config, reply, provenance={"fallback_audit": "2026-06-10"},
        )
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=None,
            decision=f"unknown_action_type_{action_type}", executed=False,
        )
        return {
            "status": "action_unknown",
            "action_type": action_type,
            "reply": reply,
        }

    # Single LLM-matching path for both clarify-needed and high-confidence
    # branches: fetch the full open-task slate (server-side $filter on
    # status), hand ALL of it to the LLM matcher (it handles apostrophes,
    # abbreviations, paraphrases — the 2026-05-08 tokenization revert),
    # then branch on what the matcher returns. No deterministic pre-filter
    # between the intent classifier and the matcher.
    #
    # 2026-05-08 second pass: switched from list_recent_todo_tasks(top=30)
    # to list_open_todo_tasks(top=100). The recency-only top=30 fetch was
    # dropping older notStarted tasks off the slate on busy days because
    # mass-completions pushed them past position 30; the matcher then
    # honestly reported "no candidates" against an incomplete slate.
    try:
        open_tasks = graph.list_open_todo_tasks(list_id, top=100)
    except Exception as e:
        logger.exception("action_intent: list_open_todo_tasks failed: %s", e)
        # AUDIT 2026-06-10: deterministic safe sentence, honest, no action claim.
        reply = "Couldn't reach MS To Do to check your tasks — try again in a moment?"
        _send_action_layer_reply(
            config, reply, provenance={"fallback_audit": "2026-06-10"},
        )
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=None,
            decision="lookup_failed", executed=False,
        )
        return {
            "status": "action_failed",
            "action_type": "mark_done",
            "reason": "lookup_failed",
            "reply": reply,
        }

    if confidence != "high" or not target_text:
        # Low/medium intent confidence OR empty target_text route through the
        # LLM clarifying composer with the FULL 30-task slate. Empty
        # target_text is the multi-item case ("Mark four things done") — the
        # matcher sees the full free_text and the full slate and surfaces
        # the topic-relevant subset itself.
        match_result = claude.match_target_to_open_task(
            target_text or free_text, open_tasks,
        )
        candidate_ids = match_result.get("candidate_task_ids") or []
        match_reasoning = match_result.get("reasoning", "")
        # Resolve candidate IDs to task dicts (preserving matcher order).
        candidates = _resolve_candidate_ids(candidate_ids, open_tasks)
        logger.info(
            "action_intent: mark_done clarify path target=%r intent_conf=%s "
            "open_tasks=%d matcher_candidates=%d reason=%r",
            (target_text or "")[:60], confidence,
            len(open_tasks), len(candidates), match_reasoning[:120],
        )
        clarify, clarify_prov = _compose_action_clarifying_reply(
            claude=claude,
            free_text=free_text,
            action_type="mark_done",
            target_text=target_text,
            open_tasks=None,
            candidates=candidates if candidates else None,
            recent_outbound=recent_outbound,
        )
        _send_action_layer_reply(config, clarify, provenance=clarify_prov)
        # Save ONLY the matcher's identified subset as pending state. Closes
        # the 2026-05-07 gap where the resolver got 30 unrelated tasks. When
        # the matcher surfaces zero candidates the proposal is empty — the
        # follow-up "Yes" path correctly resolves to ignore (no IDs to
        # confirm) and the runtime will route a new inbound through fresh
        # classify per the resolver's `fresh_intent` branch.
        _save_pending_clarification_for_action(
            config,
            sender_handle=sender_handle,
            action_type="mark_done",
            free_text=free_text,
            proposed_matches=candidates,
            reply_sent=clarify,
        )
        reason_tag = (
            "no_target_text" if not target_text
            else f"intent_confidence_{confidence}"
        )
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=None,
            decision=(
                "clarify_no_target" if not target_text
                else f"clarify_intent_confidence_{confidence}"
            ),
            executed=False,
        )
        return {
            "status": "action_clarifying",
            "action_type": "mark_done",
            "reason": reason_tag,
            "reply": clarify,
        }

    # LLM-driven match against open tasks. Replaces the prior
    # find_task_by_exact_title (string equality) so paraphrases like
    # "UW Medicine balance ($630)" can match a stored
    # "MJ Pay UW Medicine overdue balance ($630.00)". The matcher returns
    # high-confidence only on unambiguous semantic matches; medium/low/None
    # routes to a clarifying reply, not an execution.
    match_result = claude.match_target_to_open_task(target_text, open_tasks)
    match_id = match_result.get("match_id")
    match_conf = match_result.get("confidence", "low")
    match_reasoning = match_result.get("reasoning", "")
    matcher_candidate_ids = match_result.get("candidate_task_ids") or []
    logger.info(
        "action_intent: match_target_to_open_task target=%r match_id=%s conf=%s "
        "candidate_ids=%d reason=%r",
        target_text[:60], (match_id or "")[:12], match_conf,
        len(matcher_candidate_ids), match_reasoning[:120],
    )

    # Resolve match_id to the candidate task dict for status/title checks.
    matched_task: dict[str, Any] | None = None
    if match_id:
        for t in open_tasks:
            if t.get("id") == match_id:
                matched_task = t
                break
        if matched_task is None:
            logger.warning(
                "action_intent: matcher returned match_id=%s but no candidate has that id; treating as no match",
                match_id[:12],
            )
            match_id = None
            match_conf = "low"

    # Ambiguous (medium) or no match (low) — send a clarifying reply, do NOT execute.
    if match_id is None or match_conf != "high":
        # Use the matcher's `candidate_task_ids` (the LLM-judged plausible
        # subset). This replaces the prior deterministic "matched_task + 2
        # most-recently-modified" slate that picked unrelated tasks when the
        # matcher couldn't find anything (the 2026-05-07 Elders' Tea bug).
        # Drop any already-completed task; preserve matcher order.
        candidates: list[dict[str, Any]] = []
        for c in _resolve_candidate_ids(matcher_candidate_ids, open_tasks):
            if (c.get("status") or "").lower() == "completed":
                continue
            candidates.append(c)

        # Fix 2 (2026-05-07): anchor-then-sweep. If a recent periodic_summary
        # mentioned a specific task ("confirm your guest got the Zoom link")
        # AND that anchor is among the open tasks AND it semantically matches
        # the topic Megha just confirmed ("Yes on Elders' Tea"), execute the
        # anchor immediately and offer the siblings as a sweep. Without this,
        # the flat clarifier dumps every same-topic task and forces a second
        # confirmation round.
        anchor_result = _try_anchor_then_sweep(
            config=config,
            sender_handle=sender_handle,
            free_text=free_text,
            target_text=target_text,
            open_tasks=open_tasks,
            candidates=candidates,
            claude=claude,
            graph=graph,
            list_id=list_id,
            intent=intent,
        )
        if anchor_result is not None:
            return anchor_result

        clarify, clarify_prov = _compose_action_clarifying_reply(
            claude=claude,
            free_text=free_text,
            action_type="mark_done",
            target_text=target_text,
            open_tasks=None,
            candidates=[
                {**c, "match_reasoning": match_reasoning if c is matched_task else ""}
                for c in candidates
            ] if candidates else None,
            recent_outbound=recent_outbound,
        )
        _send_action_layer_reply(config, clarify, provenance=clarify_prov)
        # Save the proposal — caller's "yes" / "the UW one" can resolve it.
        # Only the matcher's identified candidates land in pending state, so
        # the resolver can bind a follow-up against a focused 2-6 task subset.
        _save_pending_clarification_for_action(
            config,
            sender_handle=sender_handle,
            action_type="mark_done",
            free_text=free_text,
            proposed_matches=candidates,
            reply_sent=clarify,
        )
        decision = "clarify_match_ambiguous" if match_conf == "medium" else "clarify_match_none"
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=matched_task,
            decision=decision, executed=False,
        )
        return {
            "status": "action_clarifying",
            "action_type": "mark_done",
            "reason": "ambiguous_match" if match_conf == "medium" else "no_match",
            "match_confidence": match_conf,
            "match_reasoning": match_reasoning,
            "reply": clarify,
        }

    # High-confidence match — proceed.
    task = matched_task
    task_id = task["id"]
    stored_title = task.get("title", "") or target_text

    # Already-completed pre-check: Graph 200s on PATCH-status=completed even
    # if the task is already done. Without this guard, Kavi would reply
    # "Done — marked X as completed" with no actual state change. Honest
    # behavior: detect, skip the PATCH, send a deterministic no-change reply.
    if (task.get("status") or "").lower() == "completed":
        logger.info(
            "action_intent: task already completed task_id=%s title=%r; sending no-change reply",
            task_id[:12], stored_title[:80],
        )
        # AUDIT 2026-06-10: deterministic no-change reply; "marked complete"
        # is grounded by the verified status read directly above.
        reply = f"Already done — '{stored_title[:80]}' was already marked complete. No change."
        reply = reply[:180]
        context = {
            "actions_executed": [
                {
                    "task_id": task_id,
                    "action_type": "mark_done",
                    "result": "already_completed",
                    "target_title": stored_title,
                }
            ],
        }
        _send_action_layer_reply(
            config, reply, context=context,
            provenance={"fallback_audit": "2026-06-10"},
        )
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=task,
            decision="already_completed", executed=False,
        )
        return {
            "status": "action_executed",
            "action_type": "mark_done",
            "result": "already_completed",
            "task_id": task_id,
            "target_title": stored_title,
            "reply": reply,
        }

    failure_reason: str | None = None
    try:
        ok, post_title = graph.mark_task_done(list_id, task_id)
        result = "success" if ok else "failure"
        title_for_reply = post_title or stored_title
        logger.info("action_intent: marked done task_id=%s title=%r", task_id[:12], title_for_reply[:80])
    except Exception as e:
        logger.exception("action_intent: mark_task_done failed: %s", e)
        result = "failure"
        failure_reason = str(e)[:200]
        title_for_reply = stored_title

    reply = claude.compose_post_action_reply(
        action_type="mark_done",
        target_title=title_for_reply,
        result=result,
        failure_reason=failure_reason,
    )
    reply_provenance = {"llm_call": "compose_post_action_reply"}
    if reply is None:
        # Deterministic cold fallback so Megha still hears back. Past-tense framing.
        # AUDIT 2026-05-29 (Phase 1 cold-fallback audit): honors voice rules
        # and is action-grounded (only fires after a real mark_done PATCH
        # attempt with concrete success/failure result). Kept in place.
        # Re-audit when persona spec or structural_checks gates change.
        readable = title_for_reply or target_text
        if result == "success":
            reply = f"Marked done — '{readable[:80]}' is closed out."
        else:
            reply = f"Couldn't mark '{readable[:80]}' done — hit an error. Want me to try again?"
        reply_provenance = {"fallback_audit": "2026-05-29"}
        logger.warning("compose_post_action_reply returned None; using cold fallback")

    # Outbound row instrumentation: log the executed action in the outbound
    # row's context dict so the eval surface can auto-fill AV at label time.
    context = {
        "actions_executed": [
            {
                "task_id": task_id,
                "action_type": "mark_done",
                "result": result,
                "target_title": title_for_reply,
            }
        ],
    }
    if failure_reason:
        context["actions_executed"][0]["failure_reason"] = failure_reason
    _send_action_layer_reply(
        config, reply, context=context, provenance=reply_provenance,
    )

    _h_mod._log_action_intent_decision(
        config, free_text=free_text, intent=intent, task_match=task,
        decision=("executed_success" if result == "success" else "executed_failure"),
        executed=(result == "success"),
    )

    return {
        "status": "action_executed",
        "action_type": "mark_done",
        "result": result,
        "task_id": task_id,
        "target_title": title_for_reply,
        "reply": reply,
    }




def _handle_correction(
    free_text: str,
    config: dict,
    sender_handle: str | None = None,
    source_imessage_id: str | None = None,
) -> dict[str, Any]:
    """Classify a free-text iMessage as a correction; if so, take the matching MS To Do
    action, append to corrections.jsonl, and ack within 60s. If classified not_correction,
    route to conversational reply (G-C1) — not a templated ack.

    sender_handle + source_imessage_id flow through to the action layer for the
    create-task verb (sender determines owner_prefix; source_imessage_id is the
    natural dedup key for duplicate webhook fires).
    """
    graph, claude, bb = _h_mod._get_clients(config)
    list_id = config["graph"]["mstodo_shared_list_id"]
    corrections_path = Path(config["paths"]["corrections_jsonl"])

    classification = claude.classify_correction(free_text)
    if classification.get("status") != "correction":
        # G-C1 (added 2026-05-04): when classifier says not-correction, the message is
        # conversational — Megha is asking a question, chatting, or testing Kavi.
        # Compose a natural reply via persona prompt instead of the templated ack.
        # G-C2 simple thread memory: pass last 5 outbound messages from the eval
        # surface as context so the reply can reference what Kavi just said.
        logger.info("classify_correction: not_correction → checking action intent reason=%r",
                    classification.get("reason", "")[:120])
        recent_outbound = _h_mod._load_recent_outbound(config, n=5)

        # Stage 1 action layer (added 2026-05-05): before falling through to the
        # conversational reply, run the action-intent classifier. If Megha said
        # "Mark X done" with high confidence and the title matches an existing
        # task exactly (case-insensitive, trimmed), execute the PATCH and reply
        # post-action. Anything else (low/medium conf, no exact match, or any
        # action_type other than mark_done) falls through to conversation —
        # Stage 2 will add fuzzy matching + multi-action.
        action_result = _try_handle_action_intent(
            free_text, recent_outbound, claude, graph, list_id, config,
            sender_handle=sender_handle, source_imessage_id=source_imessage_id,
        )
        if action_result is not None:
            return action_result

        # Pending-fact context (2026-06-10): facts awaiting Megha's
        # confirmation (e.g., an in-flight coordination ask the digest
        # offered) are answerable here. Selection feed strips boilerplate
        # and passes full text; the skill owns when to use it. Failure-safe
        # — an empty list reproduces the prior behavior.
        try:
            from capabilities.kavi_persona.selection import (
                _read_pending_facts_for_summary,
            )
            pending_facts_ctx = _read_pending_facts_for_summary(config, top_n=3)
        except Exception:
            logger.exception(
                "conversational pending-facts read failed (continuing empty)"
            )
            pending_facts_ctx = []

        conv_reply = claude.compose_conversational_reply(
            free_text, recent_outbound=recent_outbound,
            pending_facts=pending_facts_ctx,
        )
        conv_provenance = {"llm_call": "compose_conversational"}
        if conv_reply is None:
            # Cold fallback: minimal warmth, no bake. Better than a templated form.
            # AUDIT 2026-05-29 (Phase 1 cold-fallback audit): honest single
            # sentence; no action claim, no status board, prose, first-person,
            # ≤120 chars. Kept in place. Re-audit when persona spec changes.
            conv_reply = "Got it — sitting with that. Anything specific you want me to do?"
            conv_provenance = {"fallback_audit": "2026-05-29"}
            logger.warning("compose_conversational_reply returned None; using cold fallback")

        # G-A1 enforcement (2026-05-07): the conversational composer has no
        # tool result in scope. If its output contains an action verb
        # (sent, marked, added, ...), it's a Principle 7 hallucination — the
        # composer fabricated a tool result that didn't run. Drop the
        # offending text into the alert_fallback path with an honest "I'm
        # degraded" message. The original hallucinated text never ships.
        # 2026-05-08: refactored to share `_enforce_g_a1_or_alert_fallback`
        # with post_action_reply and qa_ack (Fix 1).
        conv_reply, kind, _ = _enforce_g_a1_or_alert_fallback(
            config, conv_reply, kind="conversational", context=None,
        )
        if kind == "alert_fallback":
            # AUDIT 2026-06-10: G-A1 substituted the deterministic alert
            # sentence; provenance is the audit, not the composer call.
            conv_provenance = {"fallback_audit": "2026-06-10"}
        _h_mod._send_imessage_with_fallback(
            config, conv_reply, kind=kind, provenance=conv_provenance,
        )
        return {
            "status": "conversational_reply",
            "reason": classification.get("reason"),
            "classification": classification,
            "reply": conv_reply,
        }

    correction = classification["correction"]
    correction_type = correction.get("type", "")
    target = correction.get("target", "")
    new_value = correction.get("new_value")

    logger.info("correction: type=%s target=%r new_value=%r", correction_type, target[:80], new_value)

    # Dispatch to the right MS To Do action by type. Each branch sets `ack` and
    # optionally records what changed. _handle_correction always appends to
    # corrections.jsonl regardless of action success.
    ack: str
    action_taken = False

    try:
        if correction_type == "missed":
            title = f"MJ [Manual] {target[:120]}"
            new_id = graph.create_todo_task(
                list_id,
                {"title": target[:120], "owner": "megha", "source_tag": "Manual", "owner_reason": "added via correction"},
                source_email_id=f"correction-{uuid.uuid4().hex[:8]}",
                source_subject=f"Correction: {target[:80]}",
            )
            ack = f"Got it! '{target[:80]}' added to MS To Do."
            action_taken = True
            logger.info("  -> missed: created task id=%s", new_id[:12])

        elif correction_type == "false_positive":
            task = graph.find_task_by_title(list_id, target)
            if task:
                graph.update_todo_task(list_id, task["id"], {"status": "completed"})
                ack = f"Got it! Removed '{task.get('title', '')[:80]}' from MS To Do."
                action_taken = True
                logger.info("  -> false_positive: completed task id=%s", task["id"][:12])
            else:
                ack = "Got it! Logged, but couldn't find a matching task in MS To Do."

        elif correction_type == "wrong_owner":
            task = graph.find_task_by_title(list_id, target)
            if task and new_value in OWNER_ABBREV:
                new_title = _swap_owner_in_title(task.get("title", ""), new_value)
                graph.update_todo_task(list_id, task["id"], {"title": new_title})
                ack = f"Got it! '{_strip_kavi_prefix(new_title)[:80]}' reassigned to {new_value.title()}."
                action_taken = True
                logger.info("  -> wrong_owner: patched task id=%s new_owner=%s", task["id"][:12], new_value)
            else:
                ack = "Got it! Logged, but couldn't find a matching task in MS To Do."

        elif correction_type == "wrong_title":
            task = graph.find_task_by_title(list_id, target)
            if task and new_value:
                new_title = _replace_body_in_title(task.get("title", ""), new_value)
                graph.update_todo_task(list_id, task["id"], {"title": new_title})
                ack = f"Got it! Renamed to '{new_value[:80]}'."
                action_taken = True
                logger.info("  -> wrong_title: patched task id=%s", task["id"][:12])
            else:
                ack = "Got it! Logged, but couldn't find a matching task in MS To Do."

        elif correction_type == "wrong_source_tag":
            task = graph.find_task_by_title(list_id, target)
            if task and new_value:
                new_title = _replace_tag_in_title(task.get("title", ""), new_value)
                graph.update_todo_task(list_id, task["id"], {"title": new_title})
                ack = f"Got it! Retagged as [{new_value}]."
                action_taken = True
                logger.info("  -> wrong_source_tag: patched task id=%s", task["id"][:12])
            else:
                ack = "Got it! Logged, but couldn't find a matching task in MS To Do."

        elif correction_type == "wrong_confidence":
            task = graph.find_task_by_title(list_id, target)
            if task and new_value in {"high", "medium", "low"}:
                new_title = _adjust_confidence_marker(task.get("title", ""), new_value)
                graph.update_todo_task(list_id, task["id"], {"title": new_title})
                ack = f"Got it! Confidence updated to {new_value}."
                action_taken = True
                logger.info("  -> wrong_confidence: patched task id=%s", task["id"][:12])
            else:
                ack = "Got it! Logged, but couldn't find a matching task in MS To Do."

        else:
            ack = f"Got it! Logged a {correction_type} correction."
            logger.warning("unknown correction type: %s", correction_type)

    except Exception as e:
        logger.exception("correction action failed: %s", e)
        ack = "Got it! Logged, but the MS To Do update hit an error — please check directly."

    # Append to corrections.jsonl regardless of action outcome — the learning loop
    # (step 11 Examples promotion) reads this whether or not the immediate action succeeded.
    try:
        append_correction(corrections_path, {
            **correction,
            "free_text": free_text[:500],
            "action_taken": action_taken,
            "run_id_at_correction": utc_now_iso(),
        })
    except Exception as e:
        logger.warning("corrections.jsonl append failed: %s", e)

    # G-A1 grounding (2026-05-07): correction_ack contains action verbs
    # ("added", "removed", "renamed", "retagged"). Mark tool_grounded=True
    # only when the Graph call succeeded (action_taken=True); on failure
    # the ack starts with "Got it! Logged, but couldn't find..." which the
    # action verb in the rest of the line still trips, but action_taken=False
    # also means the verb doesn't reflect a verified state change. Keep
    # tool_grounded False on failure so G-A1 catches it.
    # Conformance-sweep finding (2026-06-10 provenance grandfather pass):
    # the correction acks above are deterministic "Got it! ..." templates,
    # neither LLM-composed nor previously audited. AUDIT 2026-06-10: each
    # branch's action verb is grounded by the Graph call result directly
    # above (action_taken gates tool_grounded). NOTE: the inbound iMessage
    # dispatch no longer routes corrections here (the intent parser's
    # `correction` intent + final reply composer own that path); this
    # function remains for direct callers. Listed for LLM migration or
    # removal once the parser path has soaked.
    _h_mod._send_imessage_with_fallback_and_context(
        config, ack, kind="correction_ack",
        context={"tool_grounded": bool(action_taken)},
        provenance={"fallback_audit": "2026-06-10"},
    )

    return {"status": "correction", "type": correction_type, "action_taken": action_taken, "ack": ack}




__all__ = [
    "_enforce_g_a1_or_alert_fallback",
    "_send_action_layer_reply",
    "_compose_action_clarifying_reply",
    "_save_pending_clarification_for_action",
    "_try_resolve_pending_clarification",
    "_try_anchor_then_sweep",
    "_topic_tokens",
    "_share_topic_keyword",
    "_resolve_candidate_ids",
    "_try_handle_action_intent",
    "_handle_correction",
]
