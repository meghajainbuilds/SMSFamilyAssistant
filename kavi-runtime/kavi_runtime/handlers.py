"""Event handlers. Called from server.py and scheduler.py.

Phase 4 (2026-06-02) physical move: cross-cutting plumbing
(`_get_clients`, `_send_imessage_with_fallback`, `send_imessage_raw`,
`_send_imessage_with_fallback_and_context`, dedup cache,
`_check_spend_cap_after_call`, `_send_or_queue_alert`, `_latency_sec`,
`_decision_id`, `_runtime_events_path`) moved to `kavi_runtime/runtime/`.
This module imports them back so existing call sites inside handlers.py
keep working. Capability-specific handlers (email_arrived,
imessage_received, periodic_summary, action layer, qa loop) still live
here pending per-capability Phase 4.x moves.
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

from kavi_runtime.runtime.guardrails import (
    clear_paused,
    compute_monthly_spend_usd,
    drain_pending_alerts,
    enqueue_paused_email,
    enqueue_pending_alert,
    get_pause_state,
    is_paused,
    is_spend_cap_bypassed,
    log_trip,
    set_paused,
)
from kavi_runtime.state import (
    add_pending_question,
    append_correction,
    append_promoted_pattern,
    append_rejected_pattern,
    append_run,
    clear_pending_clarification,
    clear_summary_anchor,
    drain_summary_queue,
    drop_expired_questions,
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
from capabilities.coordination.dispatch import _try_handle_coordination_intent  # noqa: F401

# inbox-to-task helpers physically moved to capabilities/inbox_to_task/ in
# Phase 4 (2026-06-02). Imported back so handlers.py's in-file callers
# (`_email_arrived_impl`, etc.) keep working transparently.
from capabilities.inbox_to_task.selection import (  # noqa: F401
    _normalize_email,
    _build_thread_state,
    _inbox_owner_abbrev,
    _account_to_owner_name,
)
from capabilities.inbox_to_task.task_writer import (  # noqa: F401
    _apply_lifecycle_update,
    _resolve_shared_list_id,
)
from capabilities.inbox_to_task.eval_log import (  # noqa: F401
    _DECISION_MAP,
    _eval_inbox_judgments_path,
    _log_email_event,
    _log_webhook_redup_hit,
)

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
# Runtime re-export so the scheduler (a capability) schedules the coordination
# follow-up sweep through the runtime package, not via a forbidden
# capability->capability import (test_capability_isolation).
from capabilities.coordination.handler import (  # noqa: F401
    run_coordination_followup_sweep,
)
from capabilities.kavi_persona.actions.title_edits import (  # noqa: F401
    _strip_kavi_prefix,
    _swap_owner_in_title,
    _replace_body_in_title,
    _replace_tag_in_title,
    _adjust_confidence_marker,
)

# QA_REPLY_PATTERN ("1 yes" legacy regex) DELETED 2026-06-10 with the
# intent-first dispatch rebuild; the intent parser reads "1 yes" as
# natural language like everything else.
REACTION_PATTERN = re.compile(r"^(Liked|Disliked|Loved|Laughed at|Emphasized|Questioned)\s", re.IGNORECASE)
MIN_CORRECTION_TEXT_LEN = 5
OWNER_ABBREV = {"megha": "MJ", "max": "MM"}

logger = logging.getLogger(__name__)


# _eval_inbox_judgments_path, _DECISION_MAP, _log_email_event,
# _normalize_email, _build_thread_state, _inbox_owner_abbrev,
# _account_to_owner_name physically moved to capabilities/inbox_to_task/
# in Phase 4 (2026-06-02). Imported above.


_G_A1_ALERT_FALLBACK_TEXT = (
    "I caught myself about to claim that as done. Re-ask as a "
    "direct request and I'll route it."
)

# Kinds where G-A1 fail-closed applies on the BROAD action-verb check —
# every other kind logs the violation but ships the message.
G_A1_FAIL_CLOSED_KINDS = frozenset({
    "conversational",
    "post_action_reply",
    "qa_ack",
})

# Kinds where fail-closed applies on the NARROWER past-tense state-claim
# check (FORBIDDEN_CLARIFY_STATE_CLAIM_PHRASES). action_clarifying paths
# legitimately use action verbs in question form ("Confirm to mark done?")
# so the broad verb gate over-fires on legitimate questions; but they
# cannot legitimately make past-tense state claims because no tool ran in
# context. The 2026-05-08 second pass adds clarifying paths to fail-closed
# under THIS narrower rule. The production trace shipped "the volunteering/
# food donation decision is already showing completed" — that's a state
# claim, not a question, and would have been caught by this gate.
G_A1_STATE_CLAIM_FAIL_CLOSED_KINDS = frozenset({
    "action_clarifying",
})


_TOPIC_STOPWORDS = frozenset({
    "the", "and", "with", "from", "this", "that", "for",
    "your", "have", "into", "about", "yes", "marked",
    "done", "task", "tasks",
})


# ----- Phase 4 (2026-06-02) physical move: function bodies live in capabilities/ -----
# These star-imports keep legacy callers working: `from kavi_runtime.handlers
# import _try_handle_action_intent` resolves transparently through here. New
# code should import directly from the capability file.

from capabilities.inbox_to_task.handler import *  # noqa: F401,F403,E402
from kavi_runtime.runtime.imessage_dispatch import *  # noqa: F401,F403,E402
from kavi_runtime.runtime.pause import *  # noqa: F401,F403,E402
from capabilities.kavi_persona.utils import *  # noqa: F401,F403,E402
from capabilities.kavi_persona.actions.create_task import *  # noqa: F401,F403,E402
from capabilities.kavi_persona.actions.handler import *  # noqa: F401,F403,E402
from capabilities.kavi_persona.qa_loop.handler import *  # noqa: F401,F403,E402
from capabilities.kavi_persona.correction_pattern_check import *  # noqa: F401,F403,E402
