"""Capability handler functions: _handle_create_task_verb.

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
# REMOVED CROSS-CAP IMPORT (unused): from capabilities.coordination.dispatch import _try_handle_coordination_intent  # noqa: F401

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


logger = logging.getLogger(__name__)




def _handle_create_task_verb(
    *,
    free_text: str,
    intent: dict[str, Any],
    target_text: str | None,
    confidence: str,
    sender_handle: str | None,
    source_imessage_id: str | None,
    claude: ClaudeClient,
    graph: GraphClient,
    list_id: str,
    config: dict,
) -> dict[str, Any] | None:
    """Action layer create-task verb (iMessage to task capability, 2026-05-05).
    When the classifier returns action_type=create with confidence=high AND a
    non-empty target_text, create a task in the McMullen-Jain Shared list and
    reply post-action.

    Live execution — no dry-run gate (Megha approved 2026-05-05). Conservative
    on classifier output: anything but high confidence + non-empty title falls
    through to the conversational reply.

    Natural-key dedup: source_imessage_id is the externalId on the linked
    resource; a duplicate webhook fire short-circuits via
    GraphClient.find_todo_task_by_source_email before a second task is created.
    """
    if confidence != "high":
        logger.info(
            "create_task: confidence=%s — not high, falling through target=%r",
            confidence, (target_text or "")[:60],
        )
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=None,
            decision=f"fall_through_create_confidence_{confidence}", executed=False,
        )
        return None
    if not target_text or not target_text.strip():
        logger.info("create_task: empty target_text — falling through")
        _h_mod._log_action_intent_decision(
            config, free_text=free_text, intent=intent, task_match=None,
            decision="fall_through_create_no_target", executed=False,
        )
        return None

    title = target_text.strip()[:200]
    owner_prefix = _owner_prefix_from_sender(sender_handle, config)
    deadline = _parse_deadline_iso(title)

    failure_reason: str | None = None
    task_id: str | None = None
    created = False
    try:
        task_id, created = graph.create_task_in_shared_list(
            list_id,
            title=title,
            owner_prefix=owner_prefix,
            deadline=deadline,
            source_imessage_id=source_imessage_id,
        )
        result = "success"
        logger.info(
            "create_task: created=%s task_id=%s title=%r owner=%s",
            created, (task_id or "")[:12], title[:80], owner_prefix,
        )
        try:
            from kavi_runtime.structured_log import log_event
            log_event(
                "outbound", "task_created",
                source="imessage",
                owner=owner_prefix,
                dedup_hit=(not created),
            )
        except Exception:
            logger.debug("structured_log task_created emit failed (continuing)")
    except Exception as e:
        logger.exception("create_task: create_task_in_shared_list failed: %s", e)
        result = "failure"
        failure_reason = str(e)[:200]

    rendered_title = f"{owner_prefix} {title}" if task_id else title
    reply_provenance = {"llm_call": "compose_post_action_reply"}
    reply = claude.compose_post_action_reply(
        action_type="create_task",
        target_title=rendered_title,
        result=result,
        failure_reason=failure_reason,
    )
    if reply is None:
        # Deterministic cold fallback. Past-tense framing.
        # AUDIT 2026-05-29 (Phase 1 cold-fallback audit): honors voice rules
        # (past-tense, ≤120 chars, prose, first-person, no signoff, no status
        # board) and is action-grounded (only fires after a real create_task
        # PATCH attempt with concrete success/failure result). Kept in place.
        # Re-audit when persona spec or structural_checks gates change.
        if result == "success":
            verb = "Added" if created else "Already had"
            reply = f"{verb} '{title[:80]}' on the shared list."
        else:
            reply = f"Couldn't add '{title[:80]}' — hit an error. Want me to try again?"
        reply_provenance = {"fallback_audit": "2026-05-29"}
        logger.warning("compose_post_action_reply (create_task) returned None; using cold fallback")

    context = {
        "actions_executed": [
            {
                "task_id": task_id,
                "action_type": "create_task",
                "result": result,
                "target_title": rendered_title,
                "owner_prefix": owner_prefix,
                "dedup_hit": (not created) if task_id else False,
                "source_imessage_id": source_imessage_id,
            }
        ],
    }
    if failure_reason:
        context["actions_executed"][0]["failure_reason"] = failure_reason

    _h_mod._send_imessage_with_fallback_and_context(
        config, reply, kind="post_action_reply", context=context,
        provenance=reply_provenance,
    )

    decision = (
        "executed_success" if result == "success" and created
        else "executed_dedup_hit" if result == "success" and not created
        else "executed_failure"
    )
    _h_mod._log_action_intent_decision(
        config, free_text=free_text, intent=intent,
        task_match={"id": task_id, "title": rendered_title} if task_id else None,
        decision=decision, executed=(result == "success" and created),
    )

    return {
        "status": "action_executed",
        "action_type": "create_task",
        "result": result,
        "task_id": task_id,
        "target_title": rendered_title,
        "owner_prefix": owner_prefix,
        "dedup_hit": (not created) if task_id else False,
        "reply": reply,
    }




__all__ = [
    "_handle_create_task_verb",
]
