"""Capability handler functions: email_arrived, _email_arrived_impl.

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

from kavi_runtime.runtime.guardrails import enqueue_paused_email, get_pause_state, log_trip











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
# REMOVED CROSS-CAP IMPORT (unused): from capabilities.kavi_persona.selection import (  # noqa: F401
# REMOVED CROSS-CAP IMPORT (unused):     _fetch_open_todo_task_ids,
# REMOVED CROSS-CAP IMPORT (unused):     _filter_summary_queue_by_open_status,
# REMOVED CROSS-CAP IMPORT (unused):     _filter_pending_questions_by_open_status,
# REMOVED CROSS-CAP IMPORT (unused):     _count_pending_facts,
# REMOVED CROSS-CAP IMPORT (unused):     _read_pending_facts_for_summary,
# REMOVED CROSS-CAP IMPORT (unused):     _pick_summary_anchor,
# REMOVED CROSS-CAP IMPORT (unused): )
# REMOVED CROSS-CAP IMPORT (unused): from capabilities.kavi_persona.composers.periodic_summary import (  # noqa: F401
# REMOVED CROSS-CAP IMPORT (unused):     periodic_summary,
# REMOVED CROSS-CAP IMPORT (unused):     _periodic_summary_impl,
# REMOVED CROSS-CAP IMPORT (unused):     _PERIODIC_SUMMARY_SAFE_FALLBACK,
# REMOVED CROSS-CAP IMPORT (unused):     PERIODIC_SUMMARY_DEBOUNCE_HOURS,
# REMOVED CROSS-CAP IMPORT (unused):     _periodic_summary_state_path,
# REMOVED CROSS-CAP IMPORT (unused):     _periodic_summary_input_hash,
# REMOVED CROSS-CAP IMPORT (unused):     _load_periodic_summary_last_hash,
# REMOVED CROSS-CAP IMPORT (unused):     _save_periodic_summary_last_hash,
# REMOVED CROSS-CAP IMPORT (unused):     _should_suppress_periodic_summary,
# REMOVED CROSS-CAP IMPORT (unused): )
# REMOVED CROSS-CAP IMPORT (unused): from capabilities.kavi_persona.actions.title_edits import (  # noqa: F401
# REMOVED CROSS-CAP IMPORT (unused): 
# REMOVED CROSS-CAP IMPORT (unused): 
# REMOVED CROSS-CAP IMPORT (unused):     _strip_kavi_prefix,
# REMOVED CROSS-CAP IMPORT (unused):     _swap_owner_in_title,
# REMOVED CROSS-CAP IMPORT (unused):     _replace_body_in_title,
# REMOVED CROSS-CAP IMPORT (unused):     _replace_tag_in_title,
# REMOVED CROSS-CAP IMPORT (unused):     _adjust_confidence_marker,
# REMOVED CROSS-CAP IMPORT (unused): )

# Late-binding handle so tests that patch handlers._foo flow through:
import kavi_runtime.handlers as _h_mod  # noqa: E402

# Sibling utils used by multiple action/qa handler functions:
# REMOVED CROSS-CAP IMPORT (unused): from capabilities.kavi_persona.utils import (  # noqa: E402, F401
# REMOVED CROSS-CAP IMPORT (unused):     _owner_prefix_from_sender,
# REMOVED CROSS-CAP IMPORT (unused):     _log_action_intent_decision,
# REMOVED CROSS-CAP IMPORT (unused):     _load_recent_outbound,
# REMOVED CROSS-CAP IMPORT (unused):     _parse_deadline_iso,
# REMOVED CROSS-CAP IMPORT (unused): )


logger = logging.getLogger(__name__)




def email_arrived(
    notification: dict[str, Any], config: dict, source_account: str | None = None,
) -> dict[str, Any]:
    """Handle one MS Graph 'created' notification: fetch, judge, write, notify.

    `source_account` (added 2026-05-05 evening, multi-account refactor)
    identifies which Microsoft mailbox the webhook fired against. The
    webhook router in `server.py` resolves it from the inbound
    notification's `subscriptionId` and passes it here. Every Graph call
    that follows uses this account's token + this account's view of the
    shared MS To Do list. When `source_account` is None (legacy path),
    the GraphClient routes through its default account (Megha) so single-
    account installs keep working unchanged.

    Phase B unified-trace (2026-05-12). Wraps the body in a Trace context
    so downstream LLM/tool/outbound activity records into one JSONL row
    per email exchange under evals/traces/exchanges.jsonl. The Trace is
    failure-safe — its construction or write failures must NEVER break
    email handling.
    """
    try:
        from kavi_runtime.trace_log import Trace as _Trace
        _trace_ctx = _Trace(
            channel="email", participant=source_account, config=config,
            capability="inbox-to-task",
        )
    except Exception as _e:
        logger.debug("trace_log: Trace construction failed (continuing): %s", _e)
        _trace_ctx = None
    if _trace_ctx is None:
        return _email_arrived_impl(notification, config, source_account)
    with _trace_ctx:
        return _email_arrived_impl(notification, config, source_account)



def _email_arrived_impl(
    notification: dict[str, Any], config: dict, source_account: str | None = None,
) -> dict[str, Any]:
    """Inner implementation of email_arrived (Phase B refactor). The outer
    function wraps this in a Trace context manager so downstream LLM, tool,
    and outbound activity auto-mirror into the unified trace JSONL row.

    In-flight claim (2026-09-23): a concurrent Graph re-fire of a message that
    is still being processed returns `webhook_redup_hit` immediately instead
    of paying for a second fetch + judgment. See runtime/dedup.py."""
    from kavi_runtime.runtime.dedup import _claim_in_flight, _release_in_flight
    rd = notification.get("resourceData") or {}
    claim_id = rd.get("id") or notification.get("resource", "").split("/")[-1]
    if claim_id and not _claim_in_flight(claim_id):
        logger.info(
            "email_arrived: IN-FLIGHT REDUP HIT id=%s (skipping fetch + LLM)",
            claim_id[:16],
        )
        _log_webhook_redup_hit(
            config,
            email_payload=None,
            message_id=claim_id,
            prior_outcome={"decision": "in_flight", "task_id": None, "decision_id": None},
            branch="in_flight",
            source_account=source_account,
        )
        result = {
            "status": "webhook_redup_hit",
            "message_id": claim_id,
            "suppressed": True,
            "prior_decision": "in_flight",
            "task_id": None,
        }
        _record_trace_outcome(result)
        return result
    try:
        result = _email_arrived_body(notification, config, source_account)
        _record_trace_outcome(result)
        return result
    finally:
        if claim_id:
            _release_in_flight(claim_id)


def email_outcome(result: Any) -> str:
    """What happened to the email, for the unified trace (added 2026-09-23).

    Before this, `exchange_outcome` was derived only from whether Kavi sent an
    iMessage, so every email that became a task without a question iMessage
    was logged `skipped_silently` (hundreds of real tasks a month)."""
    if not isinstance(result, dict):
        return "unknown"
    status = result.get("status")
    if status == "webhook_redup_hit":
        return "webhook_redup_hit"
    if status == "paused_skipped":
        return "paused"
    if status == "skipped":
        return "skipped"
    if status:
        return str(status)
    if result.get("dedup"):
        return "dedup_hit"
    if result.get("error"):
        return "errored"
    if result.get("guardrail"):
        return f"guardrail_{result['guardrail']}"
    if result.get("lifecycle_state") and result.get("task_id"):
        return "task_updated"
    if result.get("task_id"):
        return "task_created"
    return "skipped"


def _record_trace_outcome(result: Any) -> None:
    try:
        from kavi_runtime.trace_log import Trace
        t = Trace.current()
        if t is not None:
            t.set_exchange_outcome(email_outcome(result))
    except Exception as e:  # tracing must never break email handling
        logger.debug("trace outcome record failed: %s", e)


def _email_arrived_body(
    notification: dict[str, Any], config: dict, source_account: str | None = None,
) -> dict[str, Any]:
    """Body of `_email_arrived_impl`, run while holding the in-flight claim."""
    state_path = Path(config["paths"]["imessage_state"])

    # Webhook re-fire suppression (added 2026-05-27). MS Graph re-fires the
    # email-arrival webhook roughly twice per email (median gap 15 sec). If
    # we already decided on this message_id within _DEDUP_TTL_SEC, skip the
    # Graph fetch + LLM call entirely. Log a `webhook_redup_hit` row so
    # the eval viewer sees the re-fire as suppressed (not as a fresh
    # decision). Cost saved: ~$0.05 per re-fire on skip-path traffic.
    #
    # Why this check lives this early: it MUST run before the pause-state
    # branch (a paused-then-resumed email already paid the dedup cost on
    # the original arrival; the re-fire of the same paused notification
    # should suppress here too) AND before the expensive graph.fetch_message
    # call below. The message_id is computed before this block.
    resource_data_early = notification.get("resourceData") or {}
    early_message_id = (
        resource_data_early.get("id")
        or notification.get("resource", "").split("/")[-1]
    )
    if early_message_id:
        cached_early = _h_mod._dedup_check(early_message_id)
        if cached_early:
            cached_task_id_early = cached_early.get("task_id") or ""
            logger.info(
                "email_arrived: WEBHOOK REDUP HIT id=%s prior_decision=%s (skipping fetch + LLM)",
                early_message_id[:16],
                cached_early.get("decision"),
            )
            _log_webhook_redup_hit(
                config,
                email_payload=None,
                message_id=early_message_id,
                prior_outcome=cached_early,
                branch="entry",
                source_account=source_account,
            )
            return {
                "status": "webhook_redup_hit",
                "message_id": early_message_id,
                "suppressed": True,
                "prior_decision": cached_early.get("decision"),
                "task_id": cached_task_id_early or None,
            }

    # Step 12 guardrail: if the runtime is paused (spend cap trip), persist this
    # notification verbatim and skip the Sonnet call. On resume, the saved queue
    # drains through this same handler.
    if _h_mod.is_paused(state_path):
        # Stash the source account on the queued payload so the resume drain can
        # pass the same account back into email_arrived. Otherwise queued Max
        # emails would replay through Megha's account on resume.
        if source_account:
            notification = {**notification, "_source_account": source_account}
        enqueue_paused_email(state_path, notification)
        resource_data_p = notification.get("resourceData") or {}
        msg_id_p = resource_data_p.get("id") or notification.get("resource", "").split("/")[-1] or "?"
        logger.info(
            "email_arrived: PAUSED — queued notification id=%s account=%s",
            str(msg_id_p)[:16], source_account or "default",
        )
        _log_email_event(
            config,
            message_id=msg_id_p,
            status="paused_skipped",
            paused_reason=get_pause_state(state_path).get("paused_reason"),
            source_account=source_account,
        )
        if msg_id_p and msg_id_p != "?":
            _h_mod._dedup_record(msg_id_p, {
                "decision": "paused",
                "task_id": None,
                "decision_id": None,
            })
        return {"status": "paused_skipped", "message_id": msg_id_p}

    # If a queued notification carries a stashed source_account from a prior
    # paused enqueue, prefer it over a None argument so the drain replays
    # through the right account.
    if source_account is None:
        stashed = notification.get("_source_account") if isinstance(notification, dict) else None
        if stashed:
            source_account = stashed

    graph, claude, bb = _h_mod._get_clients(config)

    resource_data = notification.get("resourceData") or {}
    message_id = resource_data.get("id") or notification.get("resource", "").split("/")[-1]
    if not message_id:
        logger.warning("notification has no message id: %s", notification)
        return {"status": "skipped", "reason": "no_message_id"}

    msg = graph.fetch_message(message_id, account=source_account)
    if msg is None:
        # Stale notification — message gone from inbox (deleted, moved, or
        # already processed). Microsoft replays buffered notifications during
        # outage recovery; treating those as failures fires false-alarm rate
        # alerts (2026-05-06 PM incident). Skip cleanly so the wrapper
        # records a success, not a failure.
        logger.info(
            "email_arrived: id=%s skipping stale notification (message gone from inbox)",
            message_id[:16],
        )
        _h_mod._dedup_record(message_id, {
            "decision": "skipped",
            "task_id": None,
            "decision_id": None,
        })
        return {
            "status": "skipped",
            "reason": "stale_notification_404",
            "message_id": message_id,
        }

    # Backlog drain window (added 2026-09-23). A resume drain stamps
    # `_drain_not_before`; mail received before it is skipped here, after the
    # free Graph fetch and before any LLM call, so a long pause can't flood the
    # shared list with expired tasks or burn spend on stale mail.
    not_before = notification.get("_drain_not_before")
    received = msg.get("receivedDateTime")
    try:
        drain_expired = bool(not_before and received) and (
            datetime.fromisoformat(received.replace("Z", "+00:00"))
            < datetime.fromisoformat(not_before)
        )
    except ValueError:
        drain_expired = False  # unparseable date: process rather than drop
    if drain_expired:
        logger.info(
            "email_arrived: id=%s skipping drain-expired email (received %s)",
            message_id[:16], received,
        )
        _h_mod._dedup_record(message_id, {
            "decision": "skipped",
            "task_id": None,
            "decision_id": None,
        })
        return {
            "status": "skipped",
            "reason": "drain_window_expired",
            "message_id": message_id,
        }

    # Belt-and-suspenders scope filter (added 2026-04-29). The Graph subscription
    # is now scoped to /me/mailFolders/inbox/messages (setup_graph_auth.py), so
    # webhooks should only fire for inbox emails. This client-side check protects
    # against subscription-scope regression: even if a future setup creates a
    # /me/messages subscription, we won't process Junk/Other/Sweep/Archive mail.
    # Failure-mode choice: if inbox_folder_id lookup fails we PROCEED (don't fail
    # closed) — better to handle a stray Junk than drop a real inbox event during
    # a transient Graph hiccup.
    inbox_folder_id = graph.inbox_folder_id(account=source_account)
    parent_folder_id = msg.get("parentFolderId")
    if inbox_folder_id and parent_folder_id and parent_folder_id != inbox_folder_id:
        logger.info(
            "email_arrived: id=%s skipping non-inbox folder (parent=%s)",
            message_id[:16],
            parent_folder_id[:16],
        )
        _h_mod._dedup_record(message_id, {
            "decision": "skipped",
            "task_id": None,
            "decision_id": None,
        })
        return {
            "status": "skipped",
            "reason": "non_inbox_folder",
            "message_id": message_id,
            "parent_folder_id": parent_folder_id,
        }

    email_payload = _normalize_email(msg)
    # Stamp the source account on the payload so downstream loggers and the
    # email-to-task LLM context can both use it without an extra parameter.
    if source_account:
        email_payload["source_account"] = source_account

    # Phase B trace mirror (2026-05-12). Populate inbound on the active Trace
    # so the unified JSONL row captures the email's identity.
    try:
        from kavi_runtime.trace_log import current as _trace_current
        _t = _trace_current()
        if _t is not None:
            _body = email_payload.get("body_text") or ""
            _t.record_inbound(
                inbound_id=email_payload.get("id"),
                ts=email_payload.get("received") or utc_now_iso(),
                text=_body,
                sender=email_payload.get("from_address"),
                source="email",
                char_count=len(_body),
            )
    except Exception as _trace_err:
        logger.debug("handlers: trace record_inbound (email) failed: %s", _trace_err)

    # Skip emails the inbox owner sent themselves (their own outbound). Graph
    # fires webhooks for both inbox and sent items; sent-by-self mail isn't
    # actionable. The legacy single-account list covers Megha; for Max we
    # also recognize his primary + alias as own-outbound when his inbox is
    # the source account.
    own_addrs = {a.lower() for a in config.get("imessage", {}).get("own_email_addresses", []) if a}
    from kavi_runtime import household as _household
    if source_account == _household.primary_email("max"):
        own_addrs = own_addrs | set(_household.member_emails("max"))
    elif source_account is not None:
        own_addrs = own_addrs | {source_account.lower()}
    sender_addr = (email_payload.get("from_address") or "").lower()
    if own_addrs and sender_addr in own_addrs:
        logger.info(
            "email_arrived: id=%s account=%s skipping own sent mail (from=%s)",
            message_id[:16], source_account or "default", sender_addr,
        )
        _h_mod._dedup_record(message_id, {
            "decision": "skipped",
            "task_id": None,
            "decision_id": None,
        })
        return {"status": "skipped", "reason": "own_outbound", "message_id": message_id}
    conv_id = msg.get("conversationId")
    thread_inbound = []
    thread_sent = []
    if conv_id:
        try:
            thread_inbound = graph.fetch_thread(conv_id, account=source_account)
            thread_sent = graph.fetch_sentitems_replies(conv_id, account=source_account)
        except Exception as e:
            logger.warning("thread fetch failed (continuing without): %s", e)

    thread_state = _build_thread_state(thread_inbound, thread_sent, email_payload["received"])
    email_payload["thread_state"] = thread_state

    # Package lifecycle extraction (added 2026-05-05; spec: capabilities/inbox-
    # to-task.md "Package lifecycle"). Pure function over subject + body + sender.
    # When a package_id is found AND it matches an existing open task (tier 1 =
    # exact id; tier 2 = heuristic merchant + recipient + 7d window), route to
    # the UPDATE path instead of the LLM judgment path. First-occurrence package
    # emails (no match) fall through to the existing LLM path so Examples +
    # judgment can decide whether to create a task at all.
    pkg = package_extractor.extract(
        subject=email_payload.get("subject", ""),
        body=email_payload.get("body_text", ""),
        sender=email_payload.get("from_address", ""),
    )
    package_id = pkg.get("package_id")
    merchant = pkg.get("merchant")
    carrier = pkg.get("carrier")
    lifecycle_state_hint = pkg.get("lifecycle_state_hint")
    # Stamp on email_payload so the lifecycle templates can reference merchant
    # cleanly without re-deriving from sender.
    if merchant:
        email_payload["merchant"] = merchant

    # Amazon shipment noise suppression (Megha 2026-06-22): the household tracks
    # Amazon orders themselves; Kavi creating a task per Amazon ship/delivery
    # email is pure noise. Skip Amazon emails that are shipment-lifecycle events
    # (carrier == amazon AND a lifecycle state was detected) entirely — no
    # create, no lifecycle update. A genuine Amazon ACTION email (return window,
    # payment problem) carries NO lifecycle_state_hint, so it still flows to the
    # LLM judgment path below and can become a task.
    _is_amazon = (carrier == "amazon") or (str(merchant or "").strip().lower() == "amazon")
    if _is_amazon and lifecycle_state_hint:
        logger.info(
            "email_arrived: id=%s skipping Amazon shipment email (state=%s) — "
            "household tracks Amazon orders itself", message_id[:16], lifecycle_state_hint,
        )
        _h_mod._dedup_record(message_id, {
            "decision": "skipped", "task_id": None, "decision_id": None,
        })
        _log_email_event(
            config,
            email_payload=email_payload,
            message_id=message_id,
            subject=email_payload.get("subject", "")[:200],
            status="skipped",
            skip_reason="amazon_shipment_noise",
            package_id=package_id,
            lifecycle_state=lifecycle_state_hint,
            source_account=source_account,
        )
        return {
            "status": "skipped",
            "reason": "amazon_shipment_noise",
            "message_id": message_id,
        }

    if lifecycle_state_hint:
        list_id_for_match = _resolve_shared_list_id(graph, source_account, config)
        merge_target: str | None = None
        merge_tier: str = "none"
        merge_reason: str | None = None

        # Tier 1: exact id match (silent auto-merge).
        if package_id:
            try:
                merge_target = graph.find_todo_task_by_package_id(
                    list_id_for_match, package_id, account=source_account,
                )
            except Exception as e:
                logger.warning("lifecycle: tier1 lookup failed (continuing): %s", e)
                merge_target = None
            if merge_target:
                merge_tier = "tier1_id"

        # Tier 2: heuristic match (merchant + recipient + 7-day window). Only
        # runs when tier 1 missed AND we have a merchant. Recipient is the
        # inbox owner — Megha for Megha's stream, Max for Max's stream.
        if not merge_target and merchant:
            from kavi_runtime import household as _household
            recipient = "Max" if source_account == _household.primary_email("max") else "Megha"
            try:
                t2_id, t2_reason = graph.find_todo_task_by_heuristic(
                    list_id_for_match,
                    merchant=merchant,
                    recipient=recipient,
                    account=source_account,
                )
            except Exception as e:
                logger.warning("lifecycle: tier2 lookup failed (continuing): %s", e)
                t2_id, t2_reason = None, ""
            if t2_id:
                merge_target = t2_id
                merge_tier = "tier2_heuristic"
                merge_reason = t2_reason

        if merge_target:
            # Webhook idempotency on the lifecycle branch (added 2026-05-05). MS
            # Graph delivers webhooks at-least-once; without this dedup, a
            # duplicate notification for a Shipped/OFD/Delivered email produces
            # two audit lines AND (for OFD) two iMessage pings. The natural key
            # is `message_id` — a single email_id always carries exactly one
            # transition today. The same `_per_message_lock` + `_dedup_check` /
            # `_dedup_record` pattern already protecting the LLM-task-creation
            # branch (~line 697) is reused here so the two paths share one
            # dedup window.
            with _per_message_lock(message_id):
                cached = _h_mod._dedup_check(message_id)
                if cached:
                    cached_task_id = cached.get("task_id") or ""
                    logger.info(
                        "  -> DEDUP HIT (memory, lifecycle): already processed in this window (target=%s)",
                        cached_task_id[:12],
                    )
                    _log_webhook_redup_hit(
                        config,
                        email_payload=email_payload,
                        message_id=message_id,
                        prior_outcome=cached,
                        branch="lifecycle",
                        source_account=source_account,
                    )
                    return {
                        "ts": utc_now_iso(),
                        "message_id": message_id,
                        "task_id": cached_task_id,
                        "dedup": "memory",
                        "branch": "lifecycle",
                    }
                lifecycle_result = _apply_lifecycle_update(
                    existing_task_id=merge_target,
                    lifecycle_state=lifecycle_state_hint,
                    email_payload=email_payload,
                    config=config,
                    package_id=package_id,
                    package_match_tier=merge_tier,
                    merge_reason=merge_reason,
                    message_id=message_id,
                    source_account=source_account,
                )
                _h_mod._dedup_record(message_id, {
                    "decision": "updated",
                    "task_id": merge_target,
                    "decision_id": lifecycle_result.get("decision_id") if isinstance(lifecycle_result, dict) else None,
                })
                return lifecycle_result
        # else: no match — fall through to first-occurrence path below. The
        # eval row emitted by the LLM path will carry package_id / merchant
        # via the runtime so the weekly trace can surface the package marker
        # correctly even on first-occurrence creates. Stamp them on the
        # downstream context here.

    # Spoofed-sender v0 detection (added 2026-05-06; Fix 2 of audit follow-up).
    # When the display name contains a brand token (IRS, treasury, bank,
    # paypal, amazon, apple, microsoft, google, chase, wells fargo, bofa)
    # but the from-address domain is NOT legitimate for that brand, stamp
    # spoof_suspected=True. Downstream, the resulting task title gets a
    # `[VERIFY SENDER]` prefix and the audit row carries
    # `spoof_suspected: <brand>` so Megha can spot which brand was being
    # impersonated without opening the email.
    from kavi_runtime import financial_domains as _fin_spoof
    spoof_suspected, spoof_brand = _fin_spoof.detect_spoof(
        from_name=email_payload.get("from_name"),
        from_address=email_payload.get("from_address"),
    )
    if spoof_suspected:
        logger.warning(
            "email_arrived: spoof_suspected brand=%s display_name=%r from=%s message_id=%s",
            spoof_brand, email_payload.get("from_name", "")[:80],
            email_payload.get("from_address", "")[:80], message_id[:16],
        )
        email_payload["spoof_suspected"] = True
        email_payload["spoof_brand"] = spoof_brand

    # Inbox pre-filter SHADOW MODE (added 2026-05-06, REC-1 from audits/
    # token_optimization_2026-05-06.md). Today the pre-filter logs matches
    # but does NOT skip the LLM call. After 7 days of clean shadow data,
    # Megha will promote by flipping `inbox_pre_filter.shadow_mode` to False
    # and the same call site will return a synthetic `skipped` row to
    # eval-inbox-judgments.jsonl before the LLM runs.
    pre_filter_match = False
    pre_filter_reason: str | None = None
    pre_filter_kept_by: str | None = None
    try:
        from kavi_runtime import inbox_pre_filter as _pre_filter
        pre_filter_match, pre_filter_reason = _pre_filter.would_pre_filter_skip(email_payload)
        if pre_filter_match:
            pre_filter_kept_by = _pre_filter.keep_rule(
                email_payload, (config.get("inbox_pre_filter") or {}).get("keep"),
            )
    except Exception as e:
        logger.warning("inbox_pre_filter check failed (continuing without): %s", e)
        pre_filter_match = False
        pre_filter_reason = None
        pre_filter_kept_by = None

    if pre_filter_match:
        from kavi_runtime.structured_log import log_event
        log_event(
            "pre_filter", "shadow_kept" if pre_filter_kept_by else "shadow_skip",
            email_id=message_id,
            sender=email_payload.get("from_address", ""),
            reason=pre_filter_reason,
            kept_by=pre_filter_kept_by,
            subject_first_60=(email_payload.get("subject") or "")[:60],
        )
        # Future: when shadow_mode flips to False, short-circuit here (only
        # when pre_filter_kept_by is None) with a synthetic skipped result +
        # eval row write. Today we fall through to the LLM call.

    # Inject unapplied corrections into the email_to_tasks prompt context. They
    # land in the user message (per-call), so cached prefix is unaffected. Cost grows
    # with corrections list — acceptable for v0.2; step 11 promotes recurring patterns
    # into household.md Examples and we'll prune corrections.jsonl from there.
    corrections_path = Path(config["paths"]["corrections_jsonl"])
    promoted_path = Path(config["paths"]["promoted_patterns_jsonl"])
    applicable = read_unapplied_corrections(corrections_path, promoted_path=promoted_path)
    result = claude.run_email_to_tasks(email_payload, thread_messages=thread_inbound, applicable_corrections=applicable)

    # Pair the shadow_skip record with the LLM's actual decision so Megha
    # can compare what shadow caught vs what the LLM judged. Run AFTER the
    # LLM call so we know the decision; failure to write must not block
    # anything downstream.
    if pre_filter_match:
        try:
            from kavi_runtime import inbox_pre_filter as _pre_filter
            # Runtime data lives with the other runtime metrics under the
            # HomeOS data tree (fixed 2026-09-23). The old
            # Path(__file__)-relative dir resolved inside capabilities/ after
            # the Jun 2 move, and every deploy's rsync --delete wiped it.
            _paths = config.get("paths", {})
            _metrics = _paths.get("metrics_dir") or (
                Path(_paths["runtime_events_jsonl"]).parent
                if _paths.get("runtime_events_jsonl") else None
            )
            if _metrics is None:
                raise RuntimeError("no paths.metrics_dir or paths.runtime_events_jsonl configured")
            metrics_dir = Path(_metrics)
            llm_decision = result.get("status") if isinstance(result, dict) else None
            llm_reason = result.get("reason") if isinstance(result, dict) else None
            _pre_filter.write_shadow_log(
                metrics_dir,
                email_id=message_id,
                sender=email_payload.get("from_address", ""),
                subject=(email_payload.get("subject") or "")[:200],
                reason=pre_filter_reason or "",
                llm_decision=llm_decision,
                llm_reason=llm_reason,
                kept_by=pre_filter_kept_by,
            )
        except Exception as e:
            logger.warning("inbox_pre_filter shadow log write failed (continuing): %s", e)

    logger.info(
        "email_arrived: id=%s subject=%r status=%s usage=%s",
        message_id[:16],
        email_payload["subject"][:60],
        result.get("status"),
        result.get("_usage"),
    )

    # Step 12 guardrail: per-call token-cap trip. claude_client returns this status
    # when the pre-call estimate exceeds the cap. iMessage Megha (or queue if quiet
    # hours) so a runaway thread or huge attachment doesn't silently drop.
    if result.get("status") == "skipped" and result.get("reason") == "token_cap_exceeded":
        estimated = result.get("estimated_input_tokens")
        cap = result.get("input_token_cap")
        log_trip(
            _runtime_events_path(config),
            guardrail="per_call_token_cap",
            threshold=cap,
            actual=estimated,
            action="halted_call",
            extra={"message_id": message_id, "subject": email_payload.get("subject", "")[:200]},
        )
        _log_email_event(
            config,
            email_payload=email_payload,
            message_id=message_id,
            subject=email_payload.get("subject", "")[:200],
            status="token_blowup",
            skip_reason=f"estimated_input_tokens={estimated} cap={cap}",
            latency_first_action_sec=_latency_sec(email_payload.get("received", "")),
            usage=None,
            source_account=source_account,
        )
        alert_text = (
            f"Heads up: I skipped one email because it would have used ~{estimated} input tokens "
            f"(cap is {cap}). Subject: {email_payload.get('subject', '')[:120]}. "
            f"You can handle it manually if it matters."
        )
        _h_mod._send_or_queue_alert(config, state_path, alert_text, kind="token_cap_trip",
                              extra={"message_id": message_id})
        _h_mod._dedup_record(message_id, {
            "decision": "token_blowup",
            "task_id": None,
            "decision_id": None,
        })
        return {"ts": utc_now_iso(), "message_id": message_id, "result": result, "guardrail": "token_cap"}

    if result.get("status") != "task":
        logger.info("  -> SKIPPED: %s", result.get("reason", "")[:200])
        _log_email_event(
            config,
            email_payload=email_payload,
            message_id=message_id,
            subject=email_payload.get("subject", "")[:200],
            status="skipped",
            skip_reason=result.get("reason", "")[:200],
            latency_first_action_sec=_latency_sec(email_payload.get("received", "")),
            usage=result.get("_usage"),
            package_id=package_id,
            lifecycle_state=lifecycle_state_hint,
            source_account=source_account,
        )
        _h_mod._dedup_record(message_id, {
            "decision": "skipped",
            "task_id": None,
            "decision_id": None,
        })
        _h_mod._check_spend_cap_after_call(config, state_path)
        return {"ts": utc_now_iso(), "message_id": message_id, "result": result}

    task = result["task"]
    task["source_email_id"] = message_id
    task["source_subject"] = email_payload["subject"]
    list_id = _resolve_shared_list_id(graph, source_account, config)

    # Serialize per-message to prevent concurrent notifications creating duplicates.
    with _per_message_lock(message_id):
        cached = _h_mod._dedup_check(message_id)
        if cached:
            cached_task_id = cached.get("task_id") or ""
            logger.info("  -> DEDUP HIT (memory): task already created in this window (id=%s)", cached_task_id[:12])
            _log_webhook_redup_hit(
                config,
                email_payload=email_payload,
                message_id=message_id,
                prior_outcome=cached,
                branch="create",
                source_account=source_account,
            )
            return {"ts": utc_now_iso(), "message_id": message_id, "result": result, "task_id": cached_task_id, "dedup": "memory"}

        existing = graph.find_todo_task_by_source_email(list_id, message_id, account=source_account)
        if existing:
            _h_mod._dedup_record(message_id, {
                "decision": "dedup_hit",
                "task_id": existing,
                "decision_id": None,
            })
            logger.info("  -> DEDUP HIT (graph): task already exists (id=%s)", existing[:12])
            return {"ts": utc_now_iso(), "message_id": message_id, "result": result, "task_id": existing, "dedup": "graph"}

        # Semantic dedup (added 2026-04-29 evening). ID-based dedup catches the same
        # email twice; semantic catches different emails about the same thing (school
        # sends a registration link AND a calendar reminder; both would otherwise
        # become two "Bayview tennis registration" tasks).
        _owner_abbrev_dedup = {"megha": "MJ", "max": "MM"}.get(task.get("owner", ""), "??")
        _proposed = f"{_owner_abbrev_dedup} {task.get('title', '')}"
        if task.get("confidence") == "low":
            _proposed = f"[?] {_proposed}"
        proposed_title_for_dedup = _proposed
        try:
            # Switched 2026-05-08 from list_recent_todo_tasks(top=25) +
            # post-filter to list_open_todo_tasks (server-side $filter on
            # status). Same intent — semantic dedup against open tasks —
            # but server-side filtering avoids the same recency-window
            # truncation bug that hit the action-layer matcher today.
            open_tasks_raw = graph.list_open_todo_tasks(
                list_id, top=100, account=source_account,
            )
            recent_active = [
                {"id": t["id"], "title": t.get("title", "")}
                for t in open_tasks_raw
                if t.get("title")
            ]
        except Exception as e:
            logger.warning("semantic dedup: list_open_todo_tasks failed (skipping dedup check): %s", e)
            recent_active = []

        if recent_active:
            try:
                sem_dup = claude.check_semantic_duplicate(
                    proposed_title_for_dedup, recent_active,
                    proposed_sender=email_payload.get("from_address", ""),
                    proposed_subject=email_payload.get("subject", ""),
                )
            except Exception as e:
                logger.warning("semantic dedup: check failed (proceeding with create): %s", e)
                sem_dup = {"is_duplicate": False}
            if sem_dup.get("is_duplicate"):
                matches_id = sem_dup.get("matches_task_id", "")
                # Look up the matched task's title from the candidate slate
                # we already passed to the judge. Lets Megha see "Matched
                # existing task: <title>" in the HTML viewer without
                # cross-referencing MS To Do manually.
                matched_title = next(
                    (t.get("title") for t in recent_active if t.get("id") == matches_id),
                    None,
                )
                _h_mod._dedup_record(message_id, {
                    "decision": "dedup_hit",
                    "task_id": matches_id,
                    "decision_id": None,
                })
                logger.info("  -> DEDUP HIT (semantic): proposed=%r matches=%s reason=%r",
                            proposed_title_for_dedup[:80], matches_id[:12], sem_dup.get("reason", "")[:120])
                _log_email_event(
                    config,
                    email_payload=email_payload,
                    message_id=message_id,
                    subject=email_payload.get("subject", "")[:200],
                    status="dedup_semantic",
                    matches_task_id=matches_id,
                    matched_task_id=matches_id,
                    matched_task_title=matched_title,
                    proposed_title=proposed_title_for_dedup[:200],
                    dedup_reason=sem_dup.get("reason", "")[:300],
                    latency_first_action_sec=_latency_sec(email_payload.get("received", "")),
                    usage=result.get("_usage"),
                    package_id=package_id,
                    lifecycle_state=lifecycle_state_hint,
                    source_account=source_account,
                )
                return {"ts": utc_now_iso(), "message_id": message_id, "result": result,
                        "task_id": matches_id, "dedup": "semantic"}

        # Financial-domain task body redaction (added 2026-05-05; spec:
        # capabilities/realtime-kavi.md Guardrails row 4). When the email
        # arrives from a known financial-domain sender (banks, brokerages,
        # insurers, IRS, schools collecting payment), the task body is
        # paraphrased only — sender + subject summary + "see source email"
        # pointer; verbatim numerics never enter the task body. The title
        # keeps the human-readable description so Megha can recognize the
        # task at a glance, but dollar amounts in the title are stripped
        # through the same scanner so a number that survives doesn't leak.
        from kavi_runtime import financial_domains as _fin
        task_for_create = dict(task)
        subject_for_create = email_payload.get("subject", "")
        if _fin.is_financial_domain_sender(email_payload.get("from_address")):
            redacted_body = _fin.redact_task_body_for_financial_sender(
                from_name=email_payload.get("from_name", ""),
                subject=subject_for_create,
            )
            # graph_client.create_todo_task builds the body from
            # `task['owner_reason']` + source_subject. Replacing
            # owner_reason with the paraphrased body keeps the existing
            # body-build path intact while landing the redaction.
            task_for_create["owner_reason"] = redacted_body
            # Strip dollar amounts from the title so a `$630.00` in the
            # title doesn't survive into MS To Do.
            task_for_create["title"] = _fin.strip_dollar_amounts(
                task_for_create.get("title", "")
            )
            # Strip dollar amounts from the source-subject path used by
            # graph_client.create_todo_task to render the task body's
            # `From:` line.
            subject_for_create = _fin.strip_dollar_amounts(subject_for_create)
            logger.info(
                "financial_domain_redaction: applied sender=%s message_id=%s",
                email_payload.get("from_address", "")[:60], message_id[:16],
            )

        # Spoof-suspected title prefix (Fix 2 of 2026-05-06 audit). Prepend
        # `[VERIFY SENDER]` so the To Do row screams "verify before action"
        # at a glance. The `[?]` low-conf prefix (added in graph_client) is
        # additive — both can be present.
        if email_payload.get("spoof_suspected"):
            current_title = task_for_create.get("title", "")
            if not current_title.startswith("[VERIFY SENDER]"):
                task_for_create["title"] = f"[VERIFY SENDER] {current_title}"

        try:
            task_id = graph.create_todo_task(
                list_id, task_for_create, message_id, subject_for_create,
                account=source_account,
            )
            _h_mod._dedup_record(message_id, {
                "decision": "created",
                "task_id": task_id,
                "decision_id": None,
            })
            logger.info(
                "  -> TASK CREATED: id=%s title=%r owner=%s confidence=%s tag=%s reason=%r",
                task_id[:12], task_for_create.get("title", "")[:80], task_for_create.get("owner"),
                task_for_create.get("confidence"), task_for_create.get("source_tag"),
                task_for_create.get("owner_reason", "")[:200],
            )
            try:
                from kavi_runtime.structured_log import log_event
                # Local import (circular-safe): OWNER_ABBREV lives in the
                # dispatch module. Unbound here since the Phase 4 move —
                # the NameError was swallowed by this try/except, so the
                # task_created telemetry event silently never fired
                # (2026-06-10 lost-import sweep).
                from kavi_runtime.runtime.imessage_dispatch import OWNER_ABBREV
                log_event(
                    "outbound", "task_created",
                    source="email",
                    owner=OWNER_ABBREV.get(task_for_create.get("owner") or "", "??"),
                    dedup_hit=False,
                    confidence=task_for_create.get("confidence"),
                )
            except Exception:
                logger.debug("structured_log task_created emit failed (continuing)")
        except Exception as e:
            logger.exception("MS To Do create failed: %s", e)
            return {"ts": utc_now_iso(), "message_id": message_id, "result": result, "error": "todo_create_failed"}

    # Routing: low-conf or priority → wants iMessage; high-conf non-priority → silent + queued.
    # Quiet hours (11 PM - 7 AM): suppress all Kavi-initiated iMessages; surface in 7 AM digest.
    priority_senders = config.get("priority_senders", [])
    sender_name = email_payload.get("from_name", "")
    sender_addr = email_payload.get("from_address", "")
    is_priority = any(p.lower() in sender_name.lower() or p.lower() in sender_addr.lower() for p in priority_senders)
    confidence = task.get("confidence", "medium")
    state_path = Path(config["paths"]["imessage_state"])
    quiet = _h_mod.is_quiet_hours(config)

    owner_abbrev = {"megha": "MJ", "max": "MM"}.get(task["owner"], "??")
    title_rendered = f"{owner_abbrev} {task.get('title', '')}"

    # Always register low-conf pending question — digest can surface it if iMessage is suppressed.
    if confidence == "low":
        pending = _h_mod.list_pending_questions(state_path)
        question_index = len(pending) + 1
        add_pending_question(state_path, {
            "id": f"q-{uuid.uuid4().hex[:8]}",
            "task_id": task_id,
            "task_title_rendered": title_rendered,
            "source_email_id": message_id,
            "source_subject": email_payload["subject"],
            "asked_at": utc_now_iso(),
        })
    else:
        question_index = None

    # Updated 2026-04-29 evening: priority sender no longer triggers immediate iMessage.
    # Priority tasks still get queued (with is_priority flag) and surfaced in the
    # next morning/evening summary. Only low-confidence tasks interrupt Megha.
    wants_imessage = confidence == "low"

    if wants_imessage and not quiet:
        # Migrated 2026-05-04: low-conf Q&A prompt is LLM-composed via persona prompt
        # (skill: qa_question_composer.md). Cold fallback drops the dead "X yes / X no"
        # reply syntax — Megha replies free-form; the reply intent parser reads it.
        msg_text = None
        msg_provenance = {"llm_call": "compose_qa_question"}
        if confidence == "low":
            _, claude_for_qa, _ = _h_mod._get_clients(config)
            msg_text = claude_for_qa.compose_qa_question(
                title=task.get("title", ""),
                source_subject=email_payload.get("subject", "")[:200],
                owner_abbrev=owner_abbrev,
            )
        if msg_text is None:
            # Cold fallback: templated, but no reply syntax (per persona spec).
            # AUDIT 2026-06-10: title + source subject only, no action claims.
            msg_text = f"{title_rendered}\nFrom: {email_payload['subject'][:80]}"
            if confidence == "low":
                msg_text = f"[?] {msg_text}"
            msg_provenance = {"fallback_audit": "2026-06-10"}
        # Route the clarifying question to the TASK OWNER, not unconditionally to
        # Megha (Megha 2026-06-22): a Max-owned task's "worth a task?" question
        # goes to Max. Only a genuinely unsure owner (unassigned) or Megha's own
        # tasks route to Megha — that is the existing "?" / Megha-resolves path.
        _owner = task.get("owner")
        _to_max = _owner == "max"
        _recipient_handle = (
            config.get("imessage", {}).get("max_phone") if _to_max else None
        )
        send_result = _h_mod._send_imessage_with_fallback(
            config, msg_text, kind="task_notification",
            provenance=msg_provenance,
            recipient_handle=_recipient_handle,
            # Outlook fallback misdelivers to Megha's inbox regardless of
            # addressee (known bug); suppress it for Max so a BlueBubbles
            # failure logs rather than landing Max's question in Megha's inbox.
            suppress_outlook_fallback=_to_max,
        )
        logger.info("  -> iMessage send: verified=%s fallback=%s (confidence=%s, priority=%s, q_index=%s)",
                    send_result["verified"], send_result["fallback_used"],
                    confidence, is_priority, question_index)
    elif wants_imessage and quiet:
        logger.info("  -> iMessage SUPPRESSED (quiet hours; confidence=%s, priority=%s, q_index=%s)",
                    confidence, is_priority, question_index)
        # High-conf priority during quiet hours: queue for digest. Low-conf already in pending_questions.
        if is_priority and confidence != "low":
            enqueue_summary_item(state_path, {
                "task_id": task_id,
                "title": title_rendered,
                "added_at": utc_now_iso(),
                "is_priority": True,
            })
    else:
        # High-conf, non-priority: silent path. Queue for next periodic summary.
        enqueue_summary_item(state_path, {
            "task_id": task_id,
            "title": title_rendered,
            "added_at": utc_now_iso(),
            "is_priority": False,
        })
        logger.info("  -> queued for summary (high-conf, non-priority)")

    # Stamp spoof_suspected onto the audit row's `reason` field (Fix 2 of
    # 2026-05-06 audit). Task rows don't normally carry a reason — null for
    # clean creates — so injecting `spoof_suspected: <brand>` here is
    # zero-friction for /eval-inbox-week and gives Megha a labelable signal.
    spoof_reason: str | None = None
    if email_payload.get("spoof_suspected"):
        spoof_reason = f"spoof_suspected: {email_payload.get('spoof_brand', 'unknown')}"

    _log_email_event(
        config,
        email_payload=email_payload,
        message_id=message_id,
        subject=email_payload.get("subject", "")[:200],
        status="task",
        skip_reason=spoof_reason,
        task_id=task_id,
        title=task_for_create.get("title", "")[:200],
        owner=task.get("owner"),
        confidence=confidence,
        tag=task.get("source_tag"),
        is_priority=is_priority,
        imessage_sent=wants_imessage and not quiet,
        imessage_suppressed_quiet_hours=wants_imessage and quiet,
        queued_for_summary=not wants_imessage or (is_priority and quiet and confidence != "low"),
        latency_first_action_sec=_latency_sec(email_payload.get("received", "")),
        usage=result.get("_usage"),
        package_id=package_id,
        lifecycle_state=lifecycle_state_hint,
        source_account=source_account,
    )

    # Step 12 guardrail: spend cap. After the row lands in runs.jsonl, check the
    # rolling monthly total. If we just crossed $30, set the pause flag and notify.
    _h_mod._check_spend_cap_after_call(config, state_path)

    return {"ts": utc_now_iso(), "message_id": message_id, "result": result, "task_id": task_id}





__all__ = [
    "email_arrived",
    "_email_arrived_impl",
]
