"""Capability handler functions: _load_recent_outbound, _log_action_intent_decision, _owner_prefix_from_sender, _parse_deadline_iso.

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


logger = logging.getLogger(__name__)




def _load_recent_outbound(config: dict, n: int = 5) -> list[dict[str, Any]]:
    """G-C2: read the last N outbound rows from eval-persona-outbound-judgments.jsonl
    so compose_conversational_reply has thread context. Returns empty list if file
    missing or unreadable. Failure-safe — conversation still works without context."""
    try:
        path = Path(config["paths"]["eval_persona_outbound_judgments_jsonl"])
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-n:] if rows else []
    except Exception as e:
        logger.warning("_load_recent_outbound failed (continuing without context): %s", e)
        return []



def _log_action_intent_decision(
    config: dict,
    *,
    free_text: str,
    intent: dict[str, Any],
    task_match: dict[str, Any] | None,
    decision: str,
    executed: bool,
) -> None:
    """Append one row to eval_persona_action_intent_jsonl. Captures every
    classifier decision (executed, dry-run, every fall-through reason) so
    Megha can review classifier accuracy before flipping action_layer.dry_run
    from true to false. Non-fatal on failure — never blocks an inbound."""
    try:
        path = Path(config["paths"]["eval_persona_action_intent_jsonl"])
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "free_text": (free_text or "")[:200],
            "intent": intent,
            "task_match_title": (task_match.get("title") if task_match else None),
            "task_match_id": (task_match.get("id") if task_match else None),
            "task_match_status": (task_match.get("status") if task_match else None),
            "decision": decision,
            "executed": executed,
        }
        with path.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        logger.warning("action_intent decision log failed (non-fatal): %s", e)



def _owner_prefix_from_sender(sender_handle: str | None, config: dict) -> str:
    """Default owner abbreviation for a create-task verb when the title doesn't
    name an owner explicitly. Maps the inbound iMessage sender handle to MJ
    (Megha) or MM (Max). Falls back to MJ — Megha is the default household
    requester and MJ is a safer default than `??`.

    Sender handles arrive as either an E.164 phone (`+1` + 10 digits) or an email
    (`megha@example.com`). Compares against config.imessage.megha_phone +
    own_email_addresses for Megha, and Max's phone from the private
    config's household identity (kavi_runtime/household.py).
    """
    if not sender_handle:
        return "MJ"
    s = sender_handle.lower().strip()
    megha_phone = (config.get("imessage", {}).get("megha_phone") or "").lower()
    own_emails = {a.lower() for a in config.get("imessage", {}).get("own_email_addresses", []) if a}
    if s == megha_phone or s in own_emails:
        return "MJ"
    # Max's iMessage handle (active 2026-05-05), from the private config.
    from kavi_runtime import household
    if s in household.member_phones("max"):
        return "MM"
    return "MJ"



def _parse_deadline_iso(text: str) -> Any:
    """Best-effort ISO date extractor for the create-task verb. Returns a
    datetime when the text contains an obvious `YYYY-MM-DD` substring;
    returns None for natural-language phrases like "tomorrow" or "Friday"
    (left to a future enhancement). The title body retains the phrase so the
    user still sees the deadline in the task title even when the structured
    dueDateTime is empty — acceptance criterion (c) says "left empty
    otherwise."
    """
    if not text:
        return None
    m = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if not m:
        return None
    try:
        from datetime import datetime as _dt
        return _dt(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _load_recent_inbound(
    config: dict, sender_handle: str | None, n: int = 5,
) -> list[str]:
    """Read the SENDER'S own last N inbound texts from
    eval-persona-inbound.jsonl (oldest first). The her-side context the old
    dispatcher never carried — the 2026-06-10 "I don't have context on
    others from this thread" failure class. Failure-safe: missing file or
    unknown sender returns []. Call BEFORE log_inbound for the current
    message so the row under dispatch is excluded."""
    try:
        path = Path(config["paths"]["eval_persona_inbound_jsonl"])
        if not path.exists():
            return []
        texts: list[str] = []
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if sender_handle and row.get("sender") and row.get("sender") != sender_handle:
                    continue
                text = row.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
        return texts[-n:] if texts else []
    except Exception as e:
        logger.warning("_load_recent_inbound failed (continuing without): %s", e)
        return []


__all__ = [
    "_load_recent_outbound",
    "_load_recent_inbound",
    "_log_action_intent_decision",
    "_owner_prefix_from_sender",
    "_parse_deadline_iso",
]
