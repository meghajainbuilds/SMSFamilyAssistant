"""Capability handler functions: correction_pattern_check, _correction_pattern_check_impl.

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




def correction_pattern_check(config: dict) -> None:
    """Scheduler-driven Examples-row proposer (every N hours). Wraps the body
    in a Trace context so any Examples-proposal iMessage gets captured under
    evals/traces/exchanges.jsonl as a scheduler-channel exchange. Failure-safe.
    See _correction_pattern_check_impl for the full behavior.
    """
    try:
        from kavi_runtime.trace_log import Trace as _Trace
        _trace_ctx = _Trace(
            channel="scheduler", config=config, capability="kavi-persona",
        )
    except Exception as _e:
        logger.debug("trace_log: Trace construction failed (continuing): %s", _e)
        _trace_ctx = None
    if _trace_ctx is None:
        return _correction_pattern_check_impl(config)
    with _trace_ctx:
        return _correction_pattern_check_impl(config)



def _correction_pattern_check_impl(config: dict) -> None:
    """Step 11: Examples-row proposer.

    Scan corrections.jsonl for unapplied + unrejected correction patterns. Group by
    (type, target_pattern). For groups with size >= correction_threshold_for_proposal,
    register a pending question with kind='examples_proposal' and send an iMessage
    proposal. Megha replies '<n> yes' or '<n> no'; the response handler appends to
    household.md (on yes) or rejected_patterns.jsonl (on no).

    Only three correction types map to Examples rows in v0.2:
      - missed → action='create'
      - false_positive → action='skip'
      - wrong_owner → action='create' with corrected owner

    The other three (wrong_title, wrong_source_tag, wrong_confidence) are per-task
    metadata fixes and don't generalize to email patterns; they stay in corrections.jsonl
    only.
    """
    PROMOTABLE_TYPES = {"missed", "false_positive", "wrong_owner"}

    corrections_path = Path(config["paths"]["corrections_jsonl"])
    promoted_path = Path(config["paths"]["promoted_patterns_jsonl"])
    rejected_path = Path(config["paths"]["rejected_patterns_jsonl"])
    state_path = Path(config["paths"]["imessage_state"])

    threshold = config["learning"]["correction_threshold_for_proposal"]
    threshold_after_rejection = config["learning"]["correction_threshold_after_rejection"]

    unapplied = read_unapplied_corrections(corrections_path, promoted_path=promoted_path)
    if not unapplied:
        logger.info("correction_pattern_check: no unapplied corrections")
        return

    rejected = read_rejected_patterns(rejected_path)

    # Group by (type, target_pattern). Only promotable types.
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in unapplied:
        c_type = c.get("type")
        c_pattern = c.get("target_pattern")
        if c_type in PROMOTABLE_TYPES and c_pattern:
            groups[(c_type, c_pattern)].append(c)

    if not groups:
        logger.info("correction_pattern_check: no promotable groups")
        return

    # Filter to groups that pass the threshold. Patterns previously rejected need a
    # higher count to surface again (correction_threshold_after_rejection).
    proposals = []
    for key, members in groups.items():
        required = threshold_after_rejection if key in rejected else threshold
        if len(members) >= required:
            proposals.append((key, members))

    if not proposals:
        logger.info("correction_pattern_check: no groups above threshold (size dist=%s)",
                    {k: len(v) for k, v in groups.items()})
        return

    # Skip if quiet hours — proposals can wait for 7 AM digest period.
    if _h_mod.is_quiet_hours(config):
        logger.info("correction_pattern_check: %d proposals deferred (quiet hours)", len(proposals))
        return

    _, _, bb = _h_mod._get_clients(config)

    for (c_type, c_pattern), members in proposals:
        # Build the Examples-row preview for the iMessage.
        first = members[0]
        action = "create" if c_type in {"missed", "wrong_owner"} else "skip"
        owner = "Megha"
        if c_type == "wrong_owner":
            owner = first.get("new_value", "Megha").title()
        signals_summary = first.get("reason", "")[:200]

        pending = _h_mod.list_pending_questions(state_path)
        question_index = len(pending) + 1

        # Examples-row proposal kept as templated for now (low-traffic path; full LLM
        # composition is a future migration). Reply syntax dropped 2026-05-04 per
        # persona spec ("Reply 1 yes / 1 no" syntax forbidden); regex parser still
        # handles examples_proposal replies as a backstop.
        proposal_text = (
            f"I've seen '{c_pattern}' come up {len(members)} times. "
            f"Add this Examples row to household.md?\n\n"
            f"  Trigger: {c_pattern}\n"
            f"  Action: {action}\n"
            f"  Owner: {owner}\n"
            f"  Signals: {signals_summary}"
        )

        add_pending_question(state_path, {
            "id": f"q-{uuid.uuid4().hex[:8]}",
            "kind": "examples_proposal",
            "task_title_rendered": f"Examples: {c_pattern}",
            "correction_type": c_type,
            "target_pattern": c_pattern,
            "action": action,
            "owner": owner,
            "signals": signals_summary,
            "contributing_correction_count": len(members),
            "asked_at": utc_now_iso(),
        })

        # Conformance-sweep finding (2026-06-10 provenance grandfather
        # pass): templated proposal, neither LLM-composed nor previously
        # audited. AUDIT 2026-06-10: honest question, no action claims,
        # asks before writing to household.md. Listed for future LLM
        # migration (low-traffic path; full LLM composition deferred).
        send_result = _h_mod._send_imessage_with_fallback(
            config, proposal_text, kind="examples_proposal",
            provenance={"fallback_audit": "2026-06-10"},
        )
        logger.info("correction_pattern_check: proposed type=%s pattern=%r count=%d q_index=%s verified=%s fallback=%s",
                    c_type, c_pattern[:60], len(members), question_index,
                    send_result["verified"], send_result["fallback_used"])





__all__ = [
    "correction_pattern_check",
    "_correction_pattern_check_impl",
]
