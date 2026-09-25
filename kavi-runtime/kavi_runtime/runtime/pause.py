"""Capability handler functions: _classify_pause_intent, _handle_resume, _handle_pause_ambiguous.

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
from datetime import datetime, timedelta, timezone
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




def _classify_pause_intent(text: str, state_path: Path, config: dict) -> dict[str, Any]:
    pause_state = get_pause_state(state_path)
    cap = config.get("guardrails", {}).get("monthly_anthropic_spend_usd_cap", 30)
    pause_context = {
        "paused_since": pause_state.get("paused_since"),
        "paused_reason": pause_state.get("paused_reason"),
        "cap_usd": cap,
    }
    state = None  # reserved for future per-call usage history
    _, claude, _ = _h_mod._get_clients(config)
    return claude.classify_pause_intent(text, pause_context)



def _handle_resume(state_path: Path, config: dict, intent_result: dict[str, Any]) -> dict[str, Any]:
    """Megha confirmed resume. Clear the pause flag, set the spend-cap bypass for this
    calendar month so the very next email doesn't immediately re-trip, drain the
    queued notifications through email_arrived, and ack with what changed."""
    _, _, bb = _h_mod._get_clients(config)
    pause_state_before = get_pause_state(state_path)
    paused_reason = pause_state_before.get("paused_reason") or "unknown"
    spend_at_trip = None
    if paused_reason == "spend_cap_exceeded":
        # pause_state.json carries paused_spend_usd when set_paused records it.
        spend_at_trip = load_pause_state(state_path).get("paused_spend_usd")

    # Bypass only when THIS month is actually over the cap (2026-09-23). A spend
    # pause that tripped in a prior month is stale once the month rolls over;
    # granting a bypass then would waive the cap for the whole new month, which
    # Megha ruled out ("don't raise the spend cap").
    cap = float(config.get("guardrails", {}).get("monthly_anthropic_spend_usd_cap", 30))
    set_bypass = (
        paused_reason == "spend_cap_exceeded"
        and compute_monthly_spend_usd(config) >= cap
    )
    drain_result = clear_paused(state_path, set_spend_bypass=set_bypass)
    queued = drain_result["drained_queue"]

    max_age_days = int(config.get("guardrails", {}).get("drain_max_age_days", 7))

    log_trip(
        _runtime_events_path(config),
        guardrail="pause_cleared",
        threshold=None,
        actual=len(queued),
        action="resume",
        extra={
            "paused_reason": paused_reason,
            "queued_count": len(queued),
            "intent_classifier_reason": intent_result.get("reason", "")[:200],
        },
    )

    ack_lines = []
    if paused_reason == "spend_cap_exceeded" and spend_at_trip is not None:
        ack_lines.append(f"Got it — running again from the ${spend_at_trip:.2f} spend pause.")
        if set_bypass:
            ack_lines.append("(I'll skip the cap for the rest of this month so I don't re-trip on the next email.)")
    else:
        ack_lines.append(f"Got it — running again. Pause reason was: {paused_reason}.")
    if queued:
        ack_lines.append(f"Catching up on emails from the last {max_age_days} days; older ones are skipped.")
    else:
        ack_lines.append("No queued emails to drain.")
    ack = "\n".join(ack_lines)

    # Conformance-sweep finding (2026-06-10 provenance grandfather pass):
    # deterministic resume ack. AUDIT 2026-06-10: ops-shaped per the
    # kavi-persona Out of scope rule (actionable ops messaging is
    # deterministic); counts are grounded in the real drain result.
    _h_mod._send_imessage_with_fallback(
        config, ack, kind="resume_ack",
        provenance={"fallback_audit": "2026-06-10"},
    )

    result = drain_paused_queue(state_path, config)
    return {**result, "status": "resume",
            "intent_classifier_reason": intent_result.get("reason")}


_DRAIN_LOCK = threading.Lock()


def drain_marker_path(state_path: Path) -> Path:
    """Exists while a drain is running. deploy.sh refuses to restart Kavi while
    it is present (a restart mid-drain is what lost 3,090 emails 2026-09-23)."""
    return Path(state_path).parent / "drain_in_progress"


def drain_paused_queue(state_path: Path, config: dict, *, announce: bool = True) -> dict[str, Any]:
    """Process the on-disk paused-email queue. Crash-safe (2026-09-23): each
    email is removed from disk only after it is handled; a failure stays queued
    and is dead-lettered after DRAIN_MAX_ATTEMPTS; a restart mid-drain leaves
    the remainder queued for the watchdog to resume. One drain at a time.

    Backlog window + held accounts (2026-09-23, Megha: "only go back 7 days"):
    older queued mail is skipped after the (free) Graph fetch, before any LLM
    call; mail for accounts in `drain_hold_accounts` (e.g. a mailbox whose
    sign-in is broken) stays queued untouched."""
    from kavi_runtime.runtime.guardrails import (
        queued_message_id, record_drain_failure, remove_paused_email,
    )
    if not _DRAIN_LOCK.acquire(blocking=False):
        logger.info("drain: another drain is already running; skipping")
        return {"status": "drain_already_running", "drained_count": 0}
    marker = drain_marker_path(state_path)
    try:
        marker.write_text(f"{utc_now_iso()} pid={__import__('os').getpid()}\n")
    except OSError:
        logger.warning("drain: could not write drain marker %s", marker)
    try:
        return _drain_locked(
            state_path, config, announce=announce,
            queued_message_id=queued_message_id,
            record_drain_failure=record_drain_failure,
            remove_paused_email=remove_paused_email,
        )
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
        _DRAIN_LOCK.release()


def _drain_locked(state_path, config, *, announce, queued_message_id,
                  record_drain_failure, remove_paused_email) -> dict[str, Any]:
    guard_cfg = config.get("guardrails", {})
    cap = float(guard_cfg.get("monthly_anthropic_spend_usd_cap", 30))
    max_age_days = int(guard_cfg.get("drain_max_age_days", 7))
    not_before = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    hold_accounts = {a.lower() for a in guard_cfg.get("drain_hold_accounts", []) or []}

    queued = list(load_pause_state(state_path).get("paused_email_queue", []))

    # Dedup the queued notifications by message_id before draining. MS Graph
    # re-fires each email webhook ~2x; on a large backlog drain the in-memory
    # 120s dedup cache is cold and TTL-bounded, so far-apart re-fires would
    # re-classify and burn spend. Collapsing to unique message_id here is
    # deterministic and TTL-independent (keeps the first occurrence; handling
    # it removes every queued copy from disk).
    deduped: list[Any] = []
    seen: set[str] = set()
    for n in queued:
        mid = queued_message_id(n)
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        deduped.append(n)
    dup_dropped = len(queued) - len(deduped)

    logger.info(
        "drain: %d queued notifications (%d duplicates collapsed)",
        len(deduped), dup_dropped,
    )
    # Reset inbound context before draining queued emails: each drained notification's
    # outbound (Q&A iMessage, etc.) is triggered by the email, not by Megha's resume
    # message. Without this, every drained-email outbound would record triggered_by =
    # Megha's "resume" inbound_id.
    from kavi_runtime.inbound_log import current_inbound_id
    # Local import: pause is a runtime module and the inbox-to-task handler
    # imports runtime helpers at module load — importing at the top would
    # be circular. Missing entirely until 2026-06-10 (lost-import class):
    # resuming with queued emails raised NameError per notification, which
    # the per-notif except swallowed into drain_errors.
    from capabilities.inbox_to_task.handler import email_arrived
    current_inbound_id.set(None)

    # Spend-guard the drain (added 2026-07-14). A large backlog drain (e.g. the
    # 912-email 17-day stuck-pause) is a thundering herd of classify calls. Re-
    # check month-to-date spend every K emails; if the drain would cross the
    # monthly cap, stop gracefully, re-pause on the spend reason (the rest stay
    # queued on disk), and say so. Do NOT lean on the per-call cap check to
    # re-pause mid-loop (the drain-retrips-cap loop in realtime-kavi.md).
    # The check honors the monthly bypass (fixed 2026-09-23): without it the
    # drain re-tripped on the first email of every post-trip resume, so
    # "resume" processed 0 emails and silently re-paused (Aug 11 → Sep 23).
    SPEND_CHECK_EVERY = 25
    drain_errors = 0
    dead_lettered = 0
    drained = 0
    expired = 0
    held = 0
    spend_stopped = False
    for i, notif in enumerate(deduped):
        acct = (notif.get("_source_account") or "").lower() if isinstance(notif, dict) else ""
        if acct and acct in hold_accounts:
            held += 1  # stays queued on disk untouched
            continue
        if (
            i % SPEND_CHECK_EVERY == 0
            and not is_spend_cap_bypassed(state_path)
            and compute_monthly_spend_usd(config) >= cap
        ):
            spend = compute_monthly_spend_usd(config)
            set_paused(state_path, "spend_cap_exceeded", spend_at_trip=spend)
            spend_stopped = True
            logger.warning(
                "drain: monthly spend $%.2f >= cap $%.2f; re-paused with %d emails still queued",
                spend, cap, len(deduped) - i,
            )
            # AUDIT 2026-07-14: deterministic ops sentence, honest counts, no
            # action claims beyond the real drain result; ≤120-char target.
            _h_mod._send_imessage_with_fallback(
                config,
                f"Drained {drained} of {len(deduped)} queued emails, then paused near the "
                f"${cap:.0f} monthly cap. Reply 'resume' to continue the rest.",
                kind="drain_spend_paused",
                provenance={"fallback_audit": "2026-07-14"},
            )
            break
        # Stashed source account survives the pause/resume round trip via
        # the `_source_account` field set in email_arrived's pause branch.
        stashed_account = notif.get("_source_account") if isinstance(notif, dict) else None
        stamped = {**notif, "_drain_not_before": not_before} if isinstance(notif, dict) else notif
        try:
            result = email_arrived(stamped, config, stashed_account)
        except Exception as e:
            logger.exception("drain: email_arrived failed: %s", e)
            drain_errors += 1
            if record_drain_failure(state_path, notif):
                dead_lettered += 1
            continue
        remove_paused_email(state_path, notif)
        if isinstance(result, dict) and result.get("reason") == "drain_window_expired":
            expired += 1
        else:
            drained += 1

    if announce and deduped and not spend_stopped:
        # AUDIT 2026-09-23: deterministic ops sentence with grounded counts
        # (kavi-persona Out of scope: actionable ops messaging is deterministic).
        parts = [f"Caught up: {drained} recent email{'s' if drained != 1 else ''} processed"]
        if expired:
            parts.append(f"{expired} older than {max_age_days} days skipped")
        if held:
            parts.append(f"{held} held for later")
        if drain_errors:
            parts.append(f"{drain_errors} failed and will be retried")
        _h_mod._send_imessage_with_fallback(
            config, ", ".join(parts) + ".",
            kind="drain_complete",
            provenance={"fallback_audit": "2026-09-23"},
        )

    return {
        "drained_count": drained,
        "expired_count": expired,
        "held_count": held,
        "duplicates_collapsed": dup_dropped,
        "drain_errors": drain_errors,
        "dead_lettered": dead_lettered,
        "spend_stopped": spend_stopped,
    }



def _handle_pause_ambiguous(state_path: Path, config: dict, text: str, intent_result: dict[str, Any]) -> dict[str, Any]:
    """Classifier returned ambiguous. Ask Megha to clarify rather than guessing."""
    _, _, bb = _h_mod._get_clients(config)
    pause_state = get_pause_state(state_path)
    reason = pause_state.get("paused_reason", "unknown")
    clarify = (
        f"Quick check — I'm paused right now ({reason}). "
        f"Did you mean to tell me to start running again, or were you talking about a task / email? "
        f"Reply 'resume' to clear the pause, or send your message again with the task name."
    )
    # Conformance-sweep finding (2026-06-10): deterministic clarify while
    # paused. AUDIT 2026-06-10: honest question, no action claims. Note:
    # the inbound dispatch no longer routes here (intent parser owns
    # pause/resume); kept for direct callers and the spend-cap pause path.
    _h_mod._send_imessage_with_fallback(
        config, clarify, kind="pause_ambiguous_clarify",
        provenance={"fallback_audit": "2026-06-10"},
    )
    return {"status": "pause_ambiguous", "intent_classifier_reason": intent_result.get("reason")}




__all__ = [
    "_classify_pause_intent",
    "_handle_resume",
    "_handle_pause_ambiguous",
]
