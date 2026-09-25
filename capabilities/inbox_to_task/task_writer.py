"""inbox-to-task task writer — MS To Do API + lifecycle update machinery.

Phase 4 (2026-06-02): physical move out of `kavi_runtime/handlers.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from kavi_runtime.graph_client import GraphClient
from kavi_runtime import lifecycle as lifecycle_module
from kavi_runtime.lifecycle import *  # noqa: F401,F403
from kavi_runtime.state import is_quiet_hours, utc_now_iso
from kavi_runtime.runtime import clients as _clients_module
from kavi_runtime.runtime import send_imessage as _send_imessage_module
from kavi_runtime.runtime.time_utils import _latency_sec

from capabilities.inbox_to_task.eval_log import _log_email_event
from capabilities.inbox_to_task.selection import _inbox_owner_abbrev

logger = logging.getLogger(__name__)


def _resolve_shared_list_id(graph: GraphClient, source_account: str | None, config: dict) -> str:
    """Return the McMullen-Jain Shared list ID for the right account.

    The shared list has a different ID per account (Microsoft Graph
    multi-tenant behavior on shared MS To Do lists). Resolution order:
      1. When `source_account` is provided, ask graph_client to look up
         and cache that account's view of the list by display name.
      2. When `source_account` is None and we have a config-level
         `mstodo_shared_list_id`, use it (legacy single-account path).
      3. Otherwise fall back to the default account's resolved list ID.
    """
    if source_account is None:
        legacy = config.get("graph", {}).get("mstodo_shared_list_id")
        if legacy:
            return legacy
        return graph.resolve_shared_list_id(account=None)
    return graph.resolve_shared_list_id(account=source_account)


def _apply_lifecycle_update(
    *,
    existing_task_id: str,
    lifecycle_state: str,
    email_payload: dict[str, Any],
    config: dict,
    package_id: str | None,
    package_match_tier: str,
    merge_reason: str | None,
    message_id: str,
    source_account: str | None = None,
) -> dict[str, Any]:
    """Apply a package lifecycle UPDATE to an existing MS To Do task.

    Routes the email to the lifecycle state machine (kavi_runtime.lifecycle)
    instead of the LLM-judgment path. Updates the title + body of
    `existing_task_id`, fires an iMessage iff the transition is "Out for
    Delivery" (porch-monitoring urgency), and emits an eval row tagged
    decision="updated" with the 5 package-lifecycle fields.
    """
    graph, _, _ = _clients_module._get_clients(config)
    list_id = _resolve_shared_list_id(graph, source_account, config)
    owner_abbrev = _inbox_owner_abbrev(config, source_account)

    try:
        existing = graph.get_todo_task(list_id, existing_task_id, account=source_account)
    except Exception as e:
        logger.warning(
            "lifecycle_update: get_todo_task failed task_id=%s err=%s; will template fresh body",
            existing_task_id[:12], e,
        )
        existing = {"title": "", "body": {"content": ""}}

    existing_title = existing.get("title") or ""
    existing_body = (existing.get("body") or {}).get("content") or ""

    email_payload_for_template = dict(email_payload)
    if package_id:
        email_payload_for_template["package_id"] = package_id

    rendered = lifecycle_module.apply_transition(
        state=lifecycle_state,
        existing_task_title=existing_title,
        existing_task_body=existing_body,
        email_payload=email_payload_for_template,
        owner_abbrev=owner_abbrev,
    )

    if rendered is None:
        logger.warning(
            "lifecycle_update: unknown state %r; falling through to no-op log",
            lifecycle_state,
        )
        _log_email_event(
            config,
            email_payload=email_payload,
            message_id=message_id,
            subject=email_payload.get("subject", "")[:200],
            status="skipped",
            skip_reason=f"lifecycle_unknown_state:{lifecycle_state}",
            package_id=package_id,
            package_match_tier=package_match_tier,
            merge_target_task_id=existing_task_id,
            merge_reason=merge_reason,
            lifecycle_state=lifecycle_state,
            latency_first_action_sec=_latency_sec(email_payload.get("received", "")),
            usage=None,
            source_account=source_account,
        )
        return {"status": "lifecycle_unknown_state", "message_id": message_id}

    new_title = rendered["new_title"]
    new_body = rendered["new_body"]

    update_ok = True
    update_err: str | None = None
    try:
        graph.update_todo_task(
            list_id,
            existing_task_id,
            {
                "title": new_title,
                "body": {"contentType": "text", "content": new_body},
            },
            account=source_account,
        )
        logger.info(
            "lifecycle_update: task_id=%s state=%s tier=%s new_title=%r",
            existing_task_id[:12], lifecycle_state, package_match_tier, new_title[:80],
        )
    except Exception as e:
        update_ok = False
        update_err = str(e)[:200]
        logger.exception("lifecycle_update: PATCH failed: %s", e)

    sent_imessage = False
    if update_ok and lifecycle_state == "Out for Delivery" and not is_quiet_hours(config):
        merchant_label = lifecycle_module._merchant_label(email_payload)
        ofd_text = (
            f"Heads up - your {merchant_label} order is out for delivery today."
        )
        # Conformance-sweep finding (2026-06-10 provenance grandfather
        # pass): deterministic out-for-delivery heads-up, neither LLM nor
        # previously audited. AUDIT 2026-06-10: honest, grounded in the
        # parsed lifecycle state, no household action claims. Listed for
        # LLM migration if the lifecycle messages grow content.
        send_result = _send_imessage_module._send_imessage_with_fallback(
            config, ofd_text, kind="lifecycle_ofd",
            provenance={"fallback_audit": "2026-06-10"},
        )
        sent_imessage = bool(send_result.get("verified") or send_result.get("fallback_used"))

    _log_email_event(
        config,
        email_payload=email_payload,
        message_id=message_id,
        subject=email_payload.get("subject", "")[:200],
        status="lifecycle_updated" if update_ok else "skipped",
        skip_reason=update_err if not update_ok else None,
        task_id=existing_task_id,
        title=new_title[:200],
        package_id=package_id,
        package_match_tier=package_match_tier,
        merge_target_task_id=existing_task_id,
        merge_reason=merge_reason,
        lifecycle_state=lifecycle_state,
        imessage_sent=sent_imessage,
        latency_first_action_sec=_latency_sec(email_payload.get("received", "")),
        usage=None,
        task_body_length=len(new_body) if update_ok else None,
        source_account=source_account,
        matched_task_id=existing_task_id,
        matched_task_title=existing_title or None,
    )

    return {
        "ts": utc_now_iso(),
        "message_id": message_id,
        "task_id": existing_task_id,
        "lifecycle_state": lifecycle_state,
        "package_match_tier": package_match_tier,
        "imessage_sent": sent_imessage,
    }


__all__ = ["_apply_lifecycle_update", "_resolve_shared_list_id", "GraphClient"]
